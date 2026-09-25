import { Graph, graphlib, layout } from '@dagrejs/dagre';
import type { GraphLabel, NodeLabel, EdgeLabel } from '@dagrejs/dagre';
import { MarkerType, Position } from '@xyflow/react';
import type {
  SemanticGraph, SemanticMeasurements, SemanticPositions, SemanticProjection,
  SemanticFlowNode, SemanticFlowEdge, SemanticNode,
} from './semanticTypes.ts';

export const SEMANTIC_NODE_WIDTH = 220;
export const SEMANTIC_NODE_HEIGHT = 86;

function reachable(starts: Iterable<string>, adjacency: ReadonlyMap<string, string[]>, stops: ReadonlySet<string> = new Set()): Set<string> {
  const visited = new Set<string>();
  const pending = [...starts];
  while (pending.length) {
    const id = pending.pop();
    if (id === undefined || visited.has(id)) continue;
    visited.add(id);
    if (!stops.has(id)) pending.push(...(adjacency.get(id) ?? []));
  }
  return visited;
}

function dimensions(node: SemanticNode, measurements: SemanticMeasurements): { width: number; height: number } {
  const measured = measurements[node.id];
  const action = node.kind === 'action' || node.kind === 'tool';
  return {
    width: measured && Number.isFinite(measured.width) && measured.width > 0 ? measured.width : action ? 176 : SEMANTIC_NODE_WIDTH,
    height: measured && Number.isFinite(measured.height) && measured.height > 0 ? measured.height : action ? 72 : SEMANTIC_NODE_HEIGHT,
  };
}

function append(adjacency: Map<string, string[]>, source: string, target: string): void {
  const targets = adjacency.get(source);
  if (targets) targets.push(target);
  else adjacency.set(source, [target]);
}

export function relatedPath(graph: SemanticGraph, nodeId: string): Set<string> {
  if (!graph.nodes.some((node) => node.id === nodeId)) return new Set();
  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    append(incoming, edge.target, edge.source);
    append(outgoing, edge.source, edge.target);
  }
  return new Set([...reachable([nodeId], incoming), ...reachable([nodeId], outgoing)]);
}

export function expandAncestorsForAction(graph: SemanticGraph, actionId: string, collapsed: ReadonlySet<string>): Set<string> {
  const incoming = new Map<string, string[]>();
  for (const edge of graph.edges) append(incoming, edge.target, edge.source);
  const currentIds = graph.nodes.filter((node) => node.actionId === actionId).map((node) => node.id);
  const ancestors = reachable(currentIds, incoming);
  for (const id of currentIds) ancestors.delete(id);
  return new Set([...collapsed].filter((id) => !ancestors.has(id)));
}

function nearestVisibleAncestors(id: string, incoming: ReadonlyMap<string, string[]>, visibleIds: ReadonlySet<string>): string[] {
  let candidates = [id];
  const visited = new Set<string>();
  while (candidates.length) {
    const visible = candidates.filter((candidate) => visibleIds.has(candidate));
    if (visible.length) return visible;
    const parents = new Set<string>();
    for (const candidate of candidates) {
      visited.add(candidate);
      for (const parent of incoming.get(candidate) ?? []) if (!visited.has(parent)) parents.add(parent);
    }
    candidates = [...parents];
  }
  return [];
}

