import { memo, type CSSProperties } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ArrowSquareOut, ChatCircle, GitBranch, ShieldCheck, WarningCircle, Wrench } from '@phosphor-icons/react';
import { statusPresentation } from './presentation.ts';
import type { ReplayNode } from './types.ts';

// oxlint-disable-next-line react/only-export-components
const EventNode = memo(({ data, selected }: NodeProps<ReplayNode>) => {
  const status = statusPresentation(data.status);
  const Icon = data.kind === 'tool' ? Wrench : data.kind === 'worker' ? GitBranch : data.kind === 'approval' ? ShieldCheck : data.kind === 'error' ? WarningCircle : ChatCircle;
  const title = data.title || '无';
  return (
    <div className={`rf-event-card ${data.current ? 'current' : ''} ${selected ? 'selected' : ''} status-${status.tone}`}
      style={{ '--card-color': data.color, '--status-color': status.color } as CSSProperties}>
      <Handle isConnectable={false} type="target" position={data.targetSide ?? Position.Top} />
      <div className="node-category"><Icon size={10} weight="bold" /><span>{data.category || data.subtitle}</span></div>
      <div className="rf-card-title" title={title}>{title}</div>
      <div className="node-footer"><span className="node-status" title={data.status ?? '无'}>{status.label}</span><span className="node-order">#{data.step + 1}</span><button className="node-open nodrag" aria-label={`查看记录 ${data.step + 1} 详情`} onClick={(event) => { event.stopPropagation(); data.onOpen?.(); }}><ArrowSquareOut size={13} /></button></div>
      <Handle isConnectable={false} type="source" position={data.sourceSide ?? Position.Bottom} />
    </div>
  );
});
EventNode.displayName = 'EventNode';
export const nodeTypes = { eventNode: EventNode };
