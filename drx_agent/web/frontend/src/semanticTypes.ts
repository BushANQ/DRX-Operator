import type { Edge, Node, Position, XYPosition } from '@xyflow/react';

export interface SemanticNode {
  id: string;
  label: string;
  kind: string;
  status: string | null;
  actionId: string | null;
  step: number | null;
  source: Record<string, unknown>;
}

export interface SemanticEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  label: string;
  sourceInfo: Record<string, unknown>;
}

export interface SemanticGraph {
  nodes: SemanticNode[];
  edges: SemanticEdge[];
}

export interface SemanticNodeData extends Record<string, unknown>, SemanticNode {
  title: string;
  subtitle: string;
  color: string;
  category: string;
  current: boolean;
  containsCurrent: boolean;
  collapsed: boolean;
  hiddenCount: number;
  canCollapse: boolean;
  sourceSide: Position;
  targetSide: Position;
  onOpen?: () => void;
  onToggle?: () => void;
}

export interface SemanticEdgeData extends Record<string, unknown> {
  relation: string;
  sourceInfo: Record<string, unknown>;
}

export type SemanticFlowNode = Node<SemanticNodeData, 'semanticNode'>;
export type SemanticFlowEdge = Edge<SemanticEdgeData>;
export type SemanticPositions = Record<string, XYPosition>;
export type SemanticMeasurements = Record<string, { width: number; height: number }>;

export interface SemanticProjection {
  nodes: SemanticFlowNode[];
  edges: SemanticFlowEdge[];
  currentNodeIds: string[];
  actualCurrentNodeIds: string[];
  nodeIdsByActionId: Map<string, string[]>;
  visibleIds: Set<string>;
}
