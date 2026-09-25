import { useState, useEffect, useRef } from 'react';

function hasValue(value) {
  return value != null && value !== '';
}

function display(value) {
  if (!hasValue(value)) return '无';
  if (typeof value === 'object') return Object.keys(value).length ? JSON.stringify(value, null, 2) : '无';
  return String(value);
}

function asRecord(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function findingConfirmation(finding) {
  if (finding.status === 'retracted') return '已撤回';
  if (finding.superseded_by) return '已被后续记录替代';
  if (finding.verified === true || finding.status === 'confirmed' || finding.status === 'exploited') return '已证实';
  if (finding.verified === false) return '未证实';
  return '无';
}

function CountCard({ value, label }) {
  return <div className="stat-card"><div className="stat-num">{display(value)}</div><div className="stat-label">{label}</div></div>;
}

function RecordDetails({ value, label }) {
  return (
    <details className="record-source-details">
      <summary>完整{label}记录</summary>
      <pre className="detail-code-pre">{display(value)}</pre>
    </details>
  );
}

export default function ActionStream({
  actions = [],
  currentStep = 0,
  onSelectStep,
  selectedAction = null,
  detailRequestId = 0,
  summary = {},
}) {
  const [tabSelection, setTabSelection] = useState({ tab: 'stream', requestId: 0 });
  const [copyFeedback, setCopyFeedback] = useState(null);
  const activeItemRef = useRef(null);
  const detailBodyRef = useRef(null);
  const currentAction = selectedAction ?? actions[currentStep] ?? null;
  const activeTab = detailRequestId > 0 && detailRequestId !== tabSelection.requestId ? 'detail' : tabSelection.tab;
  const copyStatus = copyFeedback?.actionId === currentAction?.id && copyFeedback?.sessionId === summary.sessionId ? copyFeedback?.message : '';
  const targets = Array.isArray(summary.targets) ? summary.targets : [];
  const findings = Array.isArray(summary.findings) ? summary.findings : [];
  const creds = Array.isArray(summary.creds) ? summary.creds : [];
  const displayedStep = actions.length ? Math.min(currentStep + 1, actions.length) : 0;

  function setActiveTab(tab) {
    setTabSelection({ tab, requestId: detailRequestId });
  }

  useEffect(() => {
    if (activeTab === 'stream') activeItemRef.current?.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  }, [currentStep, activeTab]);

  useEffect(() => {
    if (detailBodyRef.current) detailBodyRef.current.scrollTop = 0;
  }, [currentAction?.id, summary.sessionId]);

  async function handleCopy(value) {
    try {
      await navigator.clipboard.writeText(display(value));
      setCopyFeedback({ actionId: currentAction?.id, sessionId: summary.sessionId, message: '已复制' });
    } catch {
      setCopyFeedback({ actionId: currentAction?.id, sessionId: summary.sessionId, message: '复制失败，请选择文本手动复制' });
    }
  }

  function dataBlock(label, value, output = false) {
    return (
      <section className="detail-block">
        <div className="detail-block-header">
          <span className="detail-block-title">{label}</span>
          {hasValue(value) && <button type="button" className="detail-copy-btn" onClick={() => handleCopy(value)} aria-label={`复制${label}`}>复制</button>}
        </div>
        <pre className={`detail-code-pre ${output ? 'output-pre' : ''}`}><code>{display(value)}</code></pre>
      </section>
    );
  }

  return (
    <aside className="action-stream-panel" aria-label="会话记录和详情">
      <div className="stream-tabs-header" role="group" aria-label="记录视图">
        <button type="button" className={`stream-tab-btn ${activeTab === 'stream' ? 'active' : ''}`} aria-pressed={activeTab === 'stream'} onClick={() => setActiveTab('stream')}>
          <span className="tab-label">动作流</span><span className="tab-count-badge">{displayedStep}/{actions.length}</span>
        </button>
        <button type="button" className={`stream-tab-btn ${activeTab === 'detail' ? 'active' : ''}`} aria-pressed={activeTab === 'detail'} onClick={() => setActiveTab('detail')}>
          <span className="tab-label">详细信息</span>
        </button>
        <button type="button" className={`stream-tab-btn ${activeTab === 'findings' ? 'active' : ''}`} aria-pressed={activeTab === 'findings'} onClick={() => setActiveTab('findings')}>
          <span className="tab-label">会话汇总</span>
        </button>
      </div>

      {activeTab === 'stream' && (
        <div className="action-stream-body">
          <div className="action-stream-meta-bar"><span>动作流</span><span className="stream-action-tally">{displayedStep}/{actions.length} 条记录</span></div>
          <div className="action-items-list">
            {actions.length === 0 && <p className="empty-state">动作记录：无</p>}
            {actions.map((action, index) => {
              const isActive = index === currentStep;
              return (
                <button
                  type="button"
                  key={action.id ?? index}
                  ref={isActive ? activeItemRef : null}
                  className={`action-stream-item ${isActive ? 'active-item' : ''}`}
                  aria-current={isActive ? 'step' : undefined}
                  aria-label={`第 ${index + 1} 条：${display(action.title)}，查看详情`}
                  onClick={() => { onSelectStep(index); setActiveTab('detail'); }}
                >
                  <span className="action-item-time">{display(action.timeOffset)}</span>
                  <span className="action-category-badge" style={{ color: action.categoryColor || '#94a3b8' }}>{display(action.category || action.kind)}</span>
                  <span className="action-item-title" title={display(action.title)}>{display(action.title)}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {activeTab === 'detail' && (
        <div className="detail-panel-body" ref={detailBodyRef}>
          {!currentAction ? <p className="empty-state">详细信息：无</p> : (
            <>
              <div className="detail-meta-card">
                <div className="detail-meta-top">
                  <span className="detail-category-pill" style={{ color: currentAction.categoryColor || '#94a3b8' }}>{display(currentAction.category || currentAction.kind)}</span>
                  <span className="detail-stage-pill">阶段：{display(currentAction.stageTitle || currentAction.stageKey)}</span>
                  <span className="detail-time-stamp">时间：{display(currentAction.timeOffset)}</span>
                </div>
                <div className="detail-title-large">{display(currentAction.title)}</div>
                <dl className="detail-metadata-list">
                  <div><dt>执行者</dt><dd>{display(currentAction.actor)}</dd></div>
                  <div><dt>消息角色</dt><dd>{display(currentAction.role)}</dd></div>
                  <div><dt>工具</dt><dd>{display(currentAction.tool)}</dd></div>
                  <div><dt>状态</dt><dd>{display(currentAction.status)}</dd></div>
                </dl>
              </div>
              {hasValue(currentAction.text) && dataBlock('消息内容', currentAction.text)}
              {hasValue(currentAction.thought) && currentAction.thought !== currentAction.text && dataBlock('记录中的推理内容', currentAction.thought)}
              {dataBlock('输入参数', hasValue(currentAction.fullInput) ? currentAction.fullInput : currentAction.input)}
              {dataBlock('输出结果', hasValue(currentAction.fullOutput) ? currentAction.fullOutput : currentAction.output, true)}
              {hasValue(currentAction.data) && <RecordDetails value={currentAction.data} label="事件" />}
              <p className="copy-feedback" role="status">{copyStatus}</p>
            </>
          )}
        </div>
      )}

      {activeTab === 'findings' && (
        <div className="findings-panel-body">
          <div className="findings-stats-grid">
            <CountCard value={summary.targetsCount} label="目标数量" />
            <CountCard value={summary.findingsCount} label="发现记录" />
            <CountCard value={summary.verifiedFindingsCount} label="已证实发现" />
            <CountCard value={summary.totalActions ?? actions.length} label="动作记录" />
          </div>
          <section className="findings-section">
            <h3 className="findings-section-title">目标记录</h3>
            {targets.length === 0 ? <p className="empty-state">无</p> : targets.map((value, index) => {
              const target = asRecord(value);
              return (
                <div className="finding-item-box" key={`${target.id ?? 'target'}-${index}`}>
                  <div className="finding-item-header"><span className="finding-host">{display(target.host || target.url || target.name || (typeof value === 'string' ? value : null))}</span></div>
                  <div className="finding-item-content">端口：{display(target.open_ports ?? target.ports)}</div>
                  <div className="finding-item-content">服务：{display(target.services)}</div>
                  <div className="finding-item-content">说明：{display(target.notes ?? target.description)}</div>
                  <RecordDetails value={value} label="目标" />
                </div>
              );
            })}
          </section>
          <section className="findings-section">
            <h3 className="findings-section-title">发现记录</h3>
            {findings.length === 0 ? <p className="empty-state">无</p> : findings.map((value, index) => {
              const finding = asRecord(value);
              return (
                <div className="finding-item-box" key={`${finding.id ?? 'finding'}-${index}`}>
                  <div className="finding-item-header"><span className="finding-vuln-title">{display(finding.title || finding.name || (typeof value === 'string' ? value : null))}</span></div>
                  <div className="finding-item-content">严重程度：{display(finding.severity)}</div>
                  <div className="finding-item-content">状态：{display(finding.status)}</div>
                  <div className="finding-item-content">确认状态：{findingConfirmation(finding)}</div>
                  <div className="finding-item-content">说明：{display(finding.description ?? finding.detail)}</div>
                  <RecordDetails value={value} label="发现" />
                </div>
              );
            })}
          </section>
          <section className="findings-section">
            <h3 className="findings-section-title">凭据记录（{display(summary.credsCount)}）</h3>
            {creds.length === 0 ? <p className="empty-state">无</p> : creds.map((value, index) => <RecordDetails key={`${asRecord(value).id ?? 'credential'}-${index}`} value={value} label="凭据" />)}
          </section>
        </div>
      )}
    </aside>
  );
}
