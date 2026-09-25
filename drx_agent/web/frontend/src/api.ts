import type {
  GraphResponse, ReplayAction, ReplayEdge, ReplayNode, ReplayStage,
  SessionListItem, SessionSummary,
} from './types.ts';

interface RequestOptions {
  signal?: AbortSignal;
  fetcher?: typeof fetch;
}

export async function requestJSON(url: string, { signal, fetcher = fetch }: RequestOptions = {}): Promise<unknown> {
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

function invalid(field: string): never {
  throw new Error(`会话数据格式无效（${field}）`);
}

function record(value: unknown, field: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return invalid(field);
  return Object.fromEntries(Object.entries(value));
}

function list(value: unknown, field: string): unknown[] {
  if (!Array.isArray(value)) return invalid(field);
  return value;
}

function string(value: unknown, field: string): string {
  if (typeof value !== 'string') return invalid(field);
  return value;
}

function nullableString(value: unknown, field: string): string | null {
  return value === null ? null : string(value, field);
}

function number(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return invalid(field);
  return value;
}

function count(value: unknown, field: string): number {
  const result = number(value, field);
  if (!Number.isSafeInteger(result) || result < 0) return invalid(field);
  return result;
}

function nullableNumber(value: unknown, field: string): number | null {
  return value === null ? null : number(value, field);
}

export function parseSessionList(value: unknown): SessionListItem[] {
  const sessions = list(value, 'sessions').map((entry): SessionListItem => {
    const item = record(entry, 'session');
    const id = string(item.id, 'session.id');
    if (!id) return invalid('session.id');
    return {
      id,
      name: nullableString(item.name, 'session.name'),
      created_at: nullableNumber(item.created_at, 'session.created_at'),
      phase: nullableString(item.phase, 'session.phase'),
    };
  });
  if (new Set(sessions.map((item) => item.id)).size !== sessions.length) return invalid('session.id 重复');
  return sessions;
}

function parseAction(value: unknown): ReplayAction {
  const item = record(value, 'action');
  return {
    step: count(item.step, 'action.step'),
    id: string(item.id, 'action.id'),
    cardId: string(item.cardId, 'action.cardId'),
    sourceId: item.sourceId,
    originalIndex: count(item.originalIndex, 'action.originalIndex'),
    kind: string(item.kind, 'action.kind'),
    role: nullableString(item.role, 'action.role'),
    title: string(item.title, 'action.title'),
    category: string(item.category, 'action.category'),
    categoryColor: string(item.categoryColor, 'action.categoryColor'),
    actor: nullableString(item.actor, 'action.actor'),
    tool: nullableString(item.tool, 'action.tool'),
    input: nullableString(item.input, 'action.input'),
    output: nullableString(item.output, 'action.output'),
    fullInput: nullableString(item.fullInput, 'action.fullInput'),
    fullOutput: nullableString(item.fullOutput, 'action.fullOutput'),
    text: nullableString(item.text, 'action.text'),
    thought: nullableString(item.thought, 'action.thought'),
    timestamp: nullableNumber(item.timestamp, 'action.timestamp'),
    timeSeconds: nullableNumber(item.timeSeconds, 'action.timeSeconds'),
    timeOffset: nullableString(item.timeOffset, 'action.timeOffset'),
    status: nullableString(item.status, 'action.status'),
    stageKey: nullableString(item.stageKey, 'action.stageKey'),
    stageTitle: nullableString(item.stageTitle, 'action.stageTitle'),
    data: record(item.data, 'action.data'),
  };
}

function parseNode(value: unknown): ReplayNode {
  const item = record(value, 'node');
  const position = record(item.position, 'node.position');
  const data = record(item.data, 'node.data');
  if (item.type !== 'eventNode') return invalid('node.type');
  return {
    id: string(item.id, 'node.id'),
    type: 'eventNode',
    position: { x: number(position.x, 'node.position.x'), y: number(position.y, 'node.position.y') },
    data: {
      actionId: string(data.actionId, 'node.actionId'),
      step: count(data.step, 'node.step'),
      title: string(data.title, 'node.title'),
      subtitle: string(data.subtitle, 'node.subtitle'),
      status: nullableString(data.status, 'node.status'),
      color: string(data.color, 'node.color'),
    },
  };
}

function parseEdge(value: unknown): ReplayEdge {
  const item = record(value, 'edge');
  const data = record(item.data, 'edge.data');
  if (data.relation !== 'sequence') return invalid('edge.relation');
  return {
    id: string(item.id, 'edge.id'),
    source: string(item.source, 'edge.source'),
    target: string(item.target, 'edge.target'),
    type: string(item.type, 'edge.type'),
    data: {
      relation: 'sequence',
      ...(data.color === undefined ? {} : { color: string(data.color, 'edge.color') }),
    },
  };
}

function parseStage(value: unknown): ReplayStage {
  const item = record(value, 'stage');
  return {
    key: string(item.key, 'stage.key'),
    title: string(item.title, 'stage.title'),
    count: count(item.count, 'stage.count'),
    firstActionIndex: item.firstActionIndex === null ? null : count(item.firstActionIndex, 'stage.firstActionIndex'),
  };
}

function parseSummary(value: unknown, sessionId: string): SessionSummary {
  const item = record(value, 'summary');
  if (item.sessionId !== sessionId) throw new Error('会话数据归属无效');
  return {
    sessionId,
    name: nullableString(item.name, 'summary.name'),
    createdAt: nullableNumber(item.createdAt, 'summary.createdAt'),
    targetHost: nullableString(item.targetHost, 'summary.targetHost'),
    targetUrl: nullableString(item.targetUrl, 'summary.targetUrl'),
    targetNotes: nullableString(item.targetNotes, 'summary.targetNotes'),
    totalActions: count(item.totalActions, 'summary.totalActions'),
    totalStages: count(item.totalStages, 'summary.totalStages'),
    targetsCount: count(item.targetsCount, 'summary.targetsCount'),
    findingsCount: count(item.findingsCount, 'summary.findingsCount'),
    verifiedFindingsCount: count(item.verifiedFindingsCount, 'summary.verifiedFindingsCount'),
    credsCount: count(item.credsCount, 'summary.credsCount'),
    targets: list(item.targets, 'summary.targets').map((entry) => record(entry, 'target')),
    findings: list(item.findings, 'summary.findings').map((entry) => record(entry, 'finding')),
    creds: list(item.creds, 'summary.creds').map((entry) => record(entry, 'credential')),
  };
}

export function parseSessionGraph(value: unknown, sessionId: string): GraphResponse {
  const item = record(value, 'graph');
  const nodes = list(item.nodes, 'nodes').map(parseNode);
  const edges = list(item.edges, 'edges').map(parseEdge);
  const actions = list(item.actions, 'actions').map(parseAction);
  const timeline = list(item.timeline, 'timeline').map(parseAction);
  const stages = list(item.stages, 'stages').map(parseStage);
  const summary = parseSummary(item.summary, sessionId);
  if (summary.totalActions !== actions.length || summary.totalStages !== stages.length
    || summary.targetsCount !== summary.targets.length || summary.findingsCount !== summary.findings.length
    || summary.credsCount !== summary.creds.length || summary.verifiedFindingsCount > summary.findingsCount) {
    return invalid('summary 记录数量不一致');
  }
  const nodeIds = new Set(nodes.map((node) => node.id));
  if (nodeIds.size !== nodes.length || new Set(actions.map((action) => action.id)).size !== actions.length
    || new Set(edges.map((edge) => edge.id)).size !== edges.length
    || new Set(stages.map((stage) => stage.key)).size !== stages.length) return invalid('记录标识重复');
  if (nodes.length !== actions.length || timeline.length !== actions.length) return invalid('记录数量不一致');
  for (const [index, action] of actions.entries()) {
    const node = nodes[index];
    const event = timeline[index];
    if (action.step !== index || action.originalIndex !== index || !action.id || action.id !== action.cardId
      || !node || node.id !== action.cardId || node.data.actionId !== action.id || node.data.step !== index
      || !event || event.id !== action.id || event.step !== index) return invalid('记录索引不一致');
  }
  if (edges.some((edge) => !nodeIds.has(edge.source) || !nodeIds.has(edge.target))) return invalid('连线指向无效');
  for (const stage of stages) {
    const matches = actions.filter((action) => action.stageKey === stage.key);
    if (!stage.key || stage.count !== matches.length || stage.firstActionIndex !== (matches[0]?.step ?? null)) {
      return invalid('阶段记录不一致');
    }
  }
  if (actions.some((action) => action.stageKey !== null && !stages.some((stage) => stage.key === action.stageKey))) {
    return invalid('阶段归属无效');
  }
  return { nodes, edges, actions, timeline, stages, summary };
}
