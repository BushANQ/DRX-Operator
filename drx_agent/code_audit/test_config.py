"""Tests for audit configuration loading, defaults and validation."""

from __future__ import annotations

import os

import pytest
import yaml

from drx_agent.code_audit.config import (
    ConfigError,
    load_config,
    looks_like_api_key,
)


def write_config(tmp_path, data: dict) -> str:
    path = tmp_path / "audit.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


BASE = {
    "openai": {
        "api_interface": "responses",
        "base_url": "https://example.invalid/v1",
        "model": "test-model",
        "max_context_tokens": 100000,
        "max_output_tokens": 8000,
        "temperature": 0.2,
        "stream": True,
    },
    "agent": {
        "recon_agents": 2,
        "audit_agents": 3,
        "max_tool_result_chars": 12000,
    },
}


def test_defaults_are_seeded(tmp_path):
    cfg = load_config(write_config(tmp_path, {"openai": BASE["openai"]}))
    assert cfg.agent.recon_agents == 4
    assert cfg.agent.audit_agents == 4
    assert cfg.agent.retry_attempts == 3
    assert cfg.agent.forum_wait_seconds == 60
    assert cfg.agent.moderator_interval_seconds == 60
    assert cfg.agent.compress_at_ratio == 0.75
    assert cfg.agent.budget_hours == 8
    assert cfg.agent.moderator_is_enabled() is True
    assert cfg.openai.thinking is not None
    assert cfg.openai.thinking.type == "enabled"
    assert cfg.workspace == "."
    assert cfg.agent.max_tool_result_chars == 12000


def test_explicit_zero_token_limits_are_invalid(tmp_path):
    data = {"openai": dict(BASE["openai"], max_context_tokens=0)}
    with pytest.raises(ConfigError, match="token limits"):
        load_config(write_config(tmp_path, data))


def test_explicit_compress_zero_is_invalid(tmp_path):
    data = {
        "openai": BASE["openai"],
        "compress_openai": {"max_context_tokens": 0},
    }
    with pytest.raises(ConfigError, match="compress_openai.max_context_tokens"):
        load_config(write_config(tmp_path, data))


def test_compress_inherits_main_model(tmp_path):
    cfg = load_config(write_config(tmp_path, BASE))
    assert cfg.compress_openai.base_url == BASE["openai"]["base_url"]
    assert cfg.compress_openai.model == BASE["openai"]["model"]
    assert cfg.compress_openai.max_context_tokens == 100000
    assert cfg.compress_openai.max_output_tokens == 8000
    assert cfg.compress_openai.stream is False


def test_agent_limits_are_validated(tmp_path):
    bad_agents = dict(BASE, agent={"recon_agents": 0, "audit_agents": 1})
    with pytest.raises(ConfigError, match="recon_agents"):
        # 0 is seeded to the default, so use an out-of-range explicit value.
        bad_agents["agent"]["recon_agents"] = 667
        load_config(write_config(tmp_path, bad_agents))

    bad_wait = dict(BASE, agent={"forum_wait_seconds": 121})
    with pytest.raises(ConfigError, match="forum_wait_seconds"):
        load_config(write_config(tmp_path, bad_wait))

    bad_interval = dict(BASE, agent={"moderator_interval_seconds": 0})
    # 0 is seeded to 60, so explicit 86401 is the invalid boundary.
    bad_interval["agent"]["moderator_interval_seconds"] = 86401
    with pytest.raises(ConfigError, match="moderator_interval_seconds"):
        load_config(write_config(tmp_path, bad_interval))


def test_reasoning_validation(tmp_path):
    data = {"openai": dict(BASE["openai"], reasoning_effort="turbo")}
    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_config(write_config(tmp_path, data))
    data = {"openai": dict(BASE["openai"], thinking={"type": "disabled"})}
    with pytest.raises(ConfigError, match="thinking.type"):
        load_config(write_config(tmp_path, data))


def test_compression_threshold(tmp_path):
    cfg = load_config(write_config(tmp_path, BASE))
    # context 100000, output 8000, reserve max(12000+2048+256, 8000)=14304
    # effective limit = min(100000-8000-14304, floor(100000*0.75)) = 75000
    assert cfg.compression_threshold() == 75000
    assert cfg.compression_input_budget() == 92000


def test_compression_ratio_limit(tmp_path):
    data = {
        "openai": BASE["openai"],
        "agent": {"compress_at_ratio": 0.2},
    }
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.compression_threshold() == 20000


def test_api_key_resolution_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_TEST_KEY", "sk-from-env")
    data = {"openai": dict(BASE["openai"], api_key_env="AUDIT_TEST_KEY")}
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.openai.api_key == "sk-from-env"


def test_api_key_literal_in_env_field(tmp_path):
    literal = "sk-literal-key-value-123456"
    data = {"openai": dict(BASE["openai"], api_key_env=literal)}
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.openai.api_key == literal


def test_relative_prompt_paths_resolve_against_config(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "system.md").write_text("SYS", encoding="utf-8")
    (prompts / "plan.md").write_text("PLAN", encoding="utf-8")
    (prompts / "compress.md").write_text("COMP", encoding="utf-8")
    data = {
        "openai": BASE["openai"],
        "prompts": {
            "system": "prompts/system.md",
            "plan_system": "prompts/plan.md",
            "compress": "prompts/compress.md",
        },
    }
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.prompts.system == str(prompts / "system.md")


def test_missing_file_raises():
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config("definitely-not-here.yaml")


def test_invalid_yaml_root(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        load_config(str(path))


def test_budget_duration_and_validation(tmp_path):
    cfg = load_config(write_config(tmp_path, dict(BASE, agent={"budget_hours": 2, "budget_minutes": 30})))
    assert cfg.agent.budget_duration() == 9000
    with pytest.raises(ConfigError, match="nonnegative"):
        load_config(write_config(tmp_path, dict(BASE, agent={"budget_tokens": -1})))


def test_looks_like_api_key():
    assert looks_like_api_key("sk-123")
    assert looks_like_api_key("abcdefghijklmnopqrst-uvwx")
    assert not looks_like_api_key("AUDIT_TEST_KEY")
    assert not looks_like_api_key("")
