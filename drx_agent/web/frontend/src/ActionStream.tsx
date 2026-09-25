import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowSquareOut, Crosshair, ListBullets, Stack, TerminalWindow } from '@phosphor-icons/react';
import { RawRecord } from './RecordDetails.tsx';
import { eventPresentation } from './presentation.ts';
import type { ReplayAction, SessionSummary } from './types.ts';

export type StreamView = 'stream' | 'findings';

interface ActionStreamProps {
  actions: ReplayAction[];
  currentStep: number;
  isSnapshot: boolean;
  summary: SessionSummary;
  view: StreamView;
  onViewChange: (view: StreamView) => void;
  onOpenAction: (step: number) => void;
}

function display(value: unknown): string {
  if (value == null || value === '') return '无';
  if (typeof value === 'object') return Object.keys(value).length ? JSON.stringify(value, null, 2) : '无';
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function findingConfirmation(finding: Record<string, unknown>): string {
  if (finding.status === 'retracted') return '已撤回';
  if (finding.superseded_by) return '已被后续记录替代';
  if (finding.verified === true || finding.status === 'confirmed' || finding.status === 'exploited') return '已证实';
  if (finding.verified === false) return '未证实';
  return '无';
}

function CountCard({ value, label }: { value: unknown; label: string }) {
  return <div className="stat-card"><div className="stat-num">{display(value)}</div><div className="stat-label">{label}</div></div>;
}

export default function ActionStream({
  actions,
  currentStep,
  isSnapshot,
  summary,
  view,
  onViewChange,
  onOpenAction,
}: ActionStreamProps) {
  const [followPreference, setFollowPreference] = useState({ sessionId: summary.sessionId, enabled: true });
  const listRef = useRef<HTMLDivElement>(null);
  const activeItemRef = useRef<HTMLElement>(null);
  const lastScrollTop = useRef(0);
  const lastCursor = useRef<{ sessionId: string; step: number } | null>(null);
  const following = followPreference.sessionId !== summary.sessionId || followPreference.enabled;
  const displayedStep = actions.length ? Math.max(0, Math.min(currentStep + 1, actions.length)) : 0;
  const targets = Array.isArray(summary.targets) ? summary.targets : [];
  const findings = Array.isArray(summary.findings) ? summary.findings : [];
  const creds = Array.isArray(summary.creds) ? summary.creds : [];

  const scrollToCurrent = useCallback(() => {
    const container = listRef.current;
    const activeItem = activeItemRef.current;
    if (isSnapshot || !container || !activeItem) return;
    const frame = container.getBoundingClientRect();
    const item = activeItem.getBoundingClientRect();
    if (item.top < frame.top || item.height > frame.height) {
      container.scrollTop += item.top - frame.top;
    } else if (item.bottom > frame.bottom) {
      container.scrollTop += item.bottom - frame.bottom;
    }
    lastScrollTop.current = container.scrollTop;
  }, [isSnapshot]);

  useEffect(() => {
    const previous = lastCursor.current;
    const changed = previous?.sessionId !== summary.sessionId || previous.step !== currentStep;
    lastCursor.current = { sessionId: summary.sessionId, step: currentStep };
    if (previous?.sessionId !== summary.sessionId) lastScrollTop.current = 0;
    if (changed && following && view === 'stream') scrollToCurrent();
  }, [currentStep, summary.sessionId, following, view, scrollToCurrent]);

  function stopFollowing(): void {
    if (following) setFollowPreference({ sessionId: summary.sessionId, enabled: false });
  }

  function resumeFollowing(): void {
    setFollowPreference({ sessionId: summary.sessionId, enabled: true });
    scrollToCurrent();
  }

  return (
    <aside className="action-stream-panel" aria-label="会话事件日志和汇总">
      <div className="stream-panel-heading">
        <div className="stream-heading-label"><TerminalWindow size={17} aria-hidden="true" /><h2>事件日志</h2></div>
        <span className="stream-action-tally" aria-label={isSnapshot ? `已保存 ${actions.length} 条记录` : `回放定位第 ${displayedStep} 条，共 ${actions.length} 条`}>{isSnapshot ? actions.length : displayedStep}<span>{isSnapshot ? ' 条记录' : ` / ${actions.length}`}</span></span>
      </div>
      <div className="stream-tabs-header" role="group" aria-label="记录视图">
        <button type="button" className={`stream-tab-btn${view === 'stream' ? ' active' : ''}`} aria-pressed={view === 'stream'} onClick={() => onViewChange('stream')}>
          <ListBullets size={15} aria-hidden="true" /><span className="tab-label">日志</span>
        </button>
        <button type="button" className={`stream-tab-btn${view === 'findings' ? ' active' : ''}`} aria-pressed={view === 'findings'} onClick={() => onViewChange('findings')}>
          <Stack size={15} aria-hidden="true" /><span className="tab-label">汇总</span>
        </button>
      </div>

      {view === 'stream' && (
        <div className="action-stream-body">
          <div className="action-stream-meta-bar">
            <span>{isSnapshot ? `历史会话 · 已保存 ${actions.length} 条` : `回放定位 ${displayedStep} / ${actions.length}`}</span>
            <button type="button" className={`stream-follow-btn${following && !isSnapshot ? ' active' : ''}`} aria-pressed={following && !isSnapshot} disabled={actions.length === 0 || isSnapshot} onClick={following ? stopFollowing : resumeFollowing}>
              <Crosshair size={13} aria-hidden="true" /><span>{isSnapshot ? '跟随回放' : following ? '跟随中' : '跟随当前'}</span>
            </button>
          </div>
          <div
            className="action-items-list"
            ref={listRef}
            tabIndex={0}
            role="region"
            aria-label="完整事件日志"
            onWheel={(event) => { if (event.deltaY < 0) stopFollowing(); }}
            onScroll={(event) => {
              const top = event.currentTarget.scrollTop;
              if (top < lastScrollTop.current - 1) stopFollowing();
              lastScrollTop.current = top;
            }}
            onKeyDown={(event) => { if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) stopFollowing(); }}
          >
            {actions.length === 0 && <p className="empty-state">动作记录：无</p>}
            {actions.map((action, index) => {
              const isActive = !isSnapshot && index === currentStep;
              const preview = action.text || action.thought || action.output || action.input;
              const presentation = eventPresentation(action);
              return (
                <article
                  key={`${summary.sessionId}:${action.id ?? index}`}
                  ref={isActive ? activeItemRef : null}
                  className={`action-stream-item${isActive ? ' active-item' : ''}${!isSnapshot && index > currentStep ? ' upcoming-item' : ''}`}
                  style={{ borderLeftColor: presentation.color }}
                  aria-current={isActive ? 'step' : undefined}
                >
                  <button
                    type="button"
                    className="action-item-open"
                    aria-label={`第 ${index + 1} 条：${display(action.title)}，查看详情`}
                    onClick={() => onOpenAction(index)}
                  >
                    <span className="action-item-meta">
                      <span className="action-category-badge" style={{ color: presentation.color }}>{display(action.category || action.kind)}</span>
                      <span className="action-item-time">{display(action.timeOffset)}</span>
                    </span>
                    {action.kind !== 'message' && <span className="action-item-title">{display(action.title)}</span>}
                    <span className="action-item-preview">{display(preview)}</span>
                    <span className="action-item-footer"><span>#{index + 1} · {isSnapshot ? '已保存' : isActive ? '当前记录' : index > currentStep ? '未回放' : '已回放'}</span><ArrowSquareOut size={13} aria-hidden="true" /></span>
                  </button>
                  <RawRecord value={action.data} label="事件" />
                </article>
              );
            })}
          </div>
        </div>
      )}

      {view === 'findings' && (
        <div className="findings-panel-body">
          <div className="findings-stats-grid">
            <CountCard value={summary.targetsCount} label="目标数量" />
            <CountCard value={summary.findingsCount} label="发现记录" />
            <CountCard value={summary.verifiedFindingsCount} label="已证实发现" />
            <CountCard value={summary.totalActions ?? actions.length} label="动作记录" />
          </div>
          <section className="findings-section">
            <h3 className="findings-section-title">目标记录 <span>{targets.length}</span></h3>
            {targets.length === 0 ? <p className="empty-state">无</p> : targets.map((value, index) => {
              const target = asRecord(value);
              return (
                <div className="finding-item-box" key={`${target.id ?? 'target'}-${index}`}>
                  <div className="finding-item-header"><span className="finding-host">{display(target.host || target.url || target.name || (typeof value === 'string' ? value : null))}</span></div>
                  <div className="finding-item-content">端口：{display(target.open_ports ?? target.ports)}</div>
                  <div className="finding-item-content">服务：{display(target.services)}</div>
                  <div className="finding-item-content">说明：{display(target.notes ?? target.description)}</div>
                  <RawRecord value={value} label="目标" />
                </div>
              );
            })}
          </section>
          <section className="findings-section">
            <h3 className="findings-section-title">发现记录 <span>{findings.length}</span></h3>
            {findings.length === 0 ? <p className="empty-state">无</p> : findings.map((value, index) => {
              const finding = asRecord(value);
              return (
                <div className="finding-item-box" key={`${finding.id ?? 'finding'}-${index}`}>
                  <div className="finding-item-header"><span className="finding-vuln-title">{display(finding.claim || finding.title || finding.name || (typeof value === 'string' ? value : null))}</span></div>
                  <div className="finding-item-content">严重程度：{display(finding.severity)}</div>
                  <div className="finding-item-content">状态：{display(finding.status)}</div>
                  <div className="finding-item-content">确认状态：{findingConfirmation(finding)}</div>
                  <div className="finding-item-content">说明：{display(finding.description ?? finding.detail)}</div>
                  <RawRecord value={value} label="发现" />
                </div>
              );
            })}
          </section>
          <section className="findings-section">
            <h3 className="findings-section-title">凭据记录 <span>{display(summary.credsCount)}</span></h3>
            {creds.length === 0 ? <p className="empty-state">无</p> : creds.map((value, index) => <RawRecord key={`${asRecord(value).id ?? 'credential'}-${index}`} value={value} label="凭据" />)}
          </section>
        </div>
      )}
    </aside>
  );
}
