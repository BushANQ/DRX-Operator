import { MarkerType, Position } from '@xyflow/react';
import type { GraphResponse, NodeMeasurements, NodePositions, ReplayEdge, ReplayNode } from './types.ts';

export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 86;

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
  layout: 'vertical' | 'wrap' = 'vertical',
): ReplayProjection {
  const columns = layout === 'vertical' ? 1 : Math.max(1, Math.min(3, Math.floor((canvasWidth - 32) / 276)));
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
      position: positions[`${columns}:${node.id}`] ?? { x: 24 + column * 276, y: 24 + row * 148 },
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
  const currentNodeId = nodes.find((node) => node.data.current)?.id;
  const edges = graph.edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
    .map((edge): ReplayEdge => {
      const current = currentNodeId === edge.target;
      const color = current ? '#829fff' : edge.data?.color ?? '#455672';
      return { ...edge, type: 'default',
        style: { ...edge.style, stroke: color, strokeWidth: current ? 1.8 : 1.2 },
        markerEnd: { type: MarkerType.ArrowClosed, color, width: 12, height: 12 },
        animated: current, selectable: false, focusable: false,
      };
    });

  return { nodes, edges, columns };
}

export function isReplayShortcut(event: KeyboardEvent): boolean {
  return !event.defaultPrevented && !event.altKey && !event.metaKey && !event.ctrlKey
    && !(event.target instanceof Element
      && event.target.closest('input, textarea, button, select, summary, a, [contenteditable], [role="button"], .action-items-list, .detail-panel-body, .session-sidebar'));
}

export function overviewViewport(nodes: ReadonlyArray<{ position: { x: number; y: number }; width?: number; height?: number }> , width: number, height: number, inspectorOpen = false) {
  if (!nodes.length) return null;
  const left = inspectorOpen && width > 620 ? 350 : 30;
  const right = 66;
  const top = 70;
  const bottom = 112;
  const availableWidth = Math.max(1, width - left - right);
  const availableHeight = Math.max(1, height - top - bottom);
  const minX = Math.min(...nodes.map((node) => node.position.x));
  const minY = Math.min(...nodes.map((node) => node.position.y));
  const maxX = Math.max(...nodes.map((node) => node.position.x + (node.width ?? NODE_WIDTH)));
  const maxY = Math.max(...nodes.map((node) => node.position.y + (node.height ?? NODE_HEIGHT)));
  const zoom = Math.max(0.001, Math.min(1, availableWidth / (maxX - minX), availableHeight / (maxY - minY)) * 0.94);
  return {
    x: left + availableWidth / 2 - (minX + maxX) / 2 * zoom,
    y: top + availableHeight / 2 - (minY + maxY) / 2 * zoom,
    zoom,
  };
}
