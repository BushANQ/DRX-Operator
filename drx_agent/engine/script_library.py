import json
import os
import time
from pathlib import Path


class ScriptLibrary:
    def __init__(self, storage_dir=None):
        self._storage = storage_dir or os.path.join(
            os.path.dirname(__file__), "..", "..", "script_library"
        )
        self._index: dict = {}
        self._load_index()

    def _load_index(self):
        index_path = os.path.join(self._storage, "index.json")
        if os.path.exists(index_path):
            with open(index_path) as f:
                self._index = json.load(f)

    def _save_index(self):
        os.makedirs(self._storage, exist_ok=True)
        with open(os.path.join(self._storage, "index.json"), "w") as f:
            json.dump(self._index, f, indent=2, ensure_ascii=False)

    def save(self, name, category, code, params=None, source_skill=None):
        self._index[name] = {
            "name": name, "category": category, "code": code,
            "params": params or {},
            "source_skill": source_skill,
            "usage_count": 0,
            "created_at": time.time(), "updated_at": time.time(),
        }
        self._save_index()

    def get(self, name):
        return self._index.get(name)

    def search(self, query="", category=None):
        results = []
        for name, tmpl in self._index.items():
            if category and tmpl.get("category") != category:
                continue
            if query and query.lower() not in name.lower():
                continue
            results.append(tmpl)
        return sorted(results, key=lambda t: -t.get("usage_count", 0))

    def render(self, template, params):
        code = template["code"]
        for key, value in params.items():
            code = code.replace(f"{{{{{key}}}}}", str(value))
        return code

    def increment_usage(self, name):
        if name in self._index:
            self._index[name]["usage_count"] += 1
            self._index[name]["updated_at"] = time.time()
            self._save_index()

    def list_categories(self):
        cats = set()
        for tmpl in self._index.values():
            cats.add(tmpl.get("category", "uncategorized"))
        return sorted(cats)
