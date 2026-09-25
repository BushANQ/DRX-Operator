export async function requestJSON(url, { signal, fetcher = fetch } = {}) {
  const response = await fetcher(url, { signal });
  if (!response.ok) {
    if (response.status === 404) throw new Error('会话不存在');
    if (response.status === 422) throw new Error('会话记录无法解析');
    throw new Error(`加载失败（HTTP ${response.status}）`);
  }
  try {
    return await response.json();
  } catch {
    throw new Error('服务器返回了无效数据');
  }
}
