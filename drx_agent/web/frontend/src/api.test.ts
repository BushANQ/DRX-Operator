import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseSessionGraph, parseSessionList, requestJSON } from './api.ts';
import type { GraphResponse, ReplayAction } from './types.ts';

test('missing and corrupt sessions cannot be treated as successful graph responses', async () => {
  const failures: [number, string][] = [[404, '会话不存在'], [422, '会话记录无法解析'], [500, '加载失败']];
  for (const [status, message] of failures) {
    await assert.rejects(requestJSON('/api/sessions/test', {
      fetcher: async () => new Response('{"detail":"error"}', { status }),
    }), new RegExp(message));
  }
});

test('valid empty lists stay empty; invalid JSON fails explicitly', async () => {
  assert.deepEqual(await requestJSON('/api/sessions', { fetcher: async () => new Response('[]') }), []);
  await assert.rejects(requestJSON('/api/sessions', {
    fetcher: async () => new Response('<html>error</html>'),
  }), /无效数据/);
});

test('passes cancellation through so an obsolete session request can be stopped', async () => {
  const controller = new AbortController();
  await requestJSON('/api/sessions', {
    signal: controller.signal,
    fetcher: async (_url, options) => {
      assert.equal(options?.signal, controller.signal);
      return new Response('[]');
    },
  });
});

function graphFixture(): GraphResponse {
  const action: ReplayAction = {
    step: 0, id: 'event-0', cardId: 'event-0', sourceId: null, originalIndex: 0,
    kind: 'tool', role: null, title: '工具记录', category: '工具', categoryColor: '#06b6d4',
    actor: null, tool: null, input: null, output: null, fullInput: null, fullOutput: null,
    text: null, thought: null, timestamp: 0, timeSeconds: 0, timeOffset: 'T+0s',
    status: null, stageKey: null, stageTitle: null, data: { kind: 'tool', timestamp: 0 },
  };
  return {
    nodes: [{
      id: 'event-0', type: 'eventNode', position: { x: 0, y: 0 },
      data: { actionId: 'event-0', step: 0, title: '工具记录', subtitle: 'tool', status: null, color: '#06b6d4' },
    }],
    edges: [], actions: [action], timeline: [action], stages: [], graphs: null,
    summary: {
      sessionId: 'test', name: null, createdAt: null, targetHost: null, targetUrl: null,
      targetNotes: null, totalActions: 1, totalStages: 0, targetsCount: 0,
      findingsCount: 0, verifiedFindingsCount: 0, credsCount: 0, targets: [], findings: [], creds: [],
    },
  };
}

test('session parsing retains null fields and rejects malformed and duplicate list records', () => {
  assert.deepEqual(parseSessionList([]), []);
  const item = { id: 'test', name: null, created_at: 0, phase: null };
  assert.deepEqual(parseSessionList([item]), [item]);
  for (const value of [null, {}, [null], [{ ...item, id: 12 }], [{ ...item, name: {} }],
    [{ ...item, created_at: Number.NaN }], [{ ...item, phase: [] }], [item, item]]) {
    assert.throws(() => parseSessionList(value), /格式无效/);
  }
});

test('graph parsing preserves unknown tool outcomes and zero-valued timestamps without invented evidence', () => {
  const graph = parseSessionGraph(graphFixture(), 'test');
  assert.equal(graph.summary.targetHost, null);
  assert.equal(graph.summary.targetsCount, 0);
  assert.deepEqual(graph.summary.findings, []);
  assert.equal(graph.actions[0].status, null);
  assert.equal(graph.actions[0].timestamp, 0);
  assert.equal(graph.actions[0].output, null);
});

test('source record identifiers retain their original JSON values while replay identities remain strings', () => {
  for (const sourceId of [42, { sequence: 42, provider: 'session' }]) {
    const fixture = graphFixture();
    fixture.actions[0].sourceId = sourceId;
    const graph = parseSessionGraph(fixture, 'test');
    assert.deepEqual(graph.actions[0].sourceId, sourceId);
    assert.deepEqual(graph.timeline[0].sourceId, sourceId);
    assert.equal(graph.actions[0].id, 'event-0');
    assert.equal(graph.nodes[0].id, 'event-0');
  }
});

test('graph parsing rejects malformed nested records and wrong session ownership', () => {
  const graph = graphFixture();
  assert.throws(() => parseSessionGraph(graph, 'other-session'), /归属无效/);
  for (const value of [
    { ...graph, actions: [null] },
    { ...graph, actions: [{ ...graph.actions[0], status: { result: 'done' } }] },
    { ...graph, actions: [{ ...graph.actions[0], data: [] }] },
    { ...graph, nodes: [{ ...graph.nodes[0], data: null }] },
    { ...graph, nodes: [{ ...graph.nodes[0], position: { x: '0', y: 0 } }] },
    { ...graph, summary: { ...graph.summary, findings: [null] } },
    { ...graph, summary: { ...graph.summary, totalActions: 2 } },
    { ...graph, summary: { ...graph.summary, verifiedFindingsCount: 1 } },
  ]) assert.throws(() => parseSessionGraph(value, 'test'), /格式无效/);
});

test('graph parsing rejects broken replay identity, connections, and stage mappings', () => {
  const graph = graphFixture();
  for (const value of [
    { ...graph, actions: [{ ...graph.actions[0], cardId: 'unknown' }] },
    { ...graph, timeline: [] },
    { ...graph, edges: [{ id: 'edge', source: 'event-0', target: 'unknown', type: 'smoothstep', data: { relation: 'sequence' } }] },
    { ...graph, actions: [{ ...graph.actions[0], stageKey: 'unrecorded' }] },
    { ...graph, stages: [{ key: 'stage', title: '阶段', count: 1, firstActionIndex: 0 }], summary: { ...graph.summary, totalStages: 1 } },
  ]) assert.throws(() => parseSessionGraph(value, 'test'), /格式无效/);
});
