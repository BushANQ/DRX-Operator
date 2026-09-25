import { memo } from 'react';
import { Handle, Position } from '@xyflow/react';

/* ----------------------------------------------------------------
   SessionCardNode — Root session entry card
   ---------------------------------------------------------------- */
export const SessionCardNode = memo(({ data }) => {
  const isCurrent = data._current;
  const isPending = data._pending;

  return (
    <div
      className={`rf-session-card ${isCurrent ? 'active' : ''} ${isPending ? 'pending' : ''}`}
      style={{ '--card-color': data.color || '#38bdf8' }}
    >
      <div className="rf-card-badge-row">
        <span className="rf-badge session-badge">{data.badge || 'SESSION'}</span>
        <span className="rf-badge-status-dot" />
      </div>
      <div className="rf-card-title">{data.label || '研判会话'}</div>
      <div className="rf-card-target" title={data.target}>
        {data.target || '无'}
      </div>
      {data.sub && <div className="rf-card-subtitle">{data.sub}</div>}

      <Handle
        type="source"
        position={Position.Right}
        style={{
          background: '#38bdf8',
          width: 8,
          height: 8,
          border: '2px solid #080c14',
          right: -5,
        }}
      />
    </div>
  );
});
SessionCardNode.displayName = 'SessionCardNode';


/* ----------------------------------------------------------------
   PhaseGroupNode — Container wrapper with title header
   ---------------------------------------------------------------- */
export const PhaseGroupNode = memo(({ data }) => {
  return (
    <div
      className="rf-phase-group"
      style={{
        '--phase-color': data.color || '#06b6d4',
      }}
    >
      <div className="rf-phase-group-header">
        <span className="rf-phase-dot" />
        <span className="rf-phase-title">{data.title}</span>
      </div>
    </div>
  );
});
PhaseGroupNode.displayName = 'PhaseGroupNode';


/* ----------------------------------------------------------------
   ProcessCardNode — Concise card with badge, title, subtitle
   Lights up with replay, grays out if pending
   ---------------------------------------------------------------- */
export const ProcessCardNode = memo(({ data }) => {
  const isCurrent = data._current;
  const isLit = data._lit;
  const isPending = data._pending;

  return (
    <div
      className={`rf-process-card ${isCurrent ? 'current' : ''} ${isLit ? 'lit' : ''} ${
        isPending ? 'pending' : ''
      }`}
      style={{
        '--card-color': data.color || '#06b6d4',
      }}
    >
      <Handle
        type="target"
        position={Position.Left}
        style={{
          background: isLit ? data.color : '#334155',
          width: 8,
          height: 8,
          border: '2px solid #080c14',
          left: -5,
        }}
      />

      <div className="rf-card-badge-row">
        <span
          className="rf-badge process-badge"
          style={{
            background: isLit ? `${data.color}22` : '#1e293b55',
            color: isLit ? data.color : '#64748b',
            border: `1px solid ${isLit ? `${data.color}55` : '#33415555'}`,
          }}
        >
          {data.badge || 'PROCESS'}
        </span>
        {isCurrent && <span className="rf-pulse-ring" />}
      </div>

      <div className="rf-card-title">{data.title}</div>
      <div className="rf-card-subtitle">{data.subtitle}</div>

      <Handle
        type="source"
        position={Position.Right}
        style={{
          background: isLit ? data.color : '#334155',
          width: 8,
          height: 8,
          border: '2px solid #080c14',
          right: -5,
        }}
      />
    </div>
  );
});
ProcessCardNode.displayName = 'ProcessCardNode';


/* ----------------------------------------------------------------
   Node Types Map for React Flow
   ---------------------------------------------------------------- */
export const nodeTypes = {
  sessionCardNode: SessionCardNode,
  phaseGroupNode: PhaseGroupNode,
  processCardNode: ProcessCardNode,
  // backwards compatibility
  sessionNode: SessionCardNode,
  toolNode: ProcessCardNode,
  agentNode: ProcessCardNode,
  userNode: ProcessCardNode,
  systemNode: ProcessCardNode,
  approvalNode: ProcessCardNode,
  workerNode: ProcessCardNode,
  errorNode: ProcessCardNode,
};
