import type { SemanticGraph } from './semanticTypes.ts';
import type { Edge, Node, Position, XYPosition } from '@xyflow/react';

export interface SessionListItem {
  id: string;
  name: string | null;
  created_at: number | null;
  phase: string | null;
}

export interface ReplayAction {
  step: number;
  id: string;
  cardId: string;
  sourceId: unknown;
  originalIndex: number;
  kind: string;
  role: string | null;
  title: string;
  category: string;
  categoryColor: string;
  actor: string | null;
  tool: string | null;
  input: string | null;
  output: string | null;
  fullInput: string | null;
  fullOutput: string | null;
  text: string | null;
  thought: string | null;
  timestamp: number | null;
  timeSeconds: number | null;
  timeOffset: string | null;
  status: string | null;
  stageKey: string | null;
  stageTitle: string | null;
  data: Record<string, unknown>;
}

export interface ReplayStage {
  key: string;
  title: string;
  count: number;
  firstActionIndex: number | null;
}

export interface SessionSummary {
  sessionId: string;
  name: string | null;
  createdAt: number | null;
  targetHost: string | null;
  targetUrl: string | null;
  targetNotes: string | null;
  totalActions: number;
  totalStages: number;
  targetsCount: number;
  findingsCount: number;
  verifiedFindingsCount: number;
  credsCount: number;
  targets: Record<string, unknown>[];
  findings: Record<string, unknown>[];
  creds: Record<string, unknown>[];
}

export interface EventNodeData extends Record<string, unknown> {
  actionId: string;
  step: number;
  title: string;
  subtitle: string;
  status: string | null;
  color: string;
  current?: boolean;
  kind?: string;
  category?: string;
  sourceSide?: Position;
  targetSide?: Position;
  onOpen?: () => void;
}

export type ReplayNode = Node<EventNodeData, 'eventNode'>;
export type ReplayEdge = Edge<{ relation: 'sequence'; color?: string }>;
export type NodePositions = Record<string, XYPosition>;
export type NodeMeasurements = Record<string, { width: number; height: number }>;

export interface GraphResponse {
  graphs: { execution: SemanticGraph; causal: SemanticGraph } | null;
  nodes: ReplayNode[];
  edges: ReplayEdge[];
  actions: ReplayAction[];
  timeline: ReplayAction[];
  stages: ReplayStage[];
  summary: SessionSummary;
}

export type RemoteStatus = 'idle' | 'loading' | 'ready' | 'error';

export type RemoteResult<T> =
  | { data: T; status: 'ready'; error: '' }
  | { data: null; status: 'idle' | 'loading'; error: '' }
  | { data: null; status: 'error'; error: string };
