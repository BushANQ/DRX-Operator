export const NODE_WIDTH = 260;
export const NODE_HEIGHT = 132;

// Edges describe saved record order, never a proven attack or causal path.
export function projectReplay(graph, currentStep, canvasWidth, positions = {}) {
  const columns = Math.max(1, Math.min(3, Math.floor((canvasWidth - 32) / 304)));
  const visible = graph.nodes.filter((node) => node.data.step <= currentStep);
  const nodes = visible.map((node) => {
    const index = node.data.step;
    const row = Math.floor(index / columns);
    const offset = index % columns;
    const column = row % 2 ? columns - 1 - offset : offset;
    return {
      ...node,
      position: positions[`${columns}:${node.id}`] ?? { x: 24 + column * 304, y: 24 + row * 188 },
      style: { width: NODE_WIDTH, height: NODE_HEIGHT },
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      sourcePosition: offset === columns - 1 ? 'bottom' : row % 2 ? 'left' : 'right',
      targetPosition: offset === 0 ? 'top' : row % 2 ? 'right' : 'left',
      connectable: false,
      data: {
        ...node.data,
        current: index === currentStep,
        sourceSide: offset === columns - 1 ? 'bottom' : row % 2 ? 'left' : 'right',
        targetSide: offset === 0 ? 'top' : row % 2 ? 'right' : 'left',
      },
    };
  });
  const visibleIds = new Set(nodes.map((node) => node.id));
  const edges = graph.edges.filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target))
    .map((edge) => ({
      ...edge,
      style: { ...edge.style, stroke: edge.data?.color ?? '#4897ad', strokeWidth: 1.5 },
      animated: false,
      selectable: false,
      focusable: false,
    }));
  return { nodes, edges, columns };
}

export function isReplayShortcut(event) {
  return !event.defaultPrevented && !event.altKey && !event.metaKey && !event.ctrlKey
    && !event.target.closest?.('input, textarea, button, select, summary, a, [contenteditable], [role="button"]');
}
