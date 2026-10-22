"""Tests for prompt, skill and template loading."""

from __future__ import annotations

from drx_agent.code_audit.config import PromptConfig, PROMPTS_DIR, SKILLS_DIR
from drx_agent.code_audit.prompt import (
    Prompts,
    Skill,
    load_prompts,
    load_skills,
    load_templates,
    summarize_skill,
)


def test_load_shipped_prompts():
    prompts = load_prompts(
        PromptConfig(
            system=f"{PROMPTS_DIR}/system.md",
            plan_system=f"{PROMPTS_DIR}/plan_system.md",
            compress=f"{PROMPTS_DIR}/compress.md",
            skills_dir=SKILLS_DIR,
            templates_dir=f"{PROMPTS_DIR}/templates",
        )
    )
    assert "代码审计" in prompts.system
    assert prompts.plan_system
    assert prompts.compress
    assert [skill.name for skill in prompts.skills] == ["java-security", "web-audit"]
    assert "initial_audit_instruction" in prompts.templates
    assert "end_audit_confirmation" in prompts.templates
    assert "verify_finding" in prompts.templates


def test_render_template_replaces_all_occurrences():
    prompts = Prompts(
        system="", plan_system="", compress="",
        templates={"demo": "A !{x} B !{x} C !{y}"},
    )
    assert prompts.render_template("demo", {"x": "1", "y": "2"}) == "A 1 B 1 C 2"
    assert prompts.render_template("missing", {"x": "1"}) == ""


def test_skill_listing_and_load_state():
    prompts = Prompts(
        system="SYS", plan_system="PLAN", compress="COMP",
        skills=[Skill("alpha", "# Alpha\n\nfirst line\nsecond"),
                Skill("beta", "beta summary")],
    )
    listed = prompts.system_with_skills()
    assert "# Available Skills" in listed
    assert "- alpha：first line" in listed
    assert "- beta：beta summary" in listed
    assert "load_skill" in listed
    assert prompts.load_skill("alpha") is True
    assert prompts.load_skill("alpha") is False
    assert prompts.load_skill("missing") is False
    loaded = prompts.system_with_skills()
    assert "# Loaded Skills" in loaded
    assert "## Skill: alpha" in loaded
    assert "first line" in loaded
    assert prompts.loaded_skill_names() == ["alpha"]
    assert prompts.has_skill("beta") is True
    prompts.set_loaded_skills(["beta", "missing"])
    assert prompts.loaded_skill_names() == ["beta"]
    clone = prompts.clone()
    clone.load_skill("alpha")
    assert prompts.loaded_skill_names() == ["beta"]


def test_plan_system_with_skills_uses_plan_prompt():
    prompts = Prompts(system="SYS", plan_system="PLAN", compress="", skills=[Skill("s", "x")])
    assert prompts.plan_system_with_skills().startswith("PLAN")


def test_summarize_skill_skips_headings_and_bullets():
    assert summarize_skill("# Title\n\n- item\n") == "item"
    assert summarize_skill("* lead\n") == "lead"
    assert summarize_skill("# only heading\n") == ""


def test_load_skills_missing_directory(tmp_path):
    assert load_skills(str(tmp_path / "none")) == []


def test_load_skills_skips_dirs_without_skill_md(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "SKILL.md").write_text("one", encoding="utf-8")
    (tmp_path / "two").mkdir()
    skills = load_skills(str(tmp_path))
    assert [(skill.name, skill.content) for skill in skills] == [("one", "one")]


def test_load_templates_missing_directory(tmp_path):
    assert load_templates(str(tmp_path / "none")) == {}


def test_load_templates_reads_markdown_only(tmp_path):
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B", encoding="utf-8")
    assert load_templates(str(tmp_path)) == {"a": "A"}
