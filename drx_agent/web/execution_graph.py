"""Project recorded task structure and tool execution without conversation nodes."""

from collections import defaultdict

from .semantic_graph import (
    _Graph, _call_ids, _fingerprint, _id, _items, _json_object,
    _message_calls, _object, _plan_records, _runtime_context, _sources, _string,
)


def _context(action):
    value = _object(_sources(action)[1].get("execution_context"))
    return value if value.get("version") == 1 else {}


def _legacy_rounds(raw, tools):
    """Match exact saved calls; completed todo updates apply to following rounds."""
    messages = [message for message in _items(raw.get("messages")) if isinstance(message, dict)]
    results, by_id, by_content = defaultdict(list), defaultdict(list), defaultdict(list)
    for message in messages:
        if _string(message.get("role")) in {"tool", "function"} and message.get("tool_call_id") is not None:
            results[str(message["tool_call_id"])].append(message.get("content"))
        for block in _items(message.get("content")):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results[str(block.get("tool_use_id"))].append(block.get("content"))
    for action in tools:
        for identity in _call_ids(action):
            by_id[identity].append(action)
        by_content[_fingerprint(action.get("tool"), action.get("fullInput"), action.get("fullOutput"))].append(action)
    fingerprints = defaultdict(int)

    def fingerprint(identity, call):
        output = results.get(str(identity), [])
        if len(output) != 1:
            return None
        function = _object(call.get("function"))
        return _fingerprint(function.get("name") or call.get("name"),
                            function.get("arguments") if function else call.get("input"), output[0])

    for message in messages:
        for identity, call in _message_calls(message):
            value = fingerprint(identity, call)
            if value is not None:
                fingerprints[value] += 1
    matched, active = {}, None
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        calls = _message_calls(message)
        updates = []
        complete = bool(calls) and all(len(results.get(str(identity), [])) == 1 for identity, _ in calls)
        for identity, call in calls:
            candidates, method = by_id.get(str(identity), []), "tool_call_id"
            value = fingerprint(identity, call)
            if not candidates and value is not None and fingerprints[value] == 1:
                candidates, method = by_content.get(value, []), "tool_input_output_exact"
            actor = _string(message.get("agent_id")) or "master"
            candidates = [action for action in candidates if (action.get("actor") or "master") == actor]
            if len(candidates) != 1:
                continue
            action = candidates[0]
            matched[action["id"]] = {"turn": index, "path": f"messages[{index}].tool_calls", "matching": method,
                                     "tool_call_id": identity, "active": active if actor == "master" else None}
            if action.get("tool") == "todo_write" and action.get("status") not in {"error", "cancelled", "denied"}:
                output, inputs = _json_object(action.get("fullOutput")), _json_object(action.get("fullInput"))
                if output.get("ok") is True and isinstance(inputs.get("todos"), list):
                    current = [item for item in inputs["todos"] if isinstance(item, dict) and item.get("status") == "in_progress"]
                    updates.append({"record": current[0], "actionId": action["id"], "step": action["step"], "messageIndex": index} if len(current) == 1 else None)
        if complete and updates:
            active = updates[0] if len(updates) == 1 else None
    return matched


