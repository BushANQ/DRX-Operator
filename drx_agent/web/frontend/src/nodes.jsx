import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';

const display = (value) => value == null || value === '' ? '无' : String(value);

// React Flow consumes these components through the stable nodeTypes registry below.
// oxlint-disable-next-line react/only-export-components
const EventNode = memo(({ data, selected }) => {
  const isCurrent = data.current ?? data._current ?? false;
  const isPending = data.pending ?? data._pending ?? false;
  const isSelected = selected || data.selected;
  const color = data.color || '#38bdf8';
  const title = display(data.title);

  return (
    <div
      className={`rf-event-card ${isCurrent ? 'current' : ''} ${isPending ? 'pending' : ''} ${isSelected ? 'selected' : ''}`}
      style={{ '--card-color': color, width: 260, minHeight: 112 }}
    >
      <Handle type="target" position={data.targetSide || Position.Top} style={{ background: color }} />
      <div className="rf-card-badge-row">
        <span className="rf-badge event-badge">{Number.isInteger(data.step) ? `记录 ${data.step + 1}` : '记录'}</span>
        <span className="rf-event-status">状态：{display(data.status)}</span>
        {isCurrent && <span className="rf-pulse-ring" aria-label="当前回放记录" />}
      </div>
      <div className="rf-card-title" title={title}>{title}</div>
      <div className="rf-card-subtitle" title={display(data.subtitle)}>
        <span>{display(data.subtitle)}</span>
        <button className="rf-node-open nodrag" type="button" aria-label={`查看记录 ${data.step + 1} 详情`}
          onClick={(event) => { event.stopPropagation(); data.onOpen?.(); }}>详情</button>
      </div>
      <Handle type="source" position={data.sourceSide || Position.Bottom} style={{ background: color }} />
    </div>
  );
});
EventNode.displayName = 'EventNode';

export const nodeTypes = { eventNode: EventNode };
