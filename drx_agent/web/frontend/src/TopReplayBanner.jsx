import { useState, useCallback, useRef, useEffect } from 'react';

const SPEED_OPTIONS = [0.5, 1, 2, 4];

export default function TopReplayBanner({
  sessionName = 'test',
  sessions = [],
  selectedSessionId,
  onSelectSession,
  currentAction,
  currentStep = 0,
  totalSteps = 38,
  stages = [],
  actions = [],
  activeStageKey = '推理',
  isPlaying = false,
  onTogglePlay,
  speed = 1,
  onSetSpeed,
  isLoop = false,
  onToggleLoop,
  onReset,
  onStepChange,
  isFullscreen = false,
  onToggleFullscreen,
}) {
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef(null);

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(e) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    const handleEscape = (event) => {
      if (event.key === 'Escape') setDropdownOpen(false);
    };
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleEscape);
    };
  }, []);

  // Compute progress percentage
  const progressPercent =
    totalSteps > 0
      ? Math.min(100, Math.max(0, Math.round(((currentStep + 1) / totalSteps) * 100)))
      : 0;

  // Handle slider scrub
  const handleSliderChange = (e) => {
    const val = parseInt(e.target.value, 10);
    onStepChange(val);
  };

  const stageTitle = currentAction?.stageTitle || '第 4 阶段 · 推理';
  const stageTag = currentAction?.stageTag || '影响边界 战役推进中';
  const timeOffset = currentAction?.timeOffset || 'T+1m35s';

  return (
    <div className="top-replay-dashboard">
      {/* ---------------- 1. Top Navbar ---------------- */}
      <header className="top-navbar">
        <div className="nav-left">
          <div className="nav-brand-badge">研判报告台</div>
          <div className="nav-breadcrumb">
            <span className="nav-breadcrumb-prefix">研判回放 ·</span>
            <div className="nav-session-selector" ref={dropdownRef}>
              <button
                className="nav-session-btn"
                onClick={() => setDropdownOpen(!dropdownOpen)}
                title="切换研判会话"
                aria-expanded={dropdownOpen}
              >
                <span>{sessionName}</span>
                <span className="dropdown-arrow">▾</span>
              </button>

              {dropdownOpen && (
                <div className="session-dropdown-menu animate-fade-in">
                  <div className="session-dropdown-header">选择研判战役会话</div>
                  {sessions.map((s) => (
                    <button
                      type="button"
                      key={s.id}
                      className={`session-dropdown-item ${
                        s.id === selectedSessionId ? 'active' : ''
                      }`}
                      onClick={() => {
                        onSelectSession(s.id);
                        setDropdownOpen(false);
                      }}
                    >
                      <div className="session-item-name">{s.name || s.id}</div>
                      <div className="session-item-meta">
                        {s.created_at
                          ? new Date(s.created_at * 1000).toLocaleString('zh-CN')
                          : ''}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="nav-right">
          <div className="nav-progress-percent">{progressPercent}%</div>
          <button
            className="nav-fullscreen-btn"
            onClick={onToggleFullscreen}
            title={isFullscreen ? '退出全屏' : '全屏展示'}
          >
            {isFullscreen ? '退出全屏' : '全屏'}
          </button>
        </div>
      </header>

      {/* ---------------- 2. Replay Phase Header ---------------- */}
      <div className="replay-stage-banner">
        <div className="replay-stage-left">
          <div className="replay-phase-caption">研判战役回放</div>
          <div className="replay-phase-heading">
            <span className="replay-stage-main-title">{stageTitle}</span>
            <span className="replay-status-pill">{stageTag}</span>
          </div>
        </div>
      </div>

      {/* ---------------- 3. Glowing Progress Bar ---------------- */}
      <div className="replay-glowing-track">
        <div
          className="replay-glowing-fill"
          style={{ width: `${progressPercent}%` }}
        />
      </div>

      {/* ---------------- 4. Media Controls & Slider ---------------- */}
      <div className="replay-controls-bar">
        <div className="controls-button-group">
          {/* Play / Pause */}
          <button
            className="ctrl-btn primary-glow"
            onClick={() => onTogglePlay(!isPlaying)}
            title={isPlaying ? '暂停' : '播放'}
          >
            {isPlaying ? '暂停' : '播放'}
          </button>

          {/* Reset */}
          <button className="ctrl-btn secondary" onClick={onReset} title="重置回放">
            重来
          </button>

          {/* Loop */}
          <button
            className={`ctrl-btn secondary ${isLoop ? 'active-loop' : ''}`}
            onClick={onToggleLoop}
            title={isLoop ? '循环开启' : '循环关闭'}
          >
            循环
          </button>

          {/* Speed Buttons */}
          <div className="speed-selector">
            {SPEED_OPTIONS.map((s) => (
              <button
                key={s}
                className={`speed-btn ${speed === s ? 'active' : ''}`}
                onClick={() => onSetSpeed(s)}
              >
                {s}x
              </button>
            ))}
          </div>
        </div>

        {/* Range Slider Scrubber */}
        <div className="slider-wrapper">
          <input
            type="range"
            aria-label="回放动作进度"
            min={0}
            max={Math.max(0, totalSteps - 1)}
            value={currentStep}
            onChange={handleSliderChange}
            className="custom-range-slider"
          />
        </div>

        {/* Time and Step Indicator */}
        <div className="replay-timer-label">
          {timeOffset} · 演练 · {currentStep + 1}/{totalSteps}
        </div>
      </div>

      {/* ---------------- 5. Horizontal Stage Activity Track ---------------- */}
      <div className="stage-activity-container">
        <div className="stage-activity-title">阶段活跃</div>

        <div className="stage-milestone-track">
          {stages.map((st, i) => {
            const isActive = st.key === activeStageKey;
            const isCompleted = i <= stages.findIndex((s) => s.key === activeStageKey);
            // Height proportional to action count
            const barHeight = Math.min(24, Math.max(0, (st.count ?? 0) * 3));

            return (
              <button
                type="button"
                disabled={!st.count}
                aria-pressed={isActive}
                key={st.key}
                className={`stage-milestone-step ${isActive ? 'active' : ''} ${
                  isCompleted ? 'completed' : ''
                }`}
                onClick={() => {
                  // Find first action in this stage
                  const firstStep = actions.findIndex((action) => action.stageKey === st.key);
                  if (firstStep >= 0) onStepChange(firstStep);
                }}
              >
                {/* Mini activity bar above icon */}
                <div className="mini-histogram">
                  <div
                    className={`mini-bar ${isActive ? 'active-bar' : ''}`}
                    style={{ height: `${barHeight}px` }}
                  />
                </div>

                {/* Active node pill badge or simple icon */}
                {isActive ? (
                  <div className="active-stage-glow-pill">
                    <span className="glow-icon">●</span>
                    <span className="glow-text">{st.key}</span>
                    <span className="glow-time">{timeOffset}</span>
                  </div>
                ) : (
                  <div className="stage-dot-wrapper">
                    <div className="stage-node-dot" />
                    <span className="stage-name-label">{st.key}</span>
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
