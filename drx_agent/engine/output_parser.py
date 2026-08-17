import json
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedOutput:
    raw: str
    json_data: Optional[dict] = None
    key_values: dict = field(default_factory=dict)
    is_structured: bool = False


def parse_output(stdout: str) -> ParsedOutput:
    parsed = ParsedOutput(raw=stdout)
    json_match = re.search(r'\{[\s\S]*\}', stdout)
    if json_match:
        try:
            parsed.json_data = json.loads(json_match.group(0))
            parsed.is_structured = True
        except json.JSONDecodeError:
            pass
    kv_pattern = re.findall(r'(\w[\w_]*)\s*[:=]\s*(.+)', stdout)
    for k, v in kv_pattern:
        if k not in parsed.key_values:
            parsed.key_values[k] = v.strip()
    return parsed
