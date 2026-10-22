"""Tests for the code audit command line integration."""

from __future__ import annotations

import os
import sys

import pytest

from drx_agent.code_audit import entry


def test_resolve_audit_path_accepts_directory(tmp_path):
    assert entry.resolve_audit_path(str(tmp_path)) == os.path.abspath(str(tmp_path))


def test_resolve_audit_path_none_means_interactive():
    assert entry.resolve_audit_path(None) is None


def test_resolve_audit_path_missing(tmp_path):
    with pytest.raises(ValueError, match="审计目录不存在"):
        entry.resolve_audit_path(str(tmp_path / "missing"))


def test_resolve_audit_path_file(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="不是目录"):
        entry.resolve_audit_path(str(target))


def test_missing_config_returns_startup_error(tmp_path, capsys):
    code = entry.run_code_audit(str(tmp_path / "nope.yaml"), None)
    assert code == 2
    err = capsys.readouterr().err
    assert "未找到代码审计配置" in err
    assert "--audit-config" in err


def test_invalid_config_returns_startup_error(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("openai: [1, 2]\n", encoding="utf-8")
    code = entry.run_code_audit(str(path), None)
    assert code == 2
    assert "加载代码审计配置失败" in capsys.readouterr().err


def _write_valid_config(tmp_path) -> str:
    path = tmp_path / "audit.yaml"
    path.write_text(
        """
openai:
  base_url: https://example.invalid/v1
  model: test-model
  max_context_tokens: 64000
  max_output_tokens: 8000
agent:
  recon_agents: 1
  audit_agents: 1
""",
        encoding="utf-8",
    )
    return str(path)


def test_missing_prompts_returns_startup_error(tmp_path, capsys):
    cfg = _write_valid_config(tmp_path)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    # Missing system.md/plan_system.md/compress.md.
    path = tmp_path / "missing-prompts.yaml"
    path.write_text(
        f"openai:\n  max_context_tokens: 64000\n  max_output_tokens: 8000\n"
        f"prompts:\n  system: {prompts / 'system.md'}\n"
        f"  plan_system: {prompts / 'plan.md'}\n"
        f"  compress: {prompts / 'compress.md'}\n",
        encoding="utf-8",
    )
    code = entry.run_code_audit(str(path), None)
    assert code == 2
    assert "加载审计提示词失败" in capsys.readouterr().err


def test_audit_path_validation_happens_before_tui(tmp_path, capsys):
    cfg = _write_valid_config(tmp_path)
    code = entry.run_code_audit(cfg, str(tmp_path / "does-not-exist"))
    assert code == 2
    assert "审计目录不存在" in capsys.readouterr().err


def test_environment_defaults_use_drx_key(monkeypatch):
    from drx_agent.code_audit.config import Config, OpenAIConfig

    cfg = Config(openai=OpenAIConfig())
    monkeypatch.delenv("DRX_LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    notices = entry.apply_environment_defaults(cfg)
    assert cfg.openai.api_key == "sk-deepseek"
    assert any("DEEPSEEK_API_KEY" in notice for notice in notices)


def test_environment_defaults_warn_without_key(monkeypatch):
    from drx_agent.code_audit.config import Config, OpenAIConfig

    cfg = Config(openai=OpenAIConfig())
    for name in ("DRX_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(entry, "_load_drx_llm_config", lambda: {})
    notices = entry.apply_environment_defaults(cfg)
    assert any("未找到模型凭据" in notice for notice in notices)


def test_cli_without_code_audit_keeps_legacy_parser(monkeypatch, capsys):
    import drx_agent.main as main_module

    called = {}

    def fake_launch(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr("drx_agent.web.launcher.launch_web", fake_launch)
    monkeypatch.setattr(sys, "argv", ["drx", "--web", "--no-browser", "--no-agent"])
    assert main_module.main() == 0
    assert called["open_browser"] is False
    assert called["with_agent"] is False


def test_cli_code_audit_routes_to_entry(monkeypatch):
    import drx_agent.main as main_module

    called = {}

    def fake_run(config_path, audit_path):
        called["config"] = config_path
        called["path"] = audit_path
        return 7

    monkeypatch.setattr("drx_agent.code_audit.entry.run_code_audit", fake_run)
    monkeypatch.setattr(
        sys, "argv",
        ["drx", "--code-audit", "--audit-config", "c.yaml", "--audit-path", "target"],
    )
    assert main_module.main() == 7
    assert called == {"config": "c.yaml", "path": "target"}


def test_cli_rejects_audit_path_without_code_audit(monkeypatch):
    import drx_agent.main as main_module

    monkeypatch.setattr(sys, "argv", ["drx", "--audit-path", "target"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_cli_rejects_conflicting_modes(monkeypatch):
    import drx_agent.main as main_module

    monkeypatch.setattr(sys, "argv", ["drx", "--code-audit", "--web"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 2


def test_run_code_audit_wires_app_with_real_modules(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text("print('x')\n", encoding="utf-8")
    config_path = tmp_path / "audit.yaml"
    config_path.write_text(
        """
openai:
  base_url: http://fake.local/v1
  model: fake-model
  api_interface: chat
  max_context_tokens: 64000
  max_output_tokens: 8000
  stream: true
agent:
  recon_agents: 1
  audit_agents: 1
  moderator_enabled: false
  forum_wait_seconds: 1
  infinite_mode: false
""",
        encoding="utf-8",
    )
    captured = {}

    class FakeApp:
        def __init__(self, team, cfg, auto_dir, notices=None):
            self.team = team
            self.cfg = cfg
            self.auto_dir = auto_dir
            self.notices = list(notices or [])
            self.return_code = 0
            captured["team"] = team
            captured["cfg"] = cfg
            captured["auto_dir"] = auto_dir
            captured["notices"] = self.notices

        def run(self):
            captured["workspace"] = self.team.registry.workspace
            captured["phase"] = self.team.phase()
            captured["snapshot_ok"] = self.team.snapshot() is not None

    import drx_agent.code_audit.tui as tui_module

    monkeypatch.setattr(tui_module, "CodeAuditApp", FakeApp)
    monkeypatch.setenv("DRX_LLM_API_KEY", "sk-from-drx-env")
    code = entry.run_code_audit(str(config_path), str(target))
    assert code == 0
    assert captured["auto_dir"] == str(target)
    assert captured["cfg"].workspace == str(target)
    assert captured["phase"] == "recon"
    assert any("DRX_LLM_API_KEY" in notice for notice in captured["notices"])


def test_discover_config_path_prefers_repo_template(monkeypatch):
    from drx_agent.code_audit.config import discover_config_path

    monkeypatch.delenv("DRX_AUDIT_CONFIG", raising=False)
    path = discover_config_path(None)
    assert path.endswith("configs\\audit_config.yaml") or path.endswith(
        "configs/audit_config.yaml"
    )
    assert os.path.isfile(path)


def test_unexpected_failure_writes_panic_dump(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def explode(*args, **kwargs):
        raise RuntimeError("unexpected audit failure")

    monkeypatch.setattr(entry, "_run_code_audit", explode)
    code = entry.run_code_audit("config.yaml", None)
    assert code == 1
    assert "panic logged to" in capsys.readouterr().err
    dumps = list((tmp_path / "logs").glob("panic-*.log"))
    assert len(dumps) == 1
    assert "unexpected audit failure" in dumps[0].read_text(encoding="utf-8")
