export interface ToolRecordDetails {
  startedAt: number | null;
  completedAt: number | null;
  durationSeconds: number | null;
  invalidTimeOrder: boolean;
  toolCallId: string | null;
  taskId: string | null;
  activeTask: string | null;
  parentInvocationId: string | null;
  parentToolCallId: string | null;
}

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value)) : {};
}

function identifier(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value;
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : null;
}

function timestamp(value: unknown): number | null {
  let seconds: number;
  if (typeof value === 'number') seconds = value;
  else if (typeof value === 'string' && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value.trim())) {
    seconds = Number(value);
  } else return null;
  return Number.isFinite(seconds) && !Number.isNaN(new Date(seconds * 1000).getTime()) ? seconds : null;
}

export function toolRecordDetails(value: unknown): ToolRecordDetails {
  const record = object(value);
  const data = object(record.data);
  const context = object(record.execution_context ?? data.execution_context);
  const startedAt = timestamp(record.started_at ?? data.started_at);
  const completedAt = timestamp(record.completed_at ?? data.completed_at);
  const invalidTimeOrder = startedAt !== null && completedAt !== null && completedAt < startedAt;
  const durationSeconds = startedAt !== null && completedAt !== null && !invalidTimeOrder ? completedAt - startedAt : null;
  const activeTask = object(context.active_task);
  const taskLabel = identifier(activeTask.content) ?? identifier(activeTask.title) ?? identifier(activeTask.name);
  const activeTaskId = identifier(activeTask.id) ?? identifier(activeTask.task_id);
  return {
    startedAt, completedAt, durationSeconds, invalidTimeOrder,
    toolCallId: identifier(record.tool_call_id ?? data.tool_call_id ?? context.tool_call_id),
    taskId: identifier(context.task_id),
    activeTask: taskLabel && activeTaskId ? `${taskLabel}（${activeTaskId}）` : taskLabel ?? activeTaskId,
    parentInvocationId: identifier(context.parent_invocation_id),
    parentToolCallId: identifier(context.parent_tool_call_id),
  };
}

const timeFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3,
  hourCycle: 'h23', timeZoneName: 'shortOffset',
});

export function formatRecordedTime(value: number | null): string {
  return value === null || timestamp(value) === null ? '无' : timeFormat.format(new Date(value * 1000));
}

export function formatRecordedDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return '无';
  if (seconds < 1) return `${Number((seconds * 1000).toFixed(3))} 毫秒`;
  return `${Number(seconds.toFixed(3))} 秒`;
}
