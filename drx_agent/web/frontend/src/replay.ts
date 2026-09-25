import { Position } from '@xyflow/react';
import type { GraphResponse, NodeMeasurements, NodePositions, ReplayEdge, ReplayNode } from './types.ts';

export const NODE_WIDTH = 260;
export const NODE_HEIGHT = 132;

export interface ReplayProjection {
  nodes: ReplayNode[];
  edges: ReplayEdge[];
  columns: number;
}

// 连线仅表示保存记录的顺序。
export function projectReplay(
  graph: Pick<GraphResponse, 'nodes' | 'edges'>,
  currentStep: number,
  canvasWidth: number,
  positions: NodePositions = {},
  measurements: NodeMeasurements = {},
): ReplayProjection {
  const columns = Math.max(1, Math.min(3, Math.floor((canvasWidth - 32) / 304)));
  const visible = graph.nodes.filter((node) => node.data.step <= currentStep);
  const nodes = visible.map((node): ReplayNode => {
    const index = node.data.step;
    const row = Math.floor(index / columns);
    const offset = index % columns;
    const column = row % 2 ? columns - 1 - offset : offset;
    const sourceSide = offset === columns - 1 ? Position.Bottom : row % 2 ? Position.Left : Position.Right;
    const targetSide = offset === 0 ? Position.Top : row % 2 ? Position.Right : Position.Left;
    return {
      ...node,
      position: positions[`${columns}:${node.id}`] ?? { x: 24 + column * 304, y: 24 + row * 188 },
      style: { width: NODE_WIDTH, height: NODE_HEIGHT },
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      measured: measurements[node.id],
      sourcePosition: sourceSide,
      targetPosition: targetSide,
      connectable: false,
      data: { ...node.data, current: index === currentStep, sourceSide, targetSide },
    };
  });
  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
    .map((edge): ReplayEdge => ({
      ...edge,
      style: { ...edge.style, stroke: edge.data?.color ?? '#4897ad', strokeWidth: 1.5 },
      animated: false,
      selectable: false,
      focusable: false,
    }));
  return { nodes, edges, columns };
}

export function isReplayShortcut(event: KeyboardEvent): boolean {
  return !event.defaultPrevented && !event.altKey && !event.metaKey && !event.ctrlKey
    && !(event.target instanceof Element
      && event.target.closest('input, textarea, button, select, summary, a, [contenteditable], [role="button"]'));
}
