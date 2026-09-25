import assert from 'node:assert/strict';
import { test } from 'node:test';
import { requestJSON } from './api.js';

test('missing and corrupt sessions cannot be treated as successful graph responses', async () => {
  for (const [status, message] of [[404, '会话不存在'], [422, '会话记录无法解析'], [500, '加载失败']]) {
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
      assert.equal(options.signal, controller.signal);
      return new Response('[]');
    },
  });
});
