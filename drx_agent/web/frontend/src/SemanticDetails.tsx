import { useEffect, useRef } from 'react';
import { LinkSimple } from '@phosphor-icons/react';
import RecordDetails, { RawRecord } from './RecordDetails.tsx';
import { SemanticKindChip } from './SemanticNodeCard.tsx';
import type { SemanticNode, SemanticEdge } from './semanticTypes.ts';
import type { ReplayAction } from './types.ts';

interface SemanticDetailsProps {
  node: SemanticNode;
  action: ReplayAction | null;
  sessionId: string;
  relations?: SemanticEdge[];
}

function display(value: unknown): string {
  if (value == null || value === '') return '无';
  if (typeof value === 'object') return Object.keys(value).length ? JSON.stringify(value, null, 2) : '无';
  return String(value);
}

function groupingLabel(value: unknown): string {
  if (value === 'conversation') return '对话归属';
  if (value === 'actor') return '执行者归属';
  if (value === 'unassigned') return '未分配';
  if (value === 'saved_plan') return '已保存计划';
  return display(value);
}

export default function SemanticDetails({ node, action, sessionId, relations = [] }: SemanticDetailsProps) {
  const bodyRef = useRef<HTMLDivElement>(null);
  const source = node.source;
  const status = node.status?.trim().toLowerCase() === 'unknown' ? null : node.status;

  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [node.id, sessionId]);

  const relationDetails = <section className="semantic-relations">
    <h3 className="detail-block-title">节点关系 · {relations.length}</h3>
    {relations.length === 0 ? <p className="empty-state">无</p> : relations.map((edge) => <details key={edge.id} className="record-source-details">
      <summary>{edge.target === node.id ? '入向' : '出向'} · {edge.label || edge.relation}</summary>
      <dl className="detail-metadata-list semantic-source-list"><div><dt>关系类型</dt><dd>{edge.relation}</dd></div><div><dt>来源节点</dt><dd>{edge.source}</dd></div><div><dt>目标节点</dt><dd>{edge.target}</dd></div></dl>
      <RawRecord value={edge.sourceInfo} label="关系来源" />
    </details>)}
  </section>;
  if (action) return <RecordDetails key={`${sessionId}:${action.id}`} action={action} sessionId={sessionId}>{relationDetails}</RecordDetails>;

  return (
    <div className="detail-panel-body semantic-details-body" ref={bodyRef}>
      <div className="detail-meta-card">
        <div className="detail-meta-top"><SemanticKindChip kind={node.kind} /></div>
        <h2 className="detail-title-large">{display(node.label)}</h2>
        <dl className="detail-metadata-list">
          <div><dt>记录 ID</dt><dd>{display(node.id)}</dd></div>
          <div><dt>状态</dt><dd>{display(status)}</dd></div>
          {source.actor != null && <div><dt>执行者</dt><dd>{display(source.actor)}</dd></div>}
        </dl>
      </div>
      {relationDetails}
      <section className="semantic-source-section">
        <h3 className="detail-block-title semantic-source-heading"><LinkSimple size={14} aria-hidden="true" />关联与来源</h3>
        <dl className="detail-metadata-list semantic-source-list">
          <div><dt>来源位置</dt><dd>{display(source.path)}</dd></div>
          <div><dt>归属依据</dt><dd>{groupingLabel(source.grouping)}</dd></div>
          <div><dt>未解析关联</dt><dd>{display(source.unresolved)}</dd></div>
          {source.unresolvedRelations != null && <div><dt>待解析关系</dt><dd>{display(source.unresolvedRelations)}</dd></div>}
        </dl>
        {source.record != null && <RawRecord key={`${sessionId}:${node.id}:record`} value={source.record} label="实体" />}
        <RawRecord key={`${sessionId}:${node.id}:source`} value={source} label="来源" />
      </section>
    </div>
  );
}
