"""Prompt, skill and template loading for the code audit subsystem."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from drx_agent.code_audit.config import ConfigError, PromptConfig


@dataclass
class Skill:
    name: str
    content: str


@dataclass
class Prompts:
    system: str
    plan_system: str
    compress: str
    skills: list[Skill] = field(default_factory=list)
    templates: dict[str, str] = field(default_factory=dict)
    loaded: list[str] = field(default_factory=list)

    def render_template(self, name: str, variables: dict[str, str]) -> str:
        template = self.templates.get(name, "")
        for key, value in variables.items():
            template = template.replace("!{" + key + "}", value)
        return template

    def system_with_skills(self) -> str:
        return self._system_with_skills(self.system)

    def plan_system_with_skills(self) -> str:
        return self._system_with_skills(self.plan_system)

    def _system_with_skills(self, system: str) -> str:
        parts = [system]
        if self.skills:
            parts.append("\n\n# Available Skills\n")
            parts.append(
                "如需使用某个 skill，必须先调用 `load_skill`。你可以按需加载多个不同 skill "
                "并组合使用；加载后不要重复加载同一个 skill。可用 skills：\n"
            )
            for skill in self.skills:
                line = "- " + skill.name
                summary = summarize_skill(skill.content)
                if summary:
                    line += "：" + summary
                parts.append(line + "\n")
        loaded = self.loaded_skills()
        if not loaded:
            return "".join(parts)
        parts.append("\n\n# Loaded Skills\n")
        for skill in loaded:
            parts.append("\n## Skill: " + skill.name + "\n")
            parts.append(skill.content)
            parts.append("\n")
        return "".join(parts)

    def load_skill(self, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        if name in self.loaded:
            return False
        for skill in self.skills:
            if skill.name == name:
                self.loaded.append(name)
                return True
        return False

    def set_loaded_skills(self, names: list[str]) -> None:
        self.loaded = []
        for name in names:
            self.load_skill(name)

    def loaded_skills(self) -> list[Skill]:
        out: list[Skill] = []
        for name in self.loaded:
            for skill in self.skills:
                if skill.name == name:
                    out.append(skill)
                    break
        return out

    def loaded_skill_names(self) -> list[str]:
        return list(self.loaded)

    def has_skill(self, name: str) -> bool:
        return any(skill.name == name for skill in self.skills)

    def clone(self) -> "Prompts":
        """Return an independent copy, including loaded-skill state."""
        return Prompts(
            system=self.system,
            plan_system=self.plan_system,
            compress=self.compress,
            skills=list(self.skills),
            templates=dict(self.templates),
            loaded=list(self.loaded),
        )


def summarize_skill(content: str) -> str:
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:]
        elif line.startswith("* "):
            line = line[2:]
        return line
    return ""


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fp:
        return fp.read()


def load_prompts(cfg: PromptConfig) -> Prompts:
    try:
        system = _read(cfg.system)
        plan_system = _read(cfg.plan_system)
        compress = _read(cfg.compress)
    except OSError as exc:
        raise ConfigError(f"load prompts: {exc}") from exc
    skills = load_skills(cfg.skills_dir)
    templates = load_templates(cfg.templates_dir)
    return Prompts(
        system=system,
        plan_system=plan_system,
        compress=compress,
        skills=skills,
        templates=templates,
    )


def load_skills(root: str) -> list[Skill]:
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ConfigError(f"load skills: {exc}") from exc
    skills: list[Skill] = []
    for entry in entries:
        directory = os.path.join(root, entry)
        if not os.path.isdir(directory):
            continue
        path = os.path.join(directory, "SKILL.md")
        try:
            content = _read(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(f"load skill {entry}: {exc}") from exc
        skills.append(Skill(name=entry, content=content))
    skills.sort(key=lambda skill: skill.name)
    return skills


def load_templates(root: str) -> dict[str, str]:
    templates: dict[str, str] = {}
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return templates
    except OSError as exc:
        raise ConfigError(f"load templates: {exc}") from exc
    for entry in entries:
        if not entry.endswith(".md"):
            continue
        path = os.path.join(root, entry)
        if not os.path.isfile(path):
            continue
        try:
            content = _read(path)
        except OSError as exc:
            raise ConfigError(f"load template {entry}: {exc}") from exc
        templates[entry[: -len(".md")]] = content
    return templates
