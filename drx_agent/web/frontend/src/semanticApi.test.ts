import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSemanticGraph } from './api.ts';

const root = { id: 'root', label: '记录的任务', kind: 'task', status: null, actionId: null, step: null, source: { path: 'metadata.extra.todos[0]', record: { content: '记录的任务' } } };
const first = { ...root, id: 'first', kind: 'hypothesis' };
const second = { ...root, id: 'second', kind: 'evidence' };
const edge = { id: 'edge-a', source: 'root', target: 'first', relation: 'task_parent', label: '任务关系', sourceInfo: { field: 'parent_id', value: 'root' } };

test('semantic graphs retain real branches and unknown outcomes without adding sequence edges', () => {
  const graph = parseSemanticGraph({ nodes: [root, first, second], edges: [edge, { ...edge, id: 'edge-b', target: 'second' }] }, []);
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.length, 2);
  assert.ok(graph.edges.every((item) => item.source === 'root'));
  assert.equal(graph.nodes[0].status, null);
  assert.deepEqual(graph.edges[0].sourceInfo, edge.sourceInfo);
  assert.deepEqual(parseSemanticGraph({ nodes: [], edges: [] }, []), { nodes: [], edges: [] });
});

test('semantic graph parsing rejects missing endpoints and nonexistent action associations', () => {
  assert.throws(() => parseSemanticGraph({ nodes: [root], edges: [edge] }, []), /语义图连线/);
  assert.throws(() => parseSemanticGraph({ nodes: [root, root], edges: [] }, []), /标识重复/);
  assert.throws(() => parseSemanticGraph({ nodes: [{ ...root, actionId: 'not-recorded', step: 0 }], edges: [] }, []), /语义节点/);
});