export function projectSemanticGraph(
  graph: SemanticGraph,
  currentStep: number,
  collapsed: ReadonlySet<string> = new Set(),
  positions: SemanticPositions = {},
  measurements: SemanticMeasurements = {},
  mode: 'snapshot' | 'replay' = 'replay',
): SemanticProjection {
  const knownIds = new Set(graph.nodes.map((node) => node.id));
  if (knownIds.size !== graph.nodes.length) throw new Error('执行图节点标识重复');
  if (new Set(graph.edges.map((edge) => edge.id)).size !== graph.edges.length) throw new Error('执行图关系标识重复');
  if (graph.edges.some((edge) => !knownIds.has(edge.source) || !knownIds.has(edge.target))) {
    throw new Error('执行图关系指向无效');
  }
  const eligible = graph.nodes.filter((node) => mode === 'snapshot'
    || (node.source.replayVisibility !== 'snapshot_only' && (node.step === null || node.step <= currentStep)));
  const eligibleIds = new Set(eligible.map((node) => node.id));
  const eligibleEdges = graph.edges.filter((edge) => eligibleIds.has(edge.source) && eligibleIds.has(edge.target));
  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  const topology = new Graph<GraphLabel, NodeLabel, EdgeLabel>({ directed: true, multigraph: true });
  topology.setGraph({ rankdir: 'TB' });
  for (const node of eligible) topology.setNode(node.id, dimensions(node, measurements));
  for (const edge of eligibleEdges) {
    append(outgoing, edge.source, edge.target);
    append(incoming, edge.target, edge.source);
    topology.setEdge(edge.source, edge.target, {}, edge.id);
  }

  const actualCurrentNodeIds = mode === 'snapshot' ? [] : eligible
    .filter((node) => node.actionId !== null && node.step !== null && node.step === currentStep)
    .map((node) => node.id);

  // 多父关系按展开路径判定可见；闭环的入口分量保留原节点。
  const components = graphlib.alg.tarjan(topology);
  const componentOf = new Map<string, number>();
  components.forEach((component, index) => component.forEach((id) => componentOf.set(id, index)));
  const componentsWithParents = new Set<number>();
  for (const edge of eligibleEdges) {
    const source = componentOf.get(edge.source);
    const target = componentOf.get(edge.target);
    if (source !== target && target !== undefined) componentsWithParents.add(target);
  }
  const roots = components.flatMap((component, index) => componentsWithParents.has(index) ? [] : component);
  const visibleIds = reachable(roots, outgoing, collapsed);
  const visible = eligible.filter((node) => visibleIds.has(node.id));
  const visibleEdges = eligibleEdges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target) && !collapsed.has(edge.source));
  const dag = new Graph<GraphLabel, NodeLabel, EdgeLabel>({ directed: true, multigraph: true });
  dag.setGraph({ rankdir: 'TB', nodesep: 38, ranksep: 76, edgesep: 18, marginx: 28, marginy: 28 });
  for (const node of visible) dag.setNode(node.id, dimensions(node, measurements));
  for (const edge of visibleEdges) {
    const showLabel = ['evidence_for', 'depends_on', 'dependency'].includes(edge.relation);
    const taskTrunk = ['decomposition', 'task_parent', 'dependency', 'plan_order'].includes(edge.relation);
    dag.setEdge(edge.source, edge.target, { minlen: 1, weight: taskTrunk ? 4 : 1, width: showLabel ? 128 : 0, height: showLabel ? 18 : 0, labelpos: 'c' }, edge.id);
  }
  if (visible.length) layout(dag);
  const currentSet = new Set(actualCurrentNodeIds.flatMap((id) => nearestVisibleAncestors(id, incoming, visibleIds)));
  const actualCurrentSet = new Set(actualCurrentNodeIds);
  const nodeIdsByActionId = new Map<string, string[]>();
  for (const node of graph.nodes) if (node.actionId !== null) append(nodeIdsByActionId, node.actionId, node.id);
  const nodes = visible.map((node): SemanticFlowNode => {
    const measured = dimensions(node, measurements);
    const point = dag.node(node.id);
    if (!point || typeof point.x !== 'number' || typeof point.y !== 'number') throw new Error('执行图布局失败');
    const descendants = collapsed.has(node.id) ? reachable(outgoing.get(node.id) ?? [], outgoing) : new Set<string>();
    descendants.delete(node.id);
    const hiddenCount = [...descendants].filter((id) => !visibleIds.has(id)).length;
    return {
      id: node.id, type: 'semanticNode',
      position: positions[node.id] ?? { x: point.x - measured.width / 2, y: point.y - measured.height / 2 },
      width: measured.width, height: measured.height,
      style: { width: measured.width, height: measured.height },
      measured: measurements[node.id],
      sourcePosition: Position.Bottom, targetPosition: Position.Top,
      connectable: false,
      data: {
        ...node, title: node.label, subtitle: node.kind, category: node.kind, color: '#829fff',
        current: currentSet.has(node.id), containsCurrent: currentSet.has(node.id) && !actualCurrentSet.has(node.id),
        collapsed: collapsed.has(node.id), hiddenCount,
        canCollapse: (outgoing.get(node.id)?.length ?? 0) > 0,
        sourceSide: Position.Bottom, targetSide: Position.Top,
      },
    };
  });
  const edges = visibleEdges.map((edge): SemanticFlowEdge => {
    const current = currentSet.has(edge.target);
    const color = current ? '#829fff' : '#586880';
    const route = positions[edge.source] || positions[edge.target] ? undefined : dag.edge(edge.source, edge.target, edge.id);
    return {
      id: edge.id, source: edge.source, target: edge.target, type: 'semanticEdge', label: ['evidence_for', 'depends_on', 'dependency'].includes(edge.relation) ? edge.label : undefined,
      data: { relation: edge.relation, sourceInfo: edge.sourceInfo, routePoints: route?.points, labelPoint: route && typeof route.x === 'number' && typeof route.y === 'number' ? { x: route.x, y: route.y } : undefined },
      style: { stroke: color, strokeWidth: current ? 1.8 : 1.3, strokeDasharray: ['record_group', 'plan_order', 'record_order'].includes(edge.relation) ? '4 3' : undefined },
      labelStyle: { fill: '#b4c4dc', fontSize: 10 },
      labelBgStyle: { fill: '#0b1120', fillOpacity: 0.95 },
      markerEnd: { type: MarkerType.ArrowClosed, color, width: 12, height: 12 },
      animated: current, selectable: false, focusable: false,
    };
  });
  return { nodes, edges, currentNodeIds: [...currentSet], actualCurrentNodeIds, nodeIdsByActionId, visibleIds };
}
