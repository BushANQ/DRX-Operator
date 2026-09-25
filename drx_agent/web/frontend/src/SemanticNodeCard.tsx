import { memo, type CSSProperties } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import {
  ArrowSquareOut, CaretDown, CaretUp, ChatCircle, Cube, FileMagnifyingGlass,
  Flag, FolderOpen, GitBranch, Key, Lightbulb, Lightning, Question,
  ShieldCheck, ShieldWarning, Target, TreeStructure, Wrench,
} from '@phosphor-icons/react';
import { statusPresentation } from './presentation.ts';
import type { SemanticFlowNode } from './semanticTypes.ts';

interface KindPresentation {
  label: string;
  variant: string;
  color: string;
  Icon: typeof TreeStructure;
}

const kindStyles: Record<string, KindPresentation> = {
  root: { label: '主任务', variant: 'root', color: '#759ff6', Icon: TreeStructure },
  session: { label: '会话', variant: 'root', color: '#759ff6', Icon: TreeStructure },
  request: { label: '用户指令', variant: 'request', color: '#759ff6', Icon: ChatCircle },
  turn: { label: '助手消息', variant: 'turn', color: '#88acd5', Icon: ChatCircle },
  context: { label: '宿主上下文', variant: 'group', color: '#a7abc6', Icon: FolderOpen },
  toolgroup: { label: '工具调用组', variant: 'group', color: '#e8b45f', Icon: Wrench },
  task: { label: '子任务', variant: 'task', color: '#b291ec', Icon: GitBranch },
  subtask: { label: '子任务', variant: 'task', color: '#b291ec', Icon: GitBranch },
  intent: { label: '探索任务', variant: 'task', color: '#b291ec', Icon: Target },
  workertask: { label: '子 agent', variant: 'task', color: '#b291ec', Icon: GitBranch },
  action: { label: '动作', variant: 'action', color: '#e8b45f', Icon: Wrench },
  executionstep: { label: '执行步骤', variant: 'action', color: '#e8b45f', Icon: Wrench },
  tool: { label: '工具记录', variant: 'action', color: '#e8b45f', Icon: Wrench },
  message: { label: '消息', variant: 'message', color: '#95a9cc', Icon: ChatCircle },
  group: { label: '记录分组', variant: 'group', color: '#a7abc6', Icon: FolderOpen },
  evidence: { label: '证据', variant: 'evidence', color: '#68bec7', Icon: FileMagnifyingGlass },
  evidencereference: { label: '证据引用', variant: 'reference', color: '#8bb9c5', Icon: FileMagnifyingGlass },
  keyfact: { label: '关键事实', variant: 'evidence', color: '#68bec7', Icon: FileMagnifyingGlass },
  fact: { label: '事实记录', variant: 'evidence', color: '#68bec7', Icon: FileMagnifyingGlass },
  reference: { label: '引用记录', variant: 'reference', color: '#93a4bc', Icon: FolderOpen },
  hypothesis: { label: '假设', variant: 'hypothesis', color: '#b39aef', Icon: Lightbulb },
  finding: { label: '发现记录', variant: 'finding', color: '#dba776', Icon: ShieldWarning },
  vulnerability: { label: '漏洞记录', variant: 'finding', color: '#e79383', Icon: ShieldWarning },
  possiblevulnerability: { label: '疑似漏洞', variant: 'finding', color: '#dba776', Icon: ShieldWarning },
  confirmedvulnerability: { label: '已确认漏洞', variant: 'finding', color: '#e79383', Icon: ShieldCheck },
  exploit: { label: '利用记录', variant: 'exploit', color: '#df9ad0', Icon: Lightning },
  credential: { label: '凭据', variant: 'credential', color: '#d6b678', Icon: Key },
  systemproperty: { label: '系统属性', variant: 'artifact', color: '#9cacc2', Icon: Cube },
  targetartifact: { label: '目标产物', variant: 'artifact', color: '#7fb1d4', Icon: Target },
  target: { label: '目标', variant: 'artifact', color: '#7fb1d4', Icon: Target },
  flag: { label: 'Flag', variant: 'artifact', color: '#77c4a4', Icon: Flag },
};

