"""Configuration loading and validation for the code audit subsystem.

Relative prompt/skill paths in a configuration file are resolved against the
configuration file directory so that the shipped package resources work without
a chdir.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(PACKAGE_DIR, "prompts")
SKILLS_DIR = os.path.join(PACKAGE_DIR, "skills")


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


@dataclass
class ThinkingConfig:
    type: str = "enabled"


@dataclass
class OpenAIConfig:
    base_url: str = ""
    api_interface: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    temperature: float = 0.0
    top_p: float = 0.0
    max_context_tokens: int = 0
    max_output_tokens: int = 0
    timeout_seconds: int = 0
    stream: bool = False
    thinking: ThinkingConfig | None = None
    reasoning_effort: str = ""

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OpenAIConfig):
            return NotImplemented
        return _openai_signature(self) == _openai_signature(other)


def _openai_signature(cfg: OpenAIConfig) -> tuple:
    thinking = None if cfg.thinking is None else (cfg.thinking.type,)
    return (
        cfg.base_url, cfg.api_interface, cfg.api_key, cfg.api_key_env,
        cfg.model, cfg.temperature, cfg.top_p, cfg.max_context_tokens,
        cfg.max_output_tokens, cfg.timeout_seconds, cfg.stream, thinking,
        cfg.reasoning_effort,
    )


@dataclass
class PromptConfig:
    system: str = ""
    plan_system: str = ""
    compress: str = ""
    skills_dir: str = ""
    templates_dir: str = ""


@dataclass
class AgentConfig:
    max_turns: int = 0
    session_dir: str = ""
    log_session: bool = False
    log_session_dir: str = ""
    retry_attempts: int = 0
    compress_at_ratio: float = 0.0
    compress_buffer_tokens: int = 0
    auto_plan: bool = False
    max_tool_result_chars: int = 0
    recon_agents: int = 0
    audit_agents: int = 0
    forum_wait_seconds: int = 0
    moderator_enabled: bool | None = None
    moderator_interval_seconds: int = 0
    infinite_mode: bool = False
    budget_hours: int = 0
    budget_minutes: int = 0
    budget_tokens: int = 0

    def moderator_is_enabled(self) -> bool:
        return self.moderator_enabled is None or bool(self.moderator_enabled)

    def budget_duration(self) -> float:
        """Return the time budget in seconds.

        Zero means unlimited.  Negative values and overflow are rejected
        before any timer is created.
        """
        max_minutes = (2**63 - 1) // 60
        if self.budget_hours < 0 or self.budget_minutes < 0 or self.budget_tokens < 0:
            raise ConfigError("audit budgets must be nonnegative; 0 means unlimited")
        if self.budget_minutes > max_minutes:
            raise ConfigError("audit time budget overflows")
        if self.budget_hours > (max_minutes - self.budget_minutes) // 60:
            raise ConfigError("audit time budget overflows")
        return float((self.budget_hours * 60 + self.budget_minutes) * 60)


@dataclass
class Config:
    workspace: str = ""
    openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    compress_openai: OpenAIConfig = field(default_factory=OpenAIConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    config_path: str = ""

    def compression_threshold(self) -> int:
        """Estimated-token admission limit for one audit agent's history."""
        context = self.openai.max_context_tokens
        output = self.openai.max_output_tokens
        ratio = self.agent.compress_at_ratio
        if context <= 0 or output <= 0 or output >= context:
            raise ConfigError("openai token limits must be positive with output below context")
        if math.isnan(ratio) or math.isinf(ratio) or ratio <= 0 or ratio >= 1:
            raise ConfigError("agent.compress_at_ratio must be finite and strictly between 0 and 1")
        if self.agent.compress_buffer_tokens < 0 or self.agent.max_tool_result_chars < 0 or self.agent.max_tool_result_chars > 65536:
            raise ConfigError("compression reserve and tool result bounds are invalid")
        tool_bytes = self.agent.max_tool_result_chars
        if tool_bytes < 1024:
            tool_bytes = 1024
        reserve = tool_bytes + 2048 + 256
        if self.agent.compress_buffer_tokens > reserve:
            reserve = self.agent.compress_buffer_tokens
        if reserve >= context - output:
            raise ConfigError("openai context cannot fit output plus compression/tool/notification reserve")
        limit = context - output - reserve
        ratio_limit = math.floor(float(context) * ratio)
        if ratio_limit < float(limit):
            limit = int(ratio_limit)
        if limit < 1024:
            raise ConfigError("effective compression threshold must leave at least 1024 estimated input tokens")
        return limit

    def compression_input_budget(self) -> int:
        context = self.compress_openai.max_context_tokens
        output = self.compress_openai.max_output_tokens
        if context <= 0 or output <= 0 or output >= context:
            raise ConfigError("compress_openai token limits must be positive with output below context")
        return context - output


