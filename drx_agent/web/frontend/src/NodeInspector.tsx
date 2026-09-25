import { DotsSixVertical, X } from '@phosphor-icons/react';
import { useCallback, useEffect, useRef, useState, type PointerEvent } from 'react';
import RecordDetails from './RecordDetails.tsx';
import SemanticDetails from './SemanticDetails.tsx';
import type { SemanticNode, SemanticEdge } from './semanticTypes.ts';
import type { ReplayAction } from './types.ts';

interface InspectorProps { action: ReplayAction | null; node?: SemanticNode | null; relations?: SemanticEdge[]; sessionId: string; onClose: () => void }

export default function NodeInspector({ action, node, relations, sessionId, onClose }: InspectorProps) {
  const element = useRef<HTMLElement>(null);
  const drag = useRef<{ startX: number; startY: number; x: number; y: number } | null>(null);
  const [position, setPosition] = useState({ x: 16, y: 64 });
  const clamp = useCallback((x: number, y: number) => {
    const panel = element.current;
    const parent = panel?.parentElement;
    if (!panel || !parent) return { x, y };
    return { x: Math.max(12, Math.min(x, parent.clientWidth - panel.offsetWidth - 12)),
      y: Math.max(12, Math.min(y, parent.clientHeight - panel.offsetHeight - 100)) };
  }, []);
  useEffect(() => {
    const parent = element.current?.parentElement;
    if (!parent) return undefined;
    const observer = new ResizeObserver(() => setPosition((old) => {
      const next = clamp(old.x, old.y);
      return next.x === old.x && next.y === old.y ? old : next;
    }));
    observer.observe(parent);
    if (element.current) observer.observe(element.current);
    return () => observer.disconnect();
  }, [clamp]);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose]);
  const startDrag = (event: PointerEvent<HTMLElement>) => {
    if (event.target instanceof Element && event.target.closest('button')) return;
    drag.current = { startX: event.clientX, startY: event.clientY, ...position };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };
  return (
    <section ref={element} className="node-inspector" aria-label="节点详情" style={{ left: position.x, top: position.y }}>
      <header className="inspector-header" onPointerDown={startDrag} onPointerMove={(event) => {
        const active = drag.current;
        if (active) setPosition(clamp(active.x + event.clientX - active.startX, active.y + event.clientY - active.startY));
      }} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
        <DotsSixVertical size={16} className="inspector-grip" /><span>节点详情</span><span className="inspector-index">{action ? `#${String(action.step + 1).padStart(2, '0')}` : '实体'}</span>
        <button className="icon-button" aria-label="关闭节点详情" onClick={onClose}><X size={16} /></button>
      </header>
      <>{node ? <SemanticDetails node={node} action={action} sessionId={sessionId} relations={relations} /> : <RecordDetails action={action} sessionId={sessionId} />}</>
    </section>
  );
}
