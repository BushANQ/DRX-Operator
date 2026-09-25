import type { ReplayAction } from './types.ts';

export function eventPresentation(action: Pick<ReplayAction, 'kind' | 'role' | 'category' | 'categoryColor'>) {
  const colors: Record<string, string> = { tool: '#e9b45d', approval: '#dfaa55', worker: '#ae8ae8', error: '#ef7187' };
  return {
    label: action.category || action.kind,
    color: action.kind === 'message' ? action.role === 'assistant' ? '#ad8bed' : '#739cf4' : colors[action.kind] ?? action.categoryColor,
  };
}

export function statusPresentation(status: string | null | undefined): { label: string; tone: string; color: string } {
  switch (status) {
    case 'done': case 'completed': return { label: '已完成', tone: 'complete', color: '#3bab8e' };
    case 'error': case 'failed': return { label: '失败', tone: 'error', color: '#d56680' };
    case 'running': case 'in_progress': return { label: '进行中', tone: 'running', color: '#779df6' };
    case 'pending': return { label: '等待中', tone: 'pending', color: '#8999b0' };
    case 'denied': return { label: '已拒绝', tone: 'error', color: '#d56680' };
    case 'cancelled': case 'canceled': return { label: '已取消', tone: 'unknown', color: '#7b8ba4' };
    default: return { label: status || '无', tone: 'unknown', color: '#647791' };
  }
}