def _load_yaml(path: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ConfigError("PyYAML is required to load an audit configuration") from exc
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"invalid config {path}: root must be a mapping")
    return data


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _num(value: Any, name: str, default: float, *, integer: bool = False) -> int | float:
    if value is None:
        return default
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    return number


def _thinking_from(value: Any, name: str) -> ThinkingConfig | None:
    if value is None:
        return None
    mapping = _as_mapping(value, name)
    kind = mapping.get("type", "")
    if not isinstance(kind, str):
        raise ConfigError(f"{name}.type must be a string")
    return ThinkingConfig(type=kind)


def _openai_from(data: dict[str, Any], name: str) -> OpenAIConfig:
    return OpenAIConfig(
        base_url=str(data.get("base_url") or ""),
        api_interface=str(data.get("api_interface") or ""),
        api_key=str(data.get("api_key") or ""),
        api_key_env=str(data.get("api_key_env") or ""),
        model=str(data.get("model") or ""),
        temperature=float(_num(data.get("temperature"), f"{name}.temperature", 0.0)),
        top_p=float(_num(data.get("top_p"), f"{name}.top_p", 0.0)),
        max_context_tokens=int(_num(data.get("max_context_tokens"), f"{name}.max_context_tokens", 0, integer=True)),
        max_output_tokens=int(_num(data.get("max_output_tokens"), f"{name}.max_output_tokens", 0, integer=True)),
        timeout_seconds=int(_num(data.get("timeout_seconds"), f"{name}.timeout_seconds", 0, integer=True)),
        stream=bool(data.get("stream", False)),
        thinking=_thinking_from(data.get("thinking"), f"{name}.thinking"),
        reasoning_effort=str(data.get("reasoning_effort") or ""),
    )


def _agent_from(data: dict[str, Any]) -> AgentConfig:
    moderator = data.get("moderator_enabled")
    if moderator is not None and not isinstance(moderator, bool):
        raise ConfigError("agent.moderator_enabled must be a boolean")
    return AgentConfig(
        max_turns=int(_num(data.get("max_turns"), "agent.max_turns", 0, integer=True)),
        session_dir=str(data.get("session_dir") or ""),
        log_session=bool(data.get("log_session", False)),
        log_session_dir=str(data.get("log_session_dir") or ""),
        retry_attempts=int(_num(data.get("retry_attempts"), "agent.retry_attempts", 0, integer=True)),
        compress_at_ratio=float(_num(data.get("compress_at_ratio"), "agent.compress_at_ratio", 0.0)),
        compress_buffer_tokens=int(_num(data.get("compress_buffer_tokens"), "agent.compress_buffer_tokens", 0, integer=True)),
        auto_plan=bool(data.get("auto_plan", False)),
        max_tool_result_chars=int(_num(data.get("max_tool_result_chars"), "agent.max_tool_result_chars", 0, integer=True)),
        recon_agents=int(_num(data.get("recon_agents"), "agent.recon_agents", 0, integer=True)),
        audit_agents=int(_num(data.get("audit_agents"), "agent.audit_agents", 0, integer=True)),
        forum_wait_seconds=int(_num(data.get("forum_wait_seconds"), "agent.forum_wait_seconds", 0, integer=True)),
        moderator_enabled=moderator,
        moderator_interval_seconds=int(_num(data.get("moderator_interval_seconds"), "agent.moderator_interval_seconds", 0, integer=True)),
        infinite_mode=bool(data.get("infinite_mode", False)),
        budget_hours=int(_num(data.get("budget_hours"), "agent.budget_hours", 0, integer=True)),
        budget_minutes=int(_num(data.get("budget_minutes"), "agent.budget_minutes", 0, integer=True)),
        budget_tokens=int(_num(data.get("budget_tokens"), "agent.budget_tokens", 0, integer=True)),
    )


