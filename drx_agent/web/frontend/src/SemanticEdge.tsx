import { BaseEdge, getBezierPath, type EdgeProps } from '@xyflow/react';
import type { SemanticFlowEdge } from './semanticTypes.ts';

export default function SemanticEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition,
  markerEnd, style, label, labelStyle, labelBgStyle, data }: EdgeProps<SemanticFlowEdge>) {
  const fallback = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const recordedRoute = data?.routePoints;
  let path = fallback[0];
  let labelX = fallback[1];
  let labelY = fallback[2];
  if (recordedRoute && recordedRoute.length >= 2) {
    const points = [{ x: sourceX, y: sourceY }, ...recordedRoute.slice(1, -1), { x: targetX, y: targetY }];
    path = `M ${sourceX} ${sourceY}`;
    for (let index = 1; index < points.length - 1; index += 1) {
      const before = points[index - 1];
      const point = points[index];
      const after = points[index + 1];
      const firstLength = Math.hypot(point.x - before.x, point.y - before.y);
      const nextLength = Math.hypot(after.x - point.x, after.y - point.y);
      const radius = Math.min(12, firstLength / 2, nextLength / 2);
      if (!radius) { path += ` L ${point.x} ${point.y}`; continue; }
      const entry = { x: point.x - (point.x - before.x) * radius / firstLength, y: point.y - (point.y - before.y) * radius / firstLength };
      const exit = { x: point.x + (after.x - point.x) * radius / nextLength, y: point.y + (after.y - point.y) * radius / nextLength };
      path += ` L ${entry.x} ${entry.y} Q ${point.x} ${point.y} ${exit.x} ${exit.y}`;
    }
    path += ` L ${targetX} ${targetY}`;
    const center = data?.labelPoint ?? points[Math.floor(points.length / 2)];
    labelX = center.x;
    labelY = center.y;
  }
  return <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} label={label}
    labelX={labelX} labelY={labelY} labelStyle={labelStyle} labelBgStyle={labelBgStyle} interactionWidth={16} />;
}
