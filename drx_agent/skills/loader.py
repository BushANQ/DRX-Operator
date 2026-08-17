import os
import yaml


class SkillLoader:
    def load(self, skill_dir: str) -> dict:
        yaml_path = os.path.join(skill_dir, "skill.yaml")
        with open(yaml_path) as f:
            meta = yaml.safe_load(f)
        skill = dict(meta)
        skill["directory"] = skill_dir

        for name in ["detect.py", "exploit.py"]:
            path = os.path.join(skill_dir, name)
            if os.path.exists(path):
                with open(path) as f:
                    skill[f"{name.replace('.py', '')}_code"] = f.read()

        prompt_path = os.path.join(skill_dir, "system_prompt.md")
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                skill["system_prompt"] = f.read()
        return skill

    def load_all(self, skills_root: str) -> list[dict]:
        skills = []
        if not os.path.isdir(skills_root):
            return skills
        for entry in os.listdir(skills_root):
            skill_dir = os.path.join(skills_root, entry)
            if os.path.isdir(skill_dir) and os.path.exists(os.path.join(skill_dir, "skill.yaml")):
                try:
                    skills.append(self.load(skill_dir))
                except Exception:
                    pass
        return skills