def _prompt_from(data: dict[str, Any]) -> PromptConfig:
    return PromptConfig(
        system=str(data.get("system") or ""),
        plan_system=str(data.get("plan_system") or ""),
        compress=str(data.get("compress") or ""),
        skills_dir=str(data.get("skills_dir") or ""),
        templates_dir=str(data.get("templates_dir") or ""),
    )


def _resolve_relative(path: str, base_dir: str) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.normpath(path)
    candidate = os.path.normpath(os.path.join(base_dir, path))
    if os.path.exists(candidate):
        return candidate
    if os.path.exists(path):
        return os.path.abspath(path)
    return candidate


def _resolve_api_key(cfg: OpenAIConfig) -> None:
    if cfg.api_key or not cfg.api_key_env:
        return
    if looks_like_api_key(cfg.api_key_env):
        cfg.api_key = cfg.api_key_env
    else:
        cfg.api_key = os.environ.get(cfg.api_key_env, "")


def looks_like_api_key(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if value.startswith("sk-"):
        return True
    return "-" in value and len(value) >= 20


def _validate_reasoning(cfg: OpenAIConfig, section: str) -> None:
    if cfg.thinking is not None and cfg.thinking.type != "enabled":
        raise ConfigError(f"{section}.thinking.type must be enabled; disabled is not supported")
    if cfg.reasoning_effort not in ("", "low", "high", "max"):
        raise ConfigError(f"{section}.reasoning_effort must be low, high or max")


def _merge_openai(seed: OpenAIConfig, data: dict[str, Any], name: str) -> OpenAIConfig:
    """Decode explicit keys over a seeded config, keeping explicit zeros."""
    cfg = _openai_from(data, name)
    if "base_url" not in data:
        cfg.base_url = seed.base_url
    if "api_interface" not in data:
        cfg.api_interface = seed.api_interface
    if "api_key" not in data:
        cfg.api_key = seed.api_key
    if "api_key_env" not in data:
        cfg.api_key_env = seed.api_key_env
    if "model" not in data:
        cfg.model = seed.model
    if "temperature" not in data:
        cfg.temperature = seed.temperature
    if "top_p" not in data:
        cfg.top_p = seed.top_p
    if "max_context_tokens" not in data:
        cfg.max_context_tokens = seed.max_context_tokens
    if "max_output_tokens" not in data:
        cfg.max_output_tokens = seed.max_output_tokens
    if "timeout_seconds" not in data:
        cfg.timeout_seconds = seed.timeout_seconds
    if "stream" not in data:
        cfg.stream = seed.stream
    if "reasoning_effort" not in data:
        cfg.reasoning_effort = seed.reasoning_effort
    return cfg


def _merge_agent(seed: AgentConfig, data: dict[str, Any]) -> AgentConfig:
    cfg = _agent_from(data)
    if "compress_at_ratio" not in data:
        cfg.compress_at_ratio = seed.compress_at_ratio
    if "budget_hours" not in data:
        cfg.budget_hours = seed.budget_hours
    return cfg


def _apply_defaults(cfg: Config) -> None:
    if cfg.openai.thinking is None:
        cfg.openai.thinking = ThinkingConfig(type="enabled")
    if not cfg.openai.reasoning_effort:
        cfg.openai.reasoning_effort = "high"
    if not cfg.workspace:
        cfg.workspace = "."
    if not cfg.openai.base_url:
        cfg.openai.base_url = "https://api.openai.com/v1"
    if not cfg.openai.api_interface:
        cfg.openai.api_interface = "responses"
    if not cfg.openai.model:
        cfg.openai.model = "gpt-4o-mini"
    if cfg.openai.top_p == 0:
        cfg.openai.top_p = 1
    if cfg.openai.timeout_seconds == 0:
        cfg.openai.timeout_seconds = 10
    _apply_compress_defaults(cfg)
    if not cfg.prompts.system:
        cfg.prompts.system = os.path.join(PROMPTS_DIR, "system.md")
    if not cfg.prompts.plan_system:
        cfg.prompts.plan_system = os.path.join(PROMPTS_DIR, "plan_system.md")
    if not cfg.prompts.compress:
        cfg.prompts.compress = os.path.join(PROMPTS_DIR, "compress.md")
    if not cfg.prompts.skills_dir:
        cfg.prompts.skills_dir = SKILLS_DIR
    if not cfg.prompts.templates_dir:
        cfg.prompts.templates_dir = os.path.join(PROMPTS_DIR, "templates")
    if not cfg.agent.session_dir:
        cfg.agent.session_dir = "sessions"
    if not cfg.agent.log_session_dir:
        cfg.agent.log_session_dir = "log_sessions"
    if cfg.agent.retry_attempts == 0:
        cfg.agent.retry_attempts = 3
    if cfg.agent.compress_buffer_tokens == 0:
        cfg.agent.compress_buffer_tokens = cfg.openai.max_output_tokens
        if cfg.agent.compress_buffer_tokens < 4096:
            cfg.agent.compress_buffer_tokens = 4096
    if cfg.agent.max_tool_result_chars == 0:
        cfg.agent.max_tool_result_chars = 12000
    if cfg.agent.recon_agents == 0:
        cfg.agent.recon_agents = 4
    if cfg.agent.audit_agents == 0:
        cfg.agent.audit_agents = 4
    if cfg.agent.moderator_interval_seconds == 0:
        cfg.agent.moderator_interval_seconds = 60
    if cfg.agent.forum_wait_seconds == 0:
        cfg.agent.forum_wait_seconds = 60


def _apply_compress_defaults(cfg: Config) -> None:
    compress = cfg.compress_openai
    if compress == OpenAIConfig():
        cfg.compress_openai = OpenAIConfig(
            base_url=cfg.openai.base_url,
            api_interface=cfg.openai.api_interface,
            api_key=cfg.openai.api_key,
            api_key_env=cfg.openai.api_key_env,
            model=cfg.openai.model,
            temperature=cfg.openai.temperature,
            top_p=cfg.openai.top_p,
            max_context_tokens=cfg.openai.max_context_tokens,
            max_output_tokens=cfg.openai.max_output_tokens,
            timeout_seconds=cfg.openai.timeout_seconds,
            stream=False,
            thinking=ThinkingConfig(type="enabled"),
            reasoning_effort=cfg.openai.reasoning_effort,
        )
        return
    if not compress.base_url:
        compress.base_url = cfg.openai.base_url
    if not compress.api_interface:
        compress.api_interface = cfg.openai.api_interface
    if compress.thinking is None:
        compress.thinking = ThinkingConfig(type="enabled")
    if not compress.reasoning_effort:
        compress.reasoning_effort = "high"
    if not compress.api_key and not compress.api_key_env:
        compress.api_key = cfg.openai.api_key
        compress.api_key_env = cfg.openai.api_key_env
    if not compress.model:
        compress.model = cfg.openai.model
    if compress.top_p == 0:
        compress.top_p = cfg.openai.top_p
    if compress.max_context_tokens == 0:
        compress.max_context_tokens = cfg.openai.max_context_tokens
    if compress.max_output_tokens == 0:
        compress.max_output_tokens = cfg.openai.max_output_tokens
    if compress.timeout_seconds == 0:
        compress.timeout_seconds = cfg.openai.timeout_seconds
    compress.stream = False


def _explicit_compress_zero(raw: Mapping[str, Any]) -> str | None:
    """Return the first explicitly invalid compressor limit, if any."""
    section = raw.get("compress_openai")
    if not isinstance(section, Mapping):
        return None
    for key in ("max_context_tokens", "max_output_tokens"):
        if key in section:
            value = section[key]
            try:
                if int(value) <= 0:
                    return key
            except (TypeError, ValueError):
                return key
    return None


def load_config(path: str) -> Config:
    """Load and validate an audit configuration from a YAML file."""
    raw = _load_yaml(path)
    base_dir = os.path.dirname(os.path.abspath(path))

    # Seed required defaults before decoding so that an explicit zero in the
    # file remains invalid instead of silently receiving a default.
    seeded_agent = AgentConfig(compress_at_ratio=0.75, budget_hours=8)
    seeded_openai = OpenAIConfig(max_context_tokens=32000, max_output_tokens=4096)
    cfg = Config(config_path=os.path.abspath(path))
    cfg.workspace = str(raw.get("workspace") or "")

    openai_raw = _as_mapping(raw.get("openai"), "openai")
    cfg.openai = _merge_openai(seeded_openai, openai_raw, "openai")
    cfg.compress_openai = _openai_from(
        _as_mapping(raw.get("compress_openai"), "compress_openai"), "compress_openai"
    )
    if raw.get("compress_openai") is None:
        cfg.compress_openai = OpenAIConfig()
    cfg.prompts = _prompt_from(_as_mapping(raw.get("prompts"), "prompts"))
    cfg.agent = _merge_agent(seeded_agent, _as_mapping(raw.get("agent"), "agent"))

    bad = _explicit_compress_zero(raw)
    if bad is not None:
        raise ConfigError(f"compress_openai.{bad} must be positive")

    _apply_defaults(cfg)
    _validate_reasoning(cfg.openai, "openai")
    _validate_reasoning(cfg.compress_openai, "compress_openai")
    cfg.agent.budget_duration()
    if not 1 <= cfg.agent.recon_agents <= 666 or not 1 <= cfg.agent.audit_agents <= 666:
        raise ConfigError("agent.recon_agents and agent.audit_agents must be between 1 and 666")
    if not 1 <= cfg.agent.moderator_interval_seconds <= 86400:
        raise ConfigError("agent.moderator_interval_seconds must be between 1 and 86400")
    if not 1 <= cfg.agent.forum_wait_seconds <= 120:
        raise ConfigError("agent.forum_wait_seconds must be between 1 and 120")
    if not 1024 <= cfg.agent.max_tool_result_chars <= 65536:
        raise ConfigError("agent.max_tool_result_chars must be between 1024 and 65536 (serialized UTF-8 bytes)")
    cfg.compression_threshold()
    cfg.compression_input_budget()

    cfg.prompts.system = _resolve_relative(cfg.prompts.system, base_dir)
    cfg.prompts.plan_system = _resolve_relative(cfg.prompts.plan_system, base_dir)
    cfg.prompts.compress = _resolve_relative(cfg.prompts.compress, base_dir)
    cfg.prompts.skills_dir = _resolve_relative(cfg.prompts.skills_dir, base_dir)
    cfg.prompts.templates_dir = _resolve_relative(cfg.prompts.templates_dir, base_dir)

    _resolve_api_key(cfg.openai)
    _resolve_api_key(cfg.compress_openai)
    return cfg


def discover_config_path(explicit: str | None = None) -> str:
    """Resolve the audit configuration path for the command line.

    Priority: explicit ``--audit-config`` -> ``$DRX_AUDIT_CONFIG`` ->
    ``<repo>/configs/audit_config.yaml`` ->
    ``~/.config/drx-operator/audit_config.yaml`` ->
    packaged ``configs/audit_config.yaml`` beside the code_audit package.
    """
    if explicit:
        return os.path.abspath(explicit)
    env = os.environ.get("DRX_AUDIT_CONFIG")
    if env:
        return os.path.abspath(env)
    repo = os.path.dirname(os.path.dirname(PACKAGE_DIR))
    candidates = [
        os.path.join(repo, "configs", "audit_config.yaml"),
        os.path.join(os.path.expanduser("~"), ".config", "drx-operator", "audit_config.yaml"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(PACKAGE_DIR, "configs", "audit_config.yaml")


def config_template_path() -> str:
    return os.path.join(PACKAGE_DIR, "configs", "audit_config.yaml")
