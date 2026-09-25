import { useEffect, useState } from 'react';
import { requestJSON } from './api.js';

export function useRemoteJSON(url, retry, sessionId) {
  const [result, setResult] = useState(null);
  const key = `${url}:${retry}`;
  useEffect(() => {
    if (!url) return undefined;
    const controller = new AbortController();
    let active = true;
    requestJSON(url, { signal: controller.signal }).then((data) => {
      if (sessionId === undefined) {
        if (!Array.isArray(data) || data.some((entry) => typeof entry?.id !== 'string')) {
          throw new Error('会话列表格式无效');
        }
      } else if (!data || !Array.isArray(data.nodes) || !Array.isArray(data.edges)
        || !Array.isArray(data.actions) || !Array.isArray(data.stages)
        || data.summary?.sessionId !== sessionId) {
        throw new Error('会话数据格式或归属无效');
      }
      if (active) setResult({ key, data, status: 'ready', error: '' });
    }).catch((error) => {
      if (active) setResult({ key, data: null, status: 'error', error: error.message });
    });
    return () => { active = false; controller.abort(); };
  }, [url, retry, sessionId, key]);
  if (!url) return { data: null, status: 'idle', error: '' };
  return result?.key === key ? result : { data: null, status: 'loading', error: '' };
}
