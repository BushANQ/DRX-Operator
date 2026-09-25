import assert from 'node:assert/strict';
import { test } from 'node:test';
import { eventPresentation, statusPresentation } from './presentation.ts';

test('missing and unknown statuses do not acquire a successful outcome', () => {
  assert.equal(statusPresentation(null).label, '无');
  assert.equal(statusPresentation('waiting_for_operator').label, 'waiting_for_operator');
  assert.equal(statusPresentation('denied').tone, 'error');
  assert.equal(statusPresentation('running').label, '进行中');
});

test('event styling retains unknown event labels and source colors', () => {
  assert.deepEqual(eventPresentation({ kind: 'custom', role: null, category: '原始类型', categoryColor: '#123456' }), {
    label: '原始类型', color: '#123456',
  });
});
