import { useCallback, useEffect, useState } from 'react';
import { parseSessionGraph, parseSessionList, requestJSON } from './api.ts';
import type { GraphResponse, RemoteResult, SessionListItem } from './types.ts';

function useRemoteJSON<T>(url: string | null, retry: number, parse: (data: unknown) => T): RemoteResult<T> {
  const [result, setResult] = useState<(RemoteResult<T> & { key: string }) | null>(null);
  const key = `${url}:${retry}`;
  useEffect(() => {
    if (!url) return undefined;
    const controller = new AbortController();
    let active = true;
    requestJSON(url, { signal: controller.signal }).then((value) => {
      const data = parse(value);
      if (active) setResult({ key, data, status: 'ready', error: '' });
    }).catch((error: unknown) => {
      if (active) setResult({ key, data: null, status: 'error', error: error instanceof Error ? error.message : '加载失败' });
    });
    return () => { active = false; controller.abort(); };
  }, [url, retry, key, parse]);
  if (!url) return { data: null, status: 'idle', error: '' };
  return result?.key === key ? result : { data: null, status: 'loading', error: '' };
}

export function useSessionList(retry: number): RemoteResult<SessionListItem[]> {
  return useRemoteJSON('/api/sessions', retry, parseSessionList);
}

export function useSessionGraph(sessionId: string | null, retry: number): RemoteResult<GraphResponse> {
  const parse = useCallback((data: unknown) => {
    if (sessionId === null) throw new Error('无会话');
    return parseSessionGraph(data, sessionId);
  }, [sessionId]);
  return useRemoteJSON(sessionId === null ? null : `/api/sessions/${encodeURIComponent(sessionId)}`, retry, parse);
}
