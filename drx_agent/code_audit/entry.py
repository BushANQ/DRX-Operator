"""Command line entry for the code audit mode (``--code-audit``)."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from drx_agent.code_audit.config import (
    Config,
    ConfigError,
    discover_config_path,
    load_config,
)
from drx_agent.code_audit.paniclog import recover_with_context
from drx_agent.code_audit.prompt import load_prompts


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def resolve_audit_path(path: str | None) -> str | None:
    """Validate an explicit audit target directory.

    ``None`` means the directory is chosen inside the TUI, which keeps the
    interactive behaviour of the original workflow.
    """
    if path is None:
        return None
    candidate = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(candidate):
        raise ValueError(f"审计目录不存在：{candidate}")
    if not os.path.isdir(candidate):
        raise ValueError(f"审计路径不是目录：{candidate}")
    return candidate


def _load_drx_llm_config() -> dict:
    """Read the main DRX configuration to reuse existing provider settings."""
    try:
        from drx_agent.main import _config_path

        path = _config_path()
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        llm = data.get("llm") or {}
        return llm if isinstance(llm, dict) else {}
    except Exception:
        return {}


def apply_environment_defaults(cfg: Config) -> list[str]:
    """Fill missing credentials/provider fields from the DRX environment.

    Returns human-readable notices for the TUI log; never raises.
    """
    notices: list[str] = []
    llm = _load_drx_llm_config()

    if not cfg.openai.api_key:
        for env_name in ("DRX_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
            value = os.environ.get(env_name, "").strip()
            if value:
                cfg.openai.api_key = value
                if not cfg.compress_openai.api_key:
                    cfg.compress_openai.api_key = value
                notices.append(f"使用环境变量 {env_name} 中的模型凭据")
                break
    if not cfg.openai.api_key and llm.get("api_key"):
        cfg.openai.api_key = str(llm["api_key"])
        if not cfg.compress_openai.api_key:
            cfg.compress_openai.api_key = cfg.openai.api_key
        notices.append("使用主配置 llm.api_key 中的模型凭据")
    if not cfg.openai.base_url and llm.get("base_url"):
        cfg.openai.base_url = str(llm["base_url"])
        if not cfg.compress_openai.base_url:
            cfg.compress_openai.base_url = cfg.openai.base_url
        notices.append("使用主配置 llm.base_url")
    if not cfg.openai.model and llm.get("model"):
        cfg.openai.model = str(llm["model"])
        if not cfg.compress_openai.model:
            cfg.compress_openai.model = cfg.openai.model
        notices.append("使用主配置 llm.model")

    if not cfg.openai.api_key:
        notices.append(
            "未找到模型凭据：请在审计配置中填写 openai.api_key / api_key_env，"
            "或设置 DRX_LLM_API_KEY / DEEPSEEK_API_KEY"
        )
    return notices


def run_code_audit(config_path: str | None, audit_path: str | None) -> int:
    try:
        return _run_code_audit(config_path, audit_path)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        recover_with_context("main", exc)
        return 1


def _run_code_audit(config_path: str | None, audit_path: str | None) -> int:
    cfg_path = discover_config_path(config_path)
    if not os.path.isfile(cfg_path):
        return _fail(
            f"未找到代码审计配置：{cfg_path}\n"
            "使用 --audit-config <文件> 指定配置，或复制 configs/audit_config.yaml 后重试。\n"
            "示例：python -m drx_agent.main --code-audit "
            "--audit-config configs/audit_config.yaml --audit-path ./target"
        )

    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        return _fail(f"加载代码审计配置失败：{exc}")

    try:
        target = resolve_audit_path(audit_path)
    except ValueError as exc:
        return _fail(str(exc))
    if target is not None:
        cfg.workspace = target

    try:
        prompts = load_prompts(cfg.prompts)
    except ConfigError as exc:
        return _fail(f"加载审计提示词失败：{exc}")

    notices = apply_environment_defaults(cfg)

    return _run_tui(cfg, prompts, target, notices)


def _run_tui(cfg: Config, prompts, target: str | None, notices: list[str]) -> int:
    from drx_agent.code_audit.llm import OpenAIClient
    from drx_agent.code_audit.tools import Registry
    from drx_agent.code_audit.agent import Team
    from drx_agent.code_audit.tui import CodeAuditApp

    workspace = cfg.workspace if target is not None else os.path.abspath(
        cfg.workspace if cfg.workspace and cfg.workspace != "." else "."
    )
    try:
        registry = Registry(workspace, cfg.agent.max_tool_result_chars)
    except (OSError, ValueError) as exc:
        return _fail(f"初始化审计工具失败：{exc}")

    client = OpenAIClient(cfg.openai)
    compress_client = OpenAIClient(cfg.compress_openai)
    team = Team(cfg, prompts, client, compress_client, registry)
    if notices:
        for notice in notices:
            print(f"[code-audit] {notice}", file=sys.stderr)

    try:
        app = CodeAuditApp(team, cfg, target or "", notices=notices)
        app.run()
        return app.return_code or 0
    finally:
        try:
            asyncio.run(team.close())
        except Exception:
            pass
        try:
            asyncio.run(client.close())
            if compress_client is not client:
                asyncio.run(compress_client.close())
        except Exception:
            pass
