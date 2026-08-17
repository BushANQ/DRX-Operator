class SkillsRegistry:
    def __init__(self):
        self._skills: dict[str, dict] = {}
        self._by_category: dict[str, list[str]] = {}

    def add_skill(self, skill: dict) -> None:
        name = skill["name"]
        self._skills[name] = skill
        cat = skill.get("category", "uncategorized")
        self._by_category.setdefault(cat, []).append(name)

    def get(self, name: str) -> dict | None:
        return self._skills.get(name)

    def match(self, conditions: list[str]) -> list[dict]:
        matches = []
        for name, skill in self._skills.items():
            triggers = skill.get("trigger_conditions", [])
            if any(c in triggers for c in conditions):
                matches.append(skill)
        return matches

    def list_by_category(self, category: str) -> list[dict]:
        names = self._by_category.get(category, [])
        return [self._skills[n] for n in names if n in self._skills]

    def all_skills(self) -> list[dict]:
        return list(self._skills.values())

    def load_from_directory(self, skills_dir: str) -> int:
        from drx_agent.skills.loader import SkillLoader

        loader = SkillLoader()
        skills = loader.load_all(skills_dir)
        for skill in skills:
            self.add_skill(skill)
        return len(skills)
