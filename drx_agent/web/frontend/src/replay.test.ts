import assert from 'node:assert/strict';
import { test } from 'node:test';
import { projectReplay } from './replay.ts';
import type { GraphResponse } from './types.ts';

const graph: Pick<GraphResponse, 'nodes' | 'edges'> = {
  nodes: Array.from({ length: 100 }, (_, step) => ({
    id: `e${step}`, type: 'eventNode',
    data: { step, actionId: `e${step}`, title: '记录', subtitle: 'event', status: null, color: '#4897ad' },
    position: { x: 0, y: 0 },
  })),
  edges: Array.from({ length: 99 }, (_, i) => ({ id: `s${i}`, source: `e${i}`, target: `e${i + 1}` })),
};

test('replay reveals actual records and removes future nodes when rewinding', () => {
  assert.equal(projectReplay(graph, 99, 1000).nodes.length, 100);
  const early = projectReplay(graph, 2, 1000);
  assert.equal(early.nodes.length, 3);
  assert.equal(early.edges.length, 2);
  assert.deepEqual(early.nodes.filter((node) => node.data.current).map((node) => node.id), ['e2']);
  assert.equal(graph.nodes.length, 100);
});

test('narrow layouts stay within one readable column and preserve manual positions', () => {
  const narrow = projectReplay(graph, 3, 360);
  assert.equal(narrow.columns, 1);
  assert.ok(narrow.nodes.every((node) => node.position.x === 24));
  const custom = projectReplay(graph, 3, 360, { '1:e2': { x: 40, y: 70 } });
  assert.deepEqual(custom.nodes[2].position, { x: 40, y: 70 });
  assert.equal(projectReplay(graph, 3, 1000, { '1:e2': { x: 40, y: 70 } }).columns, 3);
});

test('rewind and forward recompute edge colors from source data', () => {
  const late = projectReplay(graph, 4, 900);
  assert.ok(late.edges[0].style);
  late.edges[0].style.stroke = '#1e293b';
  assert.equal(projectReplay(graph, 4, 900).edges[0].style?.stroke, '#4897ad');
});

test('actual node measurements survive replay without inventing measurements for unseen nodes', () => {
  const measurements = { e0: { width: 260, height: 132 } };
  const projected = projectReplay(graph, 3, 900, {}, measurements);
  assert.deepEqual(projected.nodes[0].measured, measurements.e0);
  assert.equal(projected.nodes[1].measured, undefined);
  assert.equal(projected.nodes[1].width, 260);
});