function describeKind(kind: string): KindPresentation {
  const normalized = kind.replace(/[_\s-]/g, '').toLowerCase();
  return kindStyles[normalized] ?? {
    label: !normalized || normalized === 'unknown' ? '无' : kind,
    variant: 'unknown',
    color: '#8e9bb2',
    Icon: Question,
  };
}

function describeStatus(raw: string | null): { label: string; tone: string; color: string } {
  const normalized = raw?.trim().toLowerCase();
  if (!normalized || normalized === 'unknown') return statusPresentation(null);
  switch (normalized) {
    case 'confirmed': case 'verified': return { label: '已证实', tone: 'confirmed', color: '#64bca0' };
    case 'supported': return { label: '支持', tone: 'supported', color: '#76b2bf' };
    case 'contradicted': return { label: '存在矛盾', tone: 'contradicted', color: '#dc9b70' };
    case 'falsified': return { label: '已证伪', tone: 'falsified', color: '#d67f8e' };
    case 'retracted': return { label: '已撤回', tone: 'retracted', color: '#9a91a7' };
    case 'superseded': return { label: '已被替代', tone: 'superseded', color: '#9a91a7' };
    default: {
      const status = statusPresentation(normalized);
      return status.label === normalized ? { ...status, label: raw ?? '无' } : status;
    }
  }
}

export function SemanticKindChip({ kind }: { kind: string }) {
  const { Icon, label, color, variant } = describeKind(kind);
  return (
    <span className={`semantic-kind-chip semantic-kind-${variant}`} style={{ '--kind-color': color } as CSSProperties}>
      <Icon size={11} weight="bold" aria-hidden="true" /><span>{label}</span>
    </span>
  );
}

const SemanticNodeCard = memo(({ data, selected }: NodeProps<SemanticFlowNode>) => {
  const kind = describeKind(data.kind);
  const status = describeStatus(data.status);
  const title = data.label.trim() ? data.label : '无';
  return (
    <div
      className={`semantic-node-card semantic-node-${kind.variant} semantic-status-${status.tone}${data.current ? ' is-current' : ''}${data.containsCurrent ? ' contains-current' : ''}${selected ? ' is-selected' : ''}${data.collapsed ? ' is-collapsed' : ''}`}
      style={{ '--kind-color': kind.color, '--status-color': status.color } as CSSProperties}
      data-kind={data.kind}
      data-status={data.status ?? ''}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="semantic-node-heading"><SemanticKindChip kind={data.kind} />{(data.current || data.containsCurrent) && <span className="semantic-node-current">{data.containsCurrent ? '含当前' : '当前'}</span>}</div>
      <div className="semantic-node-title" title={title}>{title}</div>
      <div className="semantic-node-footer">
        <span className="semantic-node-status" title={data.status ?? '无'}><span className="semantic-status-dot" aria-hidden="true" />{status.label}</span>
        {data.onOpen && (
          <button
            type="button"
            className="semantic-node-open nodrag nopan"
            aria-label={`查看${kind.label}详情：${title}`}
            onClick={(event) => { event.stopPropagation(); data.onOpen?.(); }}
            onKeyDown={(event) => { event.stopPropagation(); }}
          ><ArrowSquareOut size={14} aria-hidden="true" /></button>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
      {data.canCollapse && (
        <button
          type="button"
          className="semantic-node-collapse nodrag nopan"
          aria-expanded={!data.collapsed}
          aria-label={`${data.collapsed ? '展开' : '收起'}“${title}”的执行记录${data.collapsed ? `，${data.hiddenCount} 条` : ''}`}
          disabled={!data.onToggle}
          onClick={(event) => { event.stopPropagation(); data.onToggle?.(); }}
          onKeyDown={(event) => { event.stopPropagation(); }}
        >
          {data.collapsed ? <CaretDown size={12} weight="bold" aria-hidden="true" /> : <CaretUp size={12} weight="bold" aria-hidden="true" />}
          {data.collapsed && <span>{data.hiddenCount} 条</span>}
        </button>
      )}
    </div>
  );
});

SemanticNodeCard.displayName = 'SemanticNodeCard';
export default SemanticNodeCard;
