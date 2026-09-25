import assert from 'node:assert/strict';
import { test } from 'node:test';
import { expandAncestorsForAction, projectSemanticGraph } from './semanticLayout.ts';
import type { SemanticGraph, SemanticNode, SemanticEdge } from './semanticTypes.ts';

function node(id: string, step: number | null = null): SemanticNode {
  return { id, label: id, kind: step === null ? 'task' : 'tool', status: null,
    actionId: step === null ? null : `action-${step}`, step, source: { original: id } };
}

function edge(source: string, target: string, relation = 'executes'): SemanticEdge {
  return { id: `${source}-${target}-${relation}`, source, target, relation, label: relation, sourceInfo: { recorded: true } };
}

function tree(): SemanticGraph {
  return {
    nodes: [node('root'), node('left'), node('right'), node('left-action', 0), node('right-action', 1)],
    edges: [edge('root', 'left'), edge('root', 'right'), edge('left', 'left-action'), edge('right', 'right-action')],
  };
}

test('real relations create top-down branches without an array-order chain', () => {
  const graph = tree();
  graph.nodes.reverse();
  const projected = projectSemanticGraph(graph, 1);
  const positions = new Map(projected.nodes.map((item) => [item.id, item.position]));
  const root = positions.get('root');
  const left = positions.get('left');
  const right = positions.get('right');
  assert.ok(root && left && right);
  assert.ok(root.y < left.y && root.y < right.y);
  assert.equal(left.y, right.y);
  assert.notEqual(left.x, right.x);
  assert.deepEqual(projected.edges.map((item) => item.id), graph.edges.map((item) => item.id));
  assert.equal(projected.edges.some((item) => item.source === 'left' && item.target === 'right'), false);
});

test('snapshot nodes remain visible while future action nodes and their relations wait for replay', () => {
  const graph = tree();
  const early = projectSemanticGraph(graph, 0);
  assert.deepEqual(early.nodes.map((item) => item.id), ['root', 'left', 'right', 'left-action']);
  assert.equal(early.edges.length, 3);
  assert.deepEqual(early.currentNodeIds, ['left-action']);
  assert.deepEqual(early.nodeIdsByActionId.get('action-1'), ['right-action']);
  assert.equal(projectSemanticGraph(graph, 1).nodes.length, 5);
});

test('collapsing a branch hides its real descendants and counts only revealed hidden nodes', () => {
  const graph = tree();
  const projected = projectSemanticGraph(graph, 0, new Set(['root']));
  assert.deepEqual(projected.nodes.map((item) => item.id), ['root']);
  assert.equal(projected.nodes[0].data.hiddenCount, 3);
  assert.equal(projected.nodes[0].data.collapsed, true);
  assert.equal(projected.edges.length, 0);
});

test('shared children stay visible through another expanded parent path', () => {
  const graph: SemanticGraph = {
    nodes: [node('root'), node('left'), node('right'), node('shared', 0), node('exclusive', 1)],
    edges: [edge('root', 'left'), edge('root', 'right'), edge('left', 'shared'), edge('right', 'shared'), edge('left', 'exclusive')],
  };
  const projected = projectSemanticGraph(graph, 1, new Set(['left']));
  assert.equal(projected.visibleIds.has('shared'), true);
  assert.equal(projected.visibleIds.has('exclusive'), false);
  assert.equal(projected.edges.some((item) => item.source === 'right' && item.target === 'shared'), true);
  assert.equal(projected.edges.some((item) => item.source === 'left'), false);
  assert.equal(projected.nodes.find((item) => item.id === 'left')?.data.hiddenCount, 1);
});

test('explicit collapse remains honored and hidden current actions focus their closest visible ancestors', () => {
  const graph = tree();
  const collapsed = new Set(['left']);
  const projected = projectSemanticGraph(graph, 0, collapsed);
  assert.deepEqual(projected.actualCurrentNodeIds, ['left-action']);
  assert.deepEqual(projected.currentNodeIds, ['left']);
  assert.equal(projected.nodes.find((item) => item.id === 'left')?.data.containsCurrent, true);
  assert.deepEqual(projected.nodeIdsByActionId.get('action-0'), ['left-action']);
  assert.deepEqual([...collapsed], ['left']);
});

test('explicit ancestor expansion opens the selected action path without changing unrelated collapse state', () => {
  const graph = tree();
  const collapsed = new Set(['root', 'left', 'right']);
  const expanded = expandAncestorsForAction(graph, 'action-0', collapsed);
  assert.deepEqual([...expanded], ['right']);
  assert.deepEqual([...collapsed], ['root', 'left', 'right']);
  const projected = projectSemanticGraph(graph, 0, expanded);
  assert.deepEqual(projected.currentNodeIds, ['left-action']);
  assert.equal(projected.nodes.find((item) => item.id === 'left-action')?.data.containsCurrent, false);
});

test('parallel original relations and source records survive projection unchanged', () => {
  const graph = tree();
  graph.edges.push(edge('root', 'left', 'depends_on'));
  const original = structuredClone(graph);
  const projected = projectSemanticGraph(graph, 1);
  const relations = projected.edges.filter((item) => item.source === 'root' && item.target === 'left');
  assert.equal(relations.length, 2);
  assert.deepEqual(relations.map((item) => item.data?.relation), ['executes', 'depends_on']);
  assert.deepEqual(relations[1].data?.sourceInfo, { recorded: true });
  assert.deepEqual(projected.nodes.find((item) => item.id === 'root')?.data.source, { original: 'root' });
  assert.equal(projected.nodes.find((item) => item.id === 'root')?.data.status, null);
  assert.deepEqual(graph, original);
});

test('manual positions and measured node sizes survive graph projection', () => {
  const projected = projectSemanticGraph(tree(), 1, new Set(), { left: { x: 800, y: 320 } }, { left: { width: 300, height: 120 } });
  const left = projected.nodes.find((item) => item.id === 'left');
  assert.deepEqual(left?.position, { x: 800, y: 320 });
  assert.equal(left?.width, 300);
  assert.equal(left?.height, 120);
  assert.deepEqual(left?.measured, { width: 300, height: 120 });
  assert.equal(projected.nodes.find((item) => item.id === 'root')?.measured, undefined);
});

test('empty and disconnected snapshots gain no invented roots or edges', () => {
  const empty = projectSemanticGraph({ nodes: [], edges: [] }, 0);
  assert.deepEqual(empty.nodes, []);
  assert.deepEqual(empty.edges, []);
  assert.deepEqual(empty.currentNodeIds, []);
  const separate = projectSemanticGraph({ nodes: [node('one'), node('two')], edges: [] }, 0);
  assert.equal(separate.nodes.length, 2);
  assert.equal(separate.edges.length, 0);
});

test('recorded cycles remain visible and are not silently discarded by root detection', () => {
  const graph: SemanticGraph = { nodes: [node('a'), node('b'), node('leaf', 0)], edges: [edge('a', 'b'), edge('b', 'a'), edge('b', 'leaf')] };
  const projected = projectSemanticGraph(graph, 0);
  assert.equal(projected.nodes.length, 3);
  assert.equal(projected.edges.length, 3);
});

test('duplicate identities and unresolved relationship endpoints fail explicitly', () => {
  assert.throws(() => projectSemanticGraph({ nodes: [node('a'), node('a')], edges: [] }, 0), /重复/);
  assert.throws(() => projectSemanticGraph({ nodes: [node('a')], edges: [edge('a', 'missing')] }, 0), /指向无效/);
});
