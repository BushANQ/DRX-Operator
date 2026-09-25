import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatRecordedDuration, formatRecordedTime, toolRecordDetails } from './recordTiming.ts';

test('tool timing preserves epoch zero and measures only explicitly recorded endpoints', () => {
  const details = toolRecordDetails({ started_at: 0, completed_at: 1.25 });
  assert.equal(details.startedAt, 0);
  assert.equal(details.completedAt, 1.25);
  assert.equal(details.durationSeconds, 1.25);
  assert.notEqual(formatRecordedTime(details.startedAt), '无');
  assert.equal(formatRecordedDuration(0), '0 毫秒');
  assert.equal(formatRecordedDuration(0.0125), '12.5 毫秒');
});

test('missing timing is not inferred from legacy timestamps, output, or completed status', () => {
  const details = toolRecordDetails({ timestamp: 100, status: 'done', output: 'finished', data: { resultTimestamp: 110 } });
  assert.equal(details.startedAt, null);
  assert.equal(details.completedAt, null);
  assert.equal(details.durationSeconds, null);
  assert.equal(formatRecordedTime(null), '无');
  assert.equal(formatRecordedTime(Number.NaN), '无');
  assert.equal(formatRecordedTime(Number.POSITIVE_INFINITY), '无');
  assert.equal(formatRecordedDuration(null), '无');
});

test('nested recorded timing is supported and invalid present timestamps do not fall back to other clocks', () => {
  const details = toolRecordDetails({ data: { started_at: '0', completed_at: '2.5' } });
  assert.equal(details.durationSeconds, 2.5);
  for (const invalid of [true, '', 'bad-time', {}, [], Number.NaN, Number.POSITIVE_INFINITY, 1e100]) {
    const result = toolRecordDetails({ started_at: invalid, completed_at: 4, data: { started_at: 1 } });
    assert.equal(result.startedAt, null);
    assert.equal(result.durationSeconds, null);
  }
});

test('reversed or incomplete tool times remain visible without inventing a duration', () => {
  const reversed = toolRecordDetails({ started_at: 10, completed_at: 9 });
  assert.equal(reversed.startedAt, 10);
  assert.equal(reversed.completedAt, 9);
  assert.equal(reversed.durationSeconds, null);
  assert.equal(reversed.invalidTimeOrder, true);
  assert.equal(toolRecordDetails({ started_at: 0 }).durationSeconds, null);
  assert.equal(toolRecordDetails({ completed_at: 0 }).durationSeconds, null);
});

test('tool call, parent invocation and task fields come from explicit execution context', () => {
  const details = toolRecordDetails({ data: {
    tool_call_id: 'provider-call', execution_context: {
      task_id: 'intent-1', parent_invocation_id: 'invoke-parent', parent_tool_call_id: 'call-parent',
      active_task: { id: 'todo-2', content: '读取记录' },
    },
  } });
  assert.equal(details.toolCallId, 'provider-call');
  assert.equal(details.taskId, 'intent-1');
  assert.equal(details.activeTask, '读取记录（todo-2）');
  assert.equal(details.parentInvocationId, 'invoke-parent');
  assert.equal(details.parentToolCallId, 'call-parent');
});

test('active task lists never establish an unrecorded task assignment', () => {
  for (const active_tasks of [[{ id: 'one', content: '一项任务' }], [{ id: 'one' }, { id: 'two' }]]) {
    const details = toolRecordDetails({ data: { execution_context: { active_tasks } } });
    assert.equal(details.taskId, null);
    assert.equal(details.activeTask, null);
  }
  assert.equal(toolRecordDetails({ execution_context: { task_id: { guess: 'task' } } }).taskId, null);
});
