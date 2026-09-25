import assert from 'node:assert/strict';
import { test } from 'node:test';
import { overviewViewport, NODE_WIDTH, NODE_HEIGHT } from './replay.ts';

test('overview fits every long-history node above playback controls and beside an open inspector', () => {
  for (const columns of [1, 3]) {
    const nodes = Array.from({ length: 100 }, (_, index) => ({
      position: { x: (index % columns) * 280, y: Math.floor(index / columns) * 150 },
      width: NODE_WIDTH, height: NODE_HEIGHT,
    }));
    const viewport = overviewViewport(nodes, 1000, 780, true);
    assert.ok(viewport);
    for (const node of nodes) {
      assert.ok(node.position.x * viewport.zoom + viewport.x >= 350);
      assert.ok((node.position.x + NODE_WIDTH) * viewport.zoom + viewport.x <= 830);
      assert.ok(node.position.y * viewport.zoom + viewport.y >= 70);
      assert.ok((node.position.y + NODE_HEIGHT) * viewport.zoom + viewport.y <= 668);
    }
  }
});

test('overview has no fabricated camera target for an empty graph', () => {
  assert.equal(overviewViewport([], 1000, 780), null);
});
