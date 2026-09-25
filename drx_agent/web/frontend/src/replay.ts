export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 86;

export function isReplayShortcut(event: KeyboardEvent): boolean {
  return !event.defaultPrevented && !event.altKey && !event.metaKey && !event.ctrlKey
    && !(event.target instanceof Element
      && event.target.closest('input, textarea, button, select, summary, a, [contenteditable], [role="button"], .action-items-list, .detail-panel-body, .session-sidebar'));
}

export function overviewViewport(nodes: ReadonlyArray<{ position: { x: number; y: number }; width?: number; height?: number }> , width: number, height: number, inspectorOpen = false) {
  if (!nodes.length) return null;
  const left = inspectorOpen && width > 620 ? 350 : 30;
  const right = width > 620 ? 170 : 66;
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
