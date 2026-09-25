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
    graph = _Graph()
    parents = {}
    children = defaultdict(set)
    action_nodes = {}
    refs = defaultdict(list)
    actor_tasks = defaultdict(list)
    plans = []

    def register(reference, node):
        if _string(reference) and node not in refs[reference]:
            refs[reference].append(node)

    def resolve(reference):
        matches = refs.get(reference, []) if isinstance(reference, str) else []
        return matches[0] if len(matches) == 1 else None

    def group(identity, label, path, grouping):
        return graph.node(identity, label, "group", {"path": path, "grouping": grouping})

    def attach(parent, child, relation, label, info):
        if child in parents or parent == child:
            return False
        if would_cycle(parent, child):
            return False
        parents[child] = parent
        children[parent].add(child)
        graph.edge(parent, child, relation, label, info)
        return True

    def would_cycle(parent, child):
        pending, seen = [child], set()
        while pending:
            cursor = pending.pop()
            if cursor == parent:
                return True
            if cursor not in seen:
                seen.add(cursor)
                pending.extend(children.get(cursor, ()))
        return False

    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        identity = _id("execution-action", action.get("id", index))
        action_nodes[action.get("id", index)] = identity
        graph.node(identity, action.get("title") or action.get("text"), "action",
                   {"path": f"actions[{index}]", "record": action.get("data", {}), "actor": action.get("actor"), "statusScope": "record"},
                   status=action.get("status"), action=action)
        register(action.get("sourceId"), identity)
        register(action.get("id"), identity)

    for kind, record, path in _plan_records(extra):
        identity = _id(f"execution-{kind}", record.get("id") or path)
        graph.node(identity, record.get("content") or record.get("hypothesis") or record.get("action") or record.get("id"),
                   kind, {"path": path, "record": record}, status=record.get("status"))
        register(record.get("id"), identity)
        plans.append((identity, kind, record, path))

    team = _object(extra.get("team"))
    members = [(item, f"metadata.extra.team.members[{index}]") for index, item in enumerate(_items(team.get("members"))) if isinstance(item, dict)]
    known_actors = {item.get("agent_id") for item, _ in members if _string(item.get("agent_id"))}
    for actor, resident in _object(team.get("residents")).items():
        if isinstance(resident, dict) and actor not in known_actors:
            members.append(({**resident, "agent_id": actor}, f"metadata.extra.team.residents.{actor}"))
    for record, path in members:
        actor = _string(record.get("agent_id"))
        identity = _id("execution-worker", actor or path)
        graph.node(identity, record.get("task") or actor, "worker_task", {"path": path, "record": record}, status=record.get("status"))
        if actor:
            actor_tasks[actor].append(identity)
        for field in ("task_id", "dispatch_id"):
            register(record.get(field), identity)
        plans.append((identity, "worker_task", record, path))

    # Worker event metadata is itself a saved task/dispatch record.
    for index, action in enumerate(actions):
        if action.get("kind") != "worker":
            continue
        node = action_nodes[action["id"]]
        event = _sources(action)[1]
        actor = _string(event.get("agent_id")) or _string(action.get("actor"))
        if _string(event.get("task")):
            graph.nodes[node]["kind"] = "worker_task"
            graph.nodes[node]["label"] = event["task"]
        for field in ("task_id", "dispatch_id"):
            register(event.get(field), node)
        if actor and not actor_tasks[actor]:
            actor_tasks[actor].append(node)

    def explicit_parent(record, child, path):
        for field in ("parent_id", "parent_task_id", "task_id", "intent_id", "dispatch_id", "spawned_from"):
            reference = _string(record.get(field))
            if not reference:
                continue
            parent = resolve(reference)
            if parent and parent != child:
                if attach(parent, child, "task_parent", "记录的任务归属", {"path": path, "field": field, "value": reference}):
                    return True
                graph.nodes[child]["source"].setdefault("unresolvedRelations", []).append({"field": field, "value": reference, "reason": "cyclic_or_multiple_parent"})
            elif not parent:
                graph.nodes[child]["source"].setdefault("unresolvedRelations", []).append({"field": field, "value": reference, "reason": "missing_or_ambiguous_reference"})
        return False

    for identity, _, record, path in plans:
        explicit_parent(record, identity, path)
    for index, action in enumerate(actions):
        identity = action_nodes[action["id"]]
        for source in _sources(action):
            if explicit_parent(source, identity, f"actions[{index}].data"):
                break
        # Tool parameters carrying an intent ID name the record the operation addresses.
        inputs = _json_object(action.get("fullInput"))
        if action.get("tool", "") in {"intent_claim", "intent_done", "intent_kill"} and inputs.get("intent_id"):
            explicit_parent(inputs, identity, f"actions[{index}].fullInput")

    # Dispatch results explicitly identify the worker returned by that invocation.
    for index, action in enumerate(actions):
        if action.get("tool") not in {"task", "dispatch_sub_agent"}:
            continue
        output = _json_object(action.get("fullOutput"))
        actor = _string(output.get("agent_id"))
        if actor and len(actor_tasks[actor]) == 1:
            attach(action_nodes[action["id"]], actor_tasks[actor][0], "dispatch", "记录的派发", {"path": f"actions[{index}].fullOutput", "field": "agent_id", "value": actor})

    # Recorded worker identity takes precedence over generic conversation groups.
    for index, action in enumerate(actions):
        actor = _string(action.get("actor"))
        matches = actor_tasks.get(actor, [])
        identity = action_nodes[action["id"]]
        if actor != "master" and len(matches) == 1 and matches[0] != identity:
            attach(matches[0], identity, "record_group", "同一执行者的记录", {"path": f"actions[{index}]", "field": "actor", "value": actor})

    # Match conversation tool calls by their IDs, never by command keywords.
    call_actions = defaultdict(list)
    content_actions = defaultdict(list)
    message_actions = defaultdict(list)
    for action in actions:
        for call_id in _call_ids(action):
            call_actions[call_id].append(action)
        if action.get("kind") == "message":
            message_actions[(action.get("role"), action.get("text") or "")].append(action)
        if action.get("kind") == "tool":
            content_actions[_fingerprint(action.get("tool"), action.get("fullInput"), action.get("fullOutput"))].append(action)
    messages = _items(raw.get("messages"))
    results = defaultdict(list)
    for message in messages:
        if not isinstance(message, dict):
            continue
        if _string(message.get("role")) in {"tool", "function"} and message.get("tool_call_id") is not None:
            results[str(message["tool_call_id"])].append(message.get("content"))
        for block in _items(message.get("content")):
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") is not None:
                results[str(block["tool_use_id"])].append(block.get("content"))
    message_fingerprints = defaultdict(int)

    def call_fingerprint(call_id, call):
        outputs = results.get(str(call_id), [])
        if len(outputs) != 1:
            return None
        function = _object(call.get("function"))
        return _fingerprint(function.get("name") or call.get("name"),
                            function.get("arguments") if function else call.get("input"), outputs[0])

    for message in messages:
        if isinstance(message, dict):
            for call_id, call in _message_calls(message):
                fingerprint = call_fingerprint(call_id, call)
                if fingerprint is not None:
                    message_fingerprints[fingerprint] += 1
    used_message_actions = set()
    current_request = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or _string(message.get("role")) not in {"user", "assistant"}:
            continue
        text = _content(message)
        calls = _message_calls(message)
        has_text = bool(text.strip())
        if not has_text and not calls:
            continue
        context = message["role"] == "user" and _runtime_context(text)
        tool_group = message["role"] == "assistant" and not has_text and bool(calls)
        kind = "context" if context else "tool_group" if tool_group else "request" if message["role"] == "user" else "turn"
        label = "宿主状态快照" if context else f"工具调用组 · {len(calls)}项" if tool_group else text
        matches = [action for action in message_actions[(message.get("role"), text)] if action["id"] not in used_message_actions]
        actor = _string(message.get("agent_id"))
        if actor:
            matches = [action for action in matches if action.get("actor") == actor]
        if matches:
            match = matches[0]
            used_message_actions.add(match["id"])
            identity = action_nodes[match["id"]]
            graph.nodes[identity]["kind"] = kind
            graph.nodes[identity]["source"]["grouping"] = "conversation"
        else:
            identity = _id("execution-message", message.get("id") or index)
            graph.node(identity, label, kind,
                       {"path": f"messages[{index}]", "record": message, "grouping": "conversation", "replayVisibility": "snapshot_only"})
        if context:
            graph.nodes[identity]["source"]["origin"] = "host_runtime_context"
            graph.nodes[identity]["label"] = label
        if tool_group:
            graph.nodes[identity]["label"] = label
            graph.nodes[identity]["source"]["origin"] = "assistant_tool_calls"
        if kind == "request":
            current_request = identity
        elif current_request:
            attach(current_request, identity, "record_group", "宿主上下文" if context else "对话记录分组", {"path": f"messages[{index}]", "field": "role", "value": message["role"]})
        for call_id, call in calls:
            candidates = call_actions.get(str(call_id), [])
            matching = "tool_call_id"
            if not candidates:
                fingerprint = call_fingerprint(call_id, call)
                if fingerprint is not None and message_fingerprints[fingerprint] == 1:
                    candidates = content_actions.get(fingerprint, [])
                    matching = "tool_input_output_exact"
            if actor:
                candidates = [action for action in candidates if action.get("actor") == actor]
            if len(candidates) == 1:
                attach(identity, action_nodes[candidates[0]["id"]], "record_group", "该回合的工具调用", {"path": f"messages[{index}].tool_calls", "field": "id", "value": call_id, "matching": matching})

    # Transcript-only sessions can still expose true user/assistant record groups.
    current_request, current_turn = None, {}
    for index, action in enumerate(actions):
        identity = action_nodes[action["id"]]
        actor = action.get("actor")
        if action.get("kind") == "message" and action.get("role") == "user":
            if _runtime_context(action.get("text")):
                graph.nodes[identity]["kind"] = "context"
                graph.nodes[identity]["label"] = "宿主状态快照"
                graph.nodes[identity]["source"]["origin"] = "host_runtime_context"
                if current_request:
                    attach(current_request, identity, "record_group", "宿主上下文", {"path": f"actions[{index}]", "field": "role", "value": "user"})
            else:
                current_request = identity
                current_turn = {}
                graph.nodes[identity]["kind"] = "request"
        elif action.get("kind") == "message" and action.get("role") == "assistant":
            if graph.nodes[identity]["source"].get("origin") != "assistant_tool_calls":
                graph.nodes[identity]["kind"] = "turn"
            current_turn[actor] = identity
            if current_request:
                attach(current_request, identity, "record_group", "对话记录分组", {"path": f"actions[{index}]", "field": "role", "value": "assistant"})
        elif action.get("kind") == "tool" and actor in current_turn:
            attach(current_turn[actor], identity, "record_group", "同一回合的记录分组", {"path": f"actions[{index}]", "field": "actor", "value": actor})

    # Preserve all remaining actions without asserting a task assignment.
    for index, action in enumerate(actions):
        identity = action_nodes[action["id"]]
        if identity in parents or graph.nodes[identity]["kind"] in {"request", "turn"}:
            continue
        actor = _string(action.get("actor"))
        matches = actor_tasks.get(actor, [])
        if len(matches) == 1 and matches[0] != identity:
            attach(matches[0], identity, "record_group", "同一执行者的记录", {"path": f"actions[{index}]", "field": "actor", "value": actor})
        if identity not in parents:
            group_id = group(_id("execution-unassigned", actor),
                             f"未关联任务的记录 · {actor}" if actor else "未关联任务的记录",
                             "actions", "actor" if actor else "unassigned")
            attach(group_id, identity, "record_group", "未关联记录分组", {"path": f"actions[{index}]", "field": "actor", "value": actor})
    for identity, kind, record, path in plans:
        if identity not in parents:
            group_id = group(_id("execution-plan-group", kind), "已保存子任务" if kind == "worker_task" else "已保存计划", path.rsplit("[", 1)[0], "saved_plan")
            attach(group_id, identity, "record_group", "已保存计划分组", {"path": path})
    # Explicit task dependencies are additional DAG edges, not a single owner.
    for identity, kind, record, path in plans:
        for field in ("depends_on", "dependencies"):
            values = record.get(field)
            values = [values] if isinstance(values, str) else _items(values)
            for value in values:
                predecessor = resolve(value)
                if predecessor and graph.nodes[predecessor]["kind"] in {"task", "intent", "worker_task"}:
                    if not would_cycle(predecessor, identity):
                        graph.edge(predecessor, identity, "dependency", "记录的任务依赖", {"path": path, "field": field, "value": value})
                        children[predecessor].add(identity)
                    else:
                        graph.nodes[identity]["source"].setdefault("unresolvedRelations", []).append({"field": field, "value": value, "reason": "cyclic_dependency"})
    for node in graph.nodes.values():
        if node["source"].get("origin") == "assistant_tool_calls" and node["actionId"] is None:
            matched = [graph.nodes[identity] for identity in children.get(node["id"], ())
                       if graph.nodes[identity]["actionId"] is not None and isinstance(graph.nodes[identity]["step"], int)]
            matched.sort(key=lambda child: (child["step"], child["actionId"]))
            if matched:
                node["step"] = min(child["step"] for child in matched)
                node["source"].pop("replayVisibility", None)
                node["source"]["stepSource"] = {"kind": "matched_tool_calls", "actionIds": [child["actionId"] for child in matched],
                                                "meaning": "记录定位，不代表执行时间"}
        if node["kind"] in {"request", "turn", "context"} or node["source"].get("origin") == "assistant_tool_calls":
            node["source"]["recordStatus"] = node["status"]
            node["status"] = None
    if graph.nodes:
        roots = [identity for identity in graph.nodes if identity not in parents]
        session_id = graph.node(_id("execution-session", raw.get("id") or raw.get("name") or "session"),
                                raw.get("name") or raw.get("id") or "会话记录", "session",
                                {"path": "session", "record": {"id": raw.get("id"), "name": raw.get("name")}})
        for root in roots:
            attach(session_id, root, "record_group", "会话包含", {"path": "session", "field": "id", "value": raw.get("id")})
    return graph.export()


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
