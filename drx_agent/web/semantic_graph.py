"""Execution membership and recorded evidence relations for saved sessions.

Conversation and actor groups are presentation structure, never causal claims.
Every relation carries its source field; unresolved references remain explicit.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json


def _string(value):
    return value if isinstance(value, str) and value.strip() else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return _object(json.loads(value))
        except (TypeError, ValueError):
            pass
    return {}


def _id(kind, value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return f"{kind}-{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"


def _content(message):
    value = message.get("content")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(block["text"] for block in value if isinstance(block, dict) and isinstance(block.get("text"), str))
    return ""


def _runtime_context(text):
    return isinstance(text, str) and text.startswith("【宿主运行状态 — 完整快照，后出现的快照替代先前快照】\n")


def _message_calls(message):
    calls = [(item.get("id"), item) for item in _items(message.get("tool_calls")) if isinstance(item, dict)]
    calls.extend((block.get("id"), block) for block in _items(message.get("content")) if isinstance(block, dict) and block.get("type") == "tool_use")
    return calls


def _canonical(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _fingerprint(tool, inputs, output):
    return tool, _canonical(inputs), _canonical(output)


class _Graph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def node(self, identity, label, kind, source, *, status=None, action=None):
        if identity not in self.nodes:
            self.nodes[identity] = {
                "id": identity, "label": _string(label) or "无", "kind": kind,
                "status": _string(status), "actionId": action.get("id") if action else None,
                "step": action.get("step") if action else None, "source": deepcopy(source),
            }
        return identity

    def edge(self, source, target, relation, label, info):
        if source == target or source not in self.nodes or target not in self.nodes:
            return
        identity = _id("edge", [source, target, relation])
        self.edges[identity] = {"id": identity, "source": source, "target": target,
                                "relation": relation, "label": label, "sourceInfo": deepcopy(info)}

    def export(self):
        return {"nodes": list(self.nodes.values()), "edges": list(self.edges.values())}


def _sources(action):
    record = _object(action.get("data"))
    event = _object(record.get("data"))
    return [record, event]


def _call_ids(action):
    found = set()
    for source in _sources(action):
        for field in ("tool_call_id", "invocation_id", "call_seq"):
            value = source.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                found.add(str(value))
    source_id = action.get("sourceId")
    if isinstance(source_id, str):
        try:
            parts = json.loads(source_id)
            if isinstance(parts, list) and len(parts) == 3 and isinstance(parts[0], str) and parts[0] in {"tool", "legacy-tool"}:
                found.add(str(parts[2]))
        except ValueError:
            pass
    return found


def _plan_records(extra):
    records = []
    for index, todo in enumerate(_items(extra.get("todos"))):
        if isinstance(todo, dict):
            records.append(("task", todo, f"metadata.extra.todos[{index}]"))
    frontier = _object(extra.get("frontier"))
    for index, intent in enumerate(_items(frontier.get("intents"))):
        if isinstance(intent, dict):
            records.append(("intent", intent, f"metadata.extra.frontier.intents[{index}]"))
    return records


def _execution(raw, actions, extra):
    from .execution_graph import build_execution_graph
    return build_execution_graph(raw, actions, extra)


def _finding_records(kb):
    findings = kb.get("findings")
    if isinstance(findings, dict):
        for host, records in findings.items():
            for index, record in enumerate(records if isinstance(records, list) else [records]):
                if isinstance(record, dict):
                    yield record, f"kb_data.findings.{host}[{index}]", host
    elif isinstance(findings, list):
        for index, record in enumerate(findings):
            if isinstance(record, dict):
                yield record, f"kb_data.findings[{index}]", record.get("host")


def _causal(raw, extra):
    graph = _Graph()
    kb = _object(raw.get("kb_data"))
    known = {}
    intents = [(record, path) for kind, record, path in _plan_records(extra) if kind == "intent"]
    intent_nodes = {}
    for record, path in intents:
        identity = _id("causal-record", record["id"]) if _string(record.get("id")) else _id("causal-intent", path)
        intent_nodes[path] = graph.node(identity, record.get("hypothesis") or record.get("action") or record.get("id"),
                                       "hypothesis", {"path": path, "record": record, "taskStatus": record.get("status")})
        if _string(record.get("id")):
            known[record["id"]] = (record, path, "hypothesis")
    for section, entries in _object(_object(kb.get("blackboard")).get("entries")).items():
        for index, record in enumerate(_items(entries)):
            if not isinstance(record, dict):
                continue
            path = f"kb_data.blackboard.entries.{section}[{index}]"
            kind = "hypothesis" if section == "hypotheses" else "fact"
            if _string(record.get("id")):
                known[record["id"]] = (record, path, kind)
            if section in {"hypotheses", "findings"}:
                identity = _id("causal-record", record["id"]) if _string(record.get("id")) else _id("causal-blackboard", path)
                graph.node(identity, record.get("text"), kind, {"path": path, "record": record})
    findings = list(_finding_records(kb))
    finding_ids = {}
    finding_aliases = defaultdict(list)
    conflicting_aliases = set()
    for finding, path, host in findings:
        finding_id = _id("causal-finding", finding.get("id") or [host, path])
        finding_ids[path] = graph.node(finding_id, finding.get("claim") or finding.get("title"), "finding",
                                       {"path": path, "record": finding, "host": host}, status=finding.get("status"))
        claim = _string(finding.get("claim"))
        recorded_host = _string(finding.get("host"))
        hosts = {value for value in (host, recorded_host) if _string(value)}
        if claim:
            for candidate_host in hosts:
                alias = f"{candidate_host}::{claim}"
                finding_aliases[alias].append((finding_id, path))
                if len(hosts) > 1:
                    conflicting_aliases.add(alias)
        for index, evidence in enumerate(_items(finding.get("evidence"))):
            if not isinstance(evidence, dict):
                continue
            ref = _string(evidence.get("evidence_id")) or _string(evidence.get("id"))
            substantive = any(_string(evidence.get(key)) for key in ("value", "payload", "result", "source", "preview"))
            if ref and (ref not in known or substantive):
                known[ref] = (evidence, f"{path}.evidence[{index}]", "evidence" if substantive else "evidence_reference")

    def reference(value, path, kind="reference"):
        if not _string(value):
            return None
        if value in finding_aliases:
            matches = finding_aliases[value]
            if len(matches) == 1 and value not in known and value not in conflicting_aliases:
                return matches[0][0]
            return graph.node(_id("causal-unresolved", value), f"未解析引用 · {value}", kind,
                              {"path": path, "reference": value, "unresolved": True,
                               "reason": "ambiguous_finding_reference", "candidatePaths": [entry[1] for entry in matches]})
        if value in known:
            record, source_path, known_kind = known[value]
            return graph.node(_id("causal-record", value), record.get("hypothesis") or record.get("text") or record.get("value") or value,
                              known_kind, {"path": source_path, "record": record, "unresolved": known_kind == "evidence_reference"},
                              status=record.get("status") if known_kind not in {"hypothesis", "fact"} else None)
        return graph.node(_id("causal-record", value), f"未解析引用 · {value}", kind,
                          {"path": path, "reference": value, "unresolved": True})

    for finding, path, host in findings:
        finding_id = finding_ids[path]
        for index, evidence in enumerate(_items(finding.get("evidence"))):
            evidence_path = f"{path}.evidence[{index}]"
            if isinstance(evidence, str):
                evidence_id = reference(evidence, evidence_path, "evidence_reference")
            elif isinstance(evidence, dict) and evidence:
                ref = _string(evidence.get("evidence_id")) or _string(evidence.get("id"))
                evidence_id = _id("causal-record", ref) if ref else _id("causal-evidence", evidence_path)
                substantive = any(_string(evidence.get(key)) for key in ("value", "payload", "result", "source", "preview"))
                if ref:
                    evidence_id = reference(ref, evidence_path, "evidence_reference")
                else:
                    graph.node(evidence_id, evidence.get("value") or evidence.get("preview") or evidence.get("type"),
                               "evidence" if substantive else "evidence_reference",
                               {"path": evidence_path, "record": evidence, "unresolved": not substantive}, status=evidence.get("status"))
            else:
                continue
            if evidence_id:
                graph.edge(evidence_id, finding_id, "evidence_for", "记录的证据关联", {"path": evidence_path, "field": "evidence", "value": evidence})

    for intent, path in intents:
        for field, relation, label in (("depends_on", "depends_on", "记录的依赖"), ("evidence", "evidence_for", "记录的证据关联")):
            values = intent.get(field)
            values = [values] if isinstance(values, str) else _items(values)
            for index, value in enumerate(values):
                target = intent_nodes[path]
                source = reference(value, f"{path}.{field}[{index}]")
                if source and target:
                    graph.edge(source, target, relation, label, {"path": path, "field": field, "value": value})
    return graph.export()


def build_semantic_graphs(raw, actions):
    """Return an execution tree/forest and explicitly recorded causal relations."""
    raw = _object(raw)
    extra = _object(_object(raw.get("metadata")).get("extra"))
    actions = [action for action in _items(actions) if isinstance(action, dict)]
    return {"execution": _execution(raw, actions, extra), "causal": _causal(raw, extra)}