def build_execution_graph(raw, actions, extra):
    graph = _Graph()
    todos = [(kind, record, path) for kind, record, path in _plan_records(extra) if kind == "task"]
    request = next((action for action in actions if action.get("kind") == "message" and action.get("role") == "user"
                    and action.get("actor") in {None, "master"} and not _runtime_context(action.get("text"))
                    and _string(action.get("text"))), None)
    if request is None and not todos and not any(action.get("kind") in {"tool", "worker"} for action in actions):
        return graph.export()
    label = request.get("text") if request else raw.get("name") or raw.get("id") or "无"
    root = graph.node(_id("execution-root", raw.get("id") or "root"), str(label).splitlines()[0][:160], "root",
                      {"path": "session", "record": {"id": raw.get("id"), "name": raw.get("name"),
                                                       "request": request.get("data") if request else None,
                                                       "messages": raw.get("messages", [])},
                       "logActionIds": [action["id"] for action in actions]})
    children = defaultdict(set)

    def link(parent, child, relation, label, source):
        pending, seen = [child], set()
        while pending:
            node = pending.pop()
            if node == parent:
                graph.nodes[child]["source"].setdefault("unresolvedRelations", []).append({**source, "reason": "cyclic_relation"})
                return False
            if node not in seen:
                seen.add(node)
                pending.extend(children.get(node, ()))
        children[parent].add(child)
        graph.edge(parent, child, relation, label, source)
        return True

    tool_actions = [action for action in actions if action.get("kind") == "tool"]
    tool_nodes, tool_refs = {}, defaultdict(list)
    for action in tool_actions:
        node = graph.node(_id("execution-tool", action["id"]), action.get("tool") or "工具记录", "tool",
                          {"path": f"actions[{action['step']}]", "record": action.get("data", {}), "actor": action.get("actor")},
                          status=action.get("status"), action=action)
        tool_nodes[action["id"]] = node
        for value in {action.get("sourceId"), action["id"], *_call_ids(action),
                      _context(action).get("invocation_id"), _context(action).get("tool_call_id")}:
            if _string(value):
                tool_refs[value].append(node)
    task_refs, task_records = defaultdict(list), {}

    def task(record, path, kind="task"):
        node = graph.node(_id("execution-task", [record.get("id"), path]),
                          record.get("content") or record.get("hypothesis") or record.get("action") or record.get("id"), kind,
                          {"path": path, "record": record}, status=record.get("status"))
        identity = _string(record.get("id"))
        if identity and node not in task_refs[identity]:
            task_refs[identity].append(node)
        task_records[node] = record
        return node

    def unique(index, value):
        candidates = index.get(value, []) if isinstance(value, str) else []
        return candidates[0] if len(candidates) == 1 else None

    used_intents = set()
    for action in actions:
        if action.get("kind") not in {"tool", "worker"}:
            continue
        for source in [_context(action), *_sources(action)]:
            used_intents.update(value for field in ("task_id", "intent_id", "parent_task_id", "parent_id") if _string(value := source.get(field)))
    plans = [(task(record, path), record, path) for _, record, path in todos]
    for kind, record, path in _plan_records(extra):
        if kind == "intent" and record.get("id") in used_intents:
            plans.append((task(record, path), record, path))
    explicit_plan_relations = any(any(record.get(field) for field in ("parent_id", "parent_task_id", "depends_on", "dependencies")) for _, record, _ in plans)
    previous_plan = root
    for node, record, path in plans:
        parents = []
        for field in ("parent_id", "parent_task_id", "depends_on", "dependencies"):
            values = record.get(field)
            values = [values] if isinstance(values, str) else _items(values)
            for value in values:
                predecessor = unique(task_refs, value)
                if predecessor and link(predecessor, node, "task_parent" if field.startswith("parent") else "dependency",
                                        "记录的任务归属" if field.startswith("parent") else "记录的任务依赖",
                                        {"path": path, "field": field, "value": value}):
                    parents.append(predecessor)
        if not parents:
            if not explicit_plan_relations and previous_plan != root and path.startswith("metadata.extra.todos["):
                link(previous_plan, node, "plan_order", "记录的计划顺序", {"path": path, "field": "saved_list_order"})
            else:
                link(root, node, "task_parent", "已保存子任务", {"path": path})
        previous_plan = node

    workers, dispatch_refs, actor_workers = {}, defaultdict(list), defaultdict(list)
    for action in actions:
        if action.get("kind") != "worker":
            continue
        event, context = _sources(action)[1], _context(action)
        actor = _string(event.get("agent_id")) or _string(action.get("actor"))
        dispatch_id = _string(context.get("dispatch_id")) or _string(event.get("dispatch_id"))
        node = graph.node(_id("execution-worker", dispatch_id or action["id"]), event.get("task") or actor or "已记录子agent活动",
                          "worker_task", {"path": f"actions[{action['step']}]", "record": action.get("data", {})},
                          status=action.get("status"), action=action)
        workers[node] = (action, event, context)
        if actor:
            actor_workers[actor].append(node)
        if dispatch_id:
            dispatch_refs[dispatch_id].append(node)
    for action in tool_actions:
        if action.get("tool") not in {"task", "dispatch_sub_agent"}:
            continue
        output = _json_object(action.get("fullOutput"))
        actor = _string(output.get("agent_id"))
        if actor and output.get("status") in {"done", "completed", "running"} and not output.get("error") and action.get("status") not in {"error", "denied", "cancelled"}:
            node = unique(actor_workers, actor)
            if not actor_workers[actor]:
                inputs = _json_object(action.get("fullInput"))
                node = graph.node(_id("execution-worker-result", action["id"]), inputs.get("task") or inputs.get("description") or actor,
                                  "worker_task", {"path": f"actions[{action['step']}].fullOutput", "record": output}, status=output.get("status"))
                graph.nodes[node]["step"] = action.get("step")
                actor_workers[actor].append(node)
            if node:
                link(tool_nodes[action["id"]], node, "dispatch", "记录的子agent派发", {"path": f"actions[{action['step']}].fullOutput", "field": "agent_id", "value": actor})
    for node, (action, event, context) in workers.items():
        parent = None
        for field in ("parent_invocation_id", "parent_tool_call_id", "invocation_id", "tool_call_id"):
            parent = unique(tool_refs, context.get(field) or event.get(field))
            if parent:
                link(parent, node, "dispatch", "记录的子agent派发", {"path": f"actions[{action['step']}].data.data" + (".execution_context" if context.get(field) else ""), "field": field})
                break
        if not parent and not any(edge["target"] == node for edge in graph.edges.values()):
            link(root, node, "recorded_worker", "已记录子agent活动", {"path": f"actions[{action['step']}]"})

    legacy = _legacy_rounds(raw, tool_actions)
    batches, owner_sources = {}, {}

    def active_plan(record, path, *, create=False):
        if not isinstance(record, dict) or record.get("status") != "in_progress":
            return None
        node = unique(task_refs, record.get("id"))
        if node:
            return node
        content = _string(record.get("content"))
        matches = [node for node, saved in task_records.items() if content and saved.get("content") == content]
        if len(matches) == 1:
            return matches[0]
        if create and _string(record.get("id")) and content:
            node = task(record, path)
            link(root, node, "task_parent", "执行时记录的计划项", {"path": path})
            return node
        return None

    for action in tool_actions:
        node, context = tool_nodes[action["id"]], _context(action)
        event = _sources(action)[1]
        actor = context.get("actor") or action.get("actor")
        path = f"actions[{action['step']}]"
        owner, relation, info = None, "task_action", {"path": path}
        dispatch_id = _string(context.get("dispatch_id"))
        owner = unique(dispatch_refs, dispatch_id) if dispatch_id else unique(actor_workers, actor)
        unresolved_dispatch = bool(dispatch_id and not owner)
        if unresolved_dispatch:
            graph.nodes[node]["source"]["workerAssociation"] = "unresolved_dispatch"
            graph.nodes[node]["source"].setdefault("unresolvedRelations", []).append({
                "path": path + ".data.data.execution_context", "field": "dispatch_id", "value": dispatch_id,
                "reason": "missing_or_ambiguous_dispatch",
            })
        if owner:
            relation, info = "worker_action", {"path": path + ".data.data.execution_context" if context.get("dispatch_id") else path, "field": "dispatch_id" if context.get("dispatch_id") else "actor"}
        if not owner and not unresolved_dispatch:
            for source, source_path in [(context, path + ".data.data.execution_context"), (_sources(action)[0], path + ".data"), (event, path + ".data.data")]:
                for field in ("task_id", "intent_id", "parent_task_id", "parent_id"):
                    owner = unique(task_refs, source.get(field))
                    if owner:
                        relation, info = "task_action", {"path": source_path, "field": field, "value": source[field]}
                        break
                if owner:
                    break
        if not owner and not unresolved_dispatch and actor == "master" and context and len(_items(context.get("active_tasks"))) <= 1:
            owner = active_plan(context.get("active_task"), path + ".data.data.execution_context.active_task", create=True)
            if owner:
                relation, info = "active_plan", {"path": path + ".data.data.execution_context.active_task", "field": "active_task"}
        old = legacy.get(action["id"], {})
        if not owner and not context and actor in {None, "master"} and old.get("active"):
            saved = old["active"]
            owner = active_plan(saved["record"], f"actions[{saved['step']}].fullInput.todos")
            if owner:
                relation, info = "active_plan", {"path": f"actions[{saved['step']}].fullInput.todos", "field": "completed_previous_todo_write", "actionId": saved["actionId"], "appliesTo": old["path"]}
        if not owner:
            owner = root
            graph.nodes[node]["source"]["taskAssociation"] = "unassigned" if unresolved_dispatch else "unrecorded"
            if actor not in {None, "master"}:
                graph.nodes[node]["source"].setdefault("workerAssociation", "unrecorded")
        graph.nodes[node]["source"]["association"] = {"relation": relation, **info}
        explicit_parent = None
        parent_sources = [] if unresolved_dispatch else [(context, path + ".data.data.execution_context"), (event, path + ".data.data"), (_sources(action)[0], path + ".data")]
        for source, source_path in parent_sources:
            for field in ("parent_invocation_id", "parent_tool_call_id", "parent_id"):
                explicit_parent = unique(tool_refs, source.get(field))
                if explicit_parent and graph.nodes[owner]["kind"] == "worker_task" and any(
                    edge["source"] == explicit_parent and edge["target"] == owner and edge["relation"] == "dispatch"
                    for edge in graph.edges.values()
                ):
                    explicit_parent = None
                if explicit_parent:
                    if not link(explicit_parent, node, "execution_parent", "记录的动作父节点", {"path": source_path, "field": field, "value": source[field]}):
                        explicit_parent = None
                    break
            if explicit_parent:
                break
        if explicit_parent:
            continue
        turn = ("captured", context.get("run_id"), context.get("request_id"), actor, context["turn_id"]) if _string(context.get("turn_id")) else ("messages", old["turn"], actor) if "turn" in old else ("record", action["step"])
        key = (owner, turn)
        batches.setdefault(key, []).append(action)
        owner_sources.setdefault(key, (relation, info, old))
    previous_batches = {}
    for key, batch in batches.items():
        owner, turn = key
        relation, info, old = owner_sources[key]
        actor = _context(batch[0]).get("actor") or batch[0].get("actor")
        scope = (owner, actor)
        predecessors = previous_batches.get(scope, [])
        order_source = {"path": "actions", "field": "saved_record_order", "actor": actor}
        previous_turn = _context(batch[0]).get("previous_turn_id")
        if turn[0] == "captured" and _string(previous_turn):
            previous_key = (owner, (*turn[:-1], previous_turn))
            if previous_key in batches:
                predecessors = [tool_nodes[batches[previous_key][-1]["id"]]]
                order_source = {"path": f"actions[{batch[0]['step']}].data.data.execution_context", "field": "previous_turn_id", "value": previous_turn}
        for action in batch:
            node = tool_nodes[action["id"]]
            graph.nodes[node]["source"]["executionRound"] = {"key": list(turn), **({"matching": old["matching"], "path": old["path"]} if old else {})}
            if predecessors:
                for previous in predecessors:
                    link(previous, node, "record_order", "记录的执行顺序", {**order_source, "previousActionNode": previous, "sameRound": False})
            else:
                link(owner, node, relation, "执行时的活动计划项" if relation == "active_plan" else "记录的工具归属" if owner != root else "未记录任务归属的工具",
                     {**info, "sameRound": len(batch) > 1})
        # A round shares the preceding recorded tool as its display parent;
        # parallel siblings are never connected to one another as dependencies.
        previous_batches[scope] = [tool_nodes[batch[-1]["id"]]]
    return graph.export()
