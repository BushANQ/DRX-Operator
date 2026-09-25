import { useState, useEffect, useRef } from 'react';

export default function ActionStream({
  actions = [],
  currentStep = 0,
  onSelectStep,
  selectedNode,
  summary = {},
}) {
  const [activeTab, setActiveTab] = useState('stream'); // 'stream' | 'detail' | 'findings'
  const listRef = useRef(null);
  const activeItemRef = useRef(null);

  // When selectedNode changes externally, auto-switch to detail tab
  useEffect(() => {
    if (selectedNode) {
      setActiveTab('detail');
    }
  }, [selectedNode]);

  // Auto-scroll action stream to active item during replay
  useEffect(() => {
    if (activeTab === 'stream' && activeItemRef.current) {
      activeItemRef.current.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
      });
    }
  }, [currentStep, activeTab]);

  const currentAction = actions[currentStep] || {};
  const activeDetail = selectedNode?.data || currentAction;

  // Copy helper
  const handleCopy = (text) => {
    if (text) {
      navigator.clipboard.writeText(typeof text === 'string' ? text : JSON.stringify(text, null, 2));
    }
  };

  return (
    <aside className="action-stream-panel">
      {/* ---------------- Tab Switcher Header ---------------- */}
      <div className="stream-tabs-header">
        <button
          className={`stream-tab-btn ${activeTab === 'stream' ? 'active' : ''}`}
          onClick={() => setActiveTab('stream')}
        >
          <span className="tab-label">动作流</span>
          <span className="tab-count-badge">
            {actions.length > 0 ? `${currentStep + 1}/${actions.length}` : '0'}
          </span>
        </button>

        <button
          className={`stream-tab-btn ${activeTab === 'detail' ? 'active' : ''}`}
          onClick={() => setActiveTab('detail')}
        >
          <span className="tab-label">详细信息</span>
          {selectedNode && <span className="tab-active-dot" />}
        </button>

        <button
          className={`stream-tab-btn ${activeTab === 'findings' ? 'active' : ''}`}
          onClick={() => setActiveTab('findings')}
        >
          <span className="tab-label">研判战果</span>
          {(summary.findingsCount > 0 || summary.targetsCount > 0) && (
            <span className="tab-findings-badge">
              {(summary.findingsCount || 0) + (summary.targetsCount || 0)}
            </span>
          )}
        </button>
      </div>

      {/* ---------------- 1. Action Stream Tab ---------------- */}
      {activeTab === 'stream' && (
        <div className="action-stream-body" ref={listRef}>
          <div className="action-stream-meta-bar">
            <span>动作流</span>
            <span className="stream-action-tally">
              {actions.length ? currentStep + 1 : 0}/{actions.length} 条动作
            </span>
          </div>

          <div className="action-items-list">
            {actions.map((act, i) => {
              const isActive = i === currentStep;

              return (
                <button
                  type="button"
                  key={act.id || i}
                  ref={isActive ? activeItemRef : null}
                  className={`action-stream-item ${isActive ? 'active-item' : ''}`}
                  onClick={() => {
                    onSelectStep(i);
                    setActiveTab('detail');
                  }}
                >
                  <div className="action-item-time">{act.timeOffset}</div>

                  <span
                    className="action-category-badge"
                    style={{
                      background: `${act.categoryColor}18`,
                      color: act.categoryColor,
                      border: `1px solid ${act.categoryColor}44`,
                    }}
                  >
                    {act.category}
                  </span>

                  <div className="action-item-title" title={act.title}>
                    {act.title}
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* ---------------- 2. Detail Tab ---------------- */}
      {activeTab === 'detail' && !selectedNode && !actions.length && <div className="detail-panel-body">无</div>}
      {activeTab === 'detail' && (selectedNode || actions.length > 0) && (
        <div className="detail-panel-body animate-fade-in">
          {/* Node / Action Title Header */}
          <div className="detail-meta-card">
            <div className="detail-meta-top">
              <span
                className="detail-category-pill"
                style={{
                  background: `${activeDetail.categoryColor || activeDetail.color || '#38bdf8'}22`,
                  color: activeDetail.categoryColor || activeDetail.color || '#38bdf8',
                  border: `1px solid ${activeDetail.categoryColor || activeDetail.color || '#38bdf8'}55`,
                }}
              >
                {activeDetail.category || activeDetail.badge || 'PROCESS'}
              </span>
              <span className="detail-stage-pill">
                {activeDetail.stageTitle || '当前阶段'}
              </span>
              <span className="detail-time-stamp">
                {activeDetail.timeOffset || 'T+0s'}
              </span>
            </div>

            <div className="detail-title-large">
              {activeDetail.title || activeDetail.label || '节点详细'}
            </div>

            {activeDetail.subtitle && (
              <div className="detail-sub-description">
                {activeDetail.subtitle}
              </div>
            )}
            {activeDetail.summary && (
              <div className="detail-summary-quote">
                {activeDetail.summary}
              </div>
            )}
          </div>

          {/* Thought / Reasoning */}
          {activeDetail.thought && (
            <div className="detail-block">
              <div className="detail-block-header">
                <span className="detail-block-title">🧠 认知推理推演</span>
              </div>
              <div className="detail-thought-box">
                {activeDetail.thought}
              </div>
            </div>
          )}

          {/* Tool Command / Input */}
          {(activeDetail.fullInput || activeDetail.input) && (
            <div className="detail-block">
              <div className="detail-block-header">
                <span className="detail-block-title">
                  ⚡ 执行指令 / 输入参数 {activeDetail.tool && `(${activeDetail.tool})`}
                </span>
                <button
                  className="detail-copy-btn"
                  onClick={() => handleCopy(activeDetail.fullInput || activeDetail.input)}
                  title="复制输入"
                >
                  复制
                </button>
              </div>
              <pre className="detail-code-pre">
                <code>{activeDetail.fullInput || activeDetail.input}</code>
              </pre>
            </div>
          )}

          {/* Tool Output / Result */}
          {(activeDetail.fullOutput || activeDetail.output) && (
            <div className="detail-block">
              <div className="detail-block-header">
                <span className="detail-block-title">📥 执行返回 / 探测响应</span>
                <button
                  className="detail-copy-btn"
                  onClick={() => handleCopy(activeDetail.fullOutput || activeDetail.output)}
                  title="复制输出"
                >
                  复制
                </button>
              </div>
              <pre className="detail-code-pre output-pre">
                <code>{activeDetail.fullOutput || activeDetail.output}</code>
              </pre>
            </div>
          )}

          {/* Target Host metadata */}
          {activeDetail.target && (
            <div className="detail-block">
              <div className="detail-block-header">
                <span className="detail-block-title">🎯 目标地址</span>
              </div>
              <div className="detail-target-card">
                <div className="target-url-text">{activeDetail.target}</div>
                {activeDetail.sub && <div className="target-sub-text">{activeDetail.sub}</div>}
              </div>
            </div>
          )}
        </div>
      )}

      {/* ---------------- 3. Findings / Summary Tab ---------------- */}
      {activeTab === 'findings' && (
        <div className="findings-panel-body animate-fade-in">
          <div className="findings-stats-grid">
            <div className="stat-card">
              <div className="stat-num">{summary.targetsCount ?? 0}</div>
              <div className="stat-label">目标靶机</div>
            </div>
            <div className="stat-card">
              <div className="stat-num">{summary.findingsCount ?? 0}</div>
              <div className="stat-label">发现记录</div>
            </div>
            <div className="stat-card">
              <div className="stat-num">{summary.totalActions || actions.length}</div>
              <div className="stat-label">总执行步数</div>
            </div>
          </div>

          {/* Targets */}
          <div className="findings-section">
            <div className="findings-section-title">🎯 资产测绘成果</div>
            <div className="findings-list">
              <div className="finding-item-box">
                <div className="finding-item-header">
                  <span className="finding-badge target-badge">ACTIVE TARGET</span>
                  <span className="finding-host">{summary.targetHost || '192.168.0.104'}</span>
                </div>
                <div className="finding-item-content">
                  {summary.targetNotes || 'Node.js 应用「奶龙集成馆」· 端口 3000 · 局域网直连'}
                </div>
              </div>
            </div>
          </div>

          {/* Findings */}
          <div className="findings-section">
            <div className="findings-section-title">💥 漏洞利用与研判结论</div>
            <div className="findings-list">
              {(summary.findings && summary.findings.length > 0) ? (
                summary.findings.map((f, i) => (
                  <div key={i} className="finding-item-box vuln-box">
                    <div className="finding-item-header">
                      <span className="finding-badge vuln-badge">{f.severity || '无等级记录'}</span>
                      <span className="finding-vuln-title">{f.claim || f.title || f.name || '无'}</span>
                    </div>
                    <div className="finding-item-content">
                      {f.description || f.detail || JSON.stringify(f)}
                    </div>
                  </div>
                ))
              ) : (
                <div className="finding-item-box">无</div>
              )}
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}
