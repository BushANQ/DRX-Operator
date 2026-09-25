import { useState, useRef, useEffect } from 'react';

const SPEED_OPTIONS = [0.5, 1, 2, 4];
const display = (value) => value == null || value === '' ? '无' : String(value);

function sessionDate(value) {
  if (value == null || value === '') return '无';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '无' : date.toLocaleString('zh-CN');
}

export default function TopReplayBanner({
  sessionName,
  sessions = [],
  selectedSessionId,
  onSelectSession,
  currentAction,
  currentStep = 0,
  totalSteps = 0,
  stages = [],
  activeStageKey,
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
  disabled = false,
  loading = false,
}) {
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef(null);
  const dropdownButtonRef = useRef(null);
  const stepCount = Number.isInteger(totalSteps) && totalSteps > 0 ? totalSteps : 0;
  const stepIndex = stepCount ? Math.max(0, Math.min(currentStep, stepCount - 1)) : 0;
  const displayedStep = stepCount ? stepIndex + 1 : 0;
  const progressPercent = stepCount ? Math.round(displayedStep / stepCount * 100) : 0;
  const controlsDisabled = disabled || loading || stepCount === 0;

  useEffect(() => {
    if (!dropdownOpen) return;
    function handleOutsideClick(event) {
      if (!dropdownRef.current?.contains(event.target)) setDropdownOpen(false);
    }
    function handleEscape(event) {
      if (event.key === 'Escape') {
        setDropdownOpen(false);
        dropdownButtonRef.current?.focus();
      }
    }
    document.addEventListener('pointerdown', handleOutsideClick);
    document.addEventListener('keydown', handleEscape);
    return () => {
      document.removeEventListener('pointerdown', handleOutsideClick);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [dropdownOpen]);

  return (
    <div className="top-replay-dashboard" aria-busy={loading}>
      <header className="top-navbar">
        <div className="nav-left">
          <div className="nav-brand-badge">会话回放</div>
          <div className="nav-breadcrumb">
            <span className="nav-breadcrumb-prefix">会话 ·</span>
            <div className="nav-session-selector" ref={dropdownRef}>
              <button
                type="button"
                className="nav-session-btn"
                ref={dropdownButtonRef}
                onClick={() => setDropdownOpen((open) => !open)}
                aria-expanded={dropdownOpen}
                aria-controls="session-picker"
                aria-label={`切换会话，当前会话：${display(sessionName)}`}
                disabled={sessions.length === 0}
              >
                <span>{display(sessionName)}</span>
                <span className="dropdown-arrow" aria-hidden="true">▾</span>
              </button>
              {dropdownOpen && (
                <div id="session-picker" className="session-dropdown-menu animate-fade-in">
                  <div className="session-dropdown-header">选择会话</div>
                  {sessions.map((session) => (
                    <button
                      type="button"
                      key={session.id}
                      className={`session-dropdown-item ${session.id === selectedSessionId ? 'active' : ''}`}
                      aria-pressed={session.id === selectedSessionId}
                      onClick={() => {
                        onSelectSession(session.id);
                        setDropdownOpen(false);
                        dropdownButtonRef.current?.focus();
                      }}
                    >
                      <span className="session-item-name">{display(session.name || session.id)}</span>
                      <span className="session-item-meta">创建时间：{sessionDate(session.created_at)}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="nav-right">
          <div className="nav-progress-percent" aria-label={`回放进度 ${progressPercent}%`}>{progressPercent}%</div>
          <button type="button" className="nav-fullscreen-btn" onClick={onToggleFullscreen}>
            {isFullscreen ? '退出全屏' : '全屏'}
          </button>
        </div>
      </header>

      <div className="replay-stage-banner">
        <div className="replay-stage-left">
          <div className="replay-phase-caption">{loading ? '正在读取会话记录' : '当前记录'}</div>
          <div className="replay-phase-heading">
            <span className="replay-stage-main-title">{display(currentAction?.title)}</span>
            <span className="replay-status-pill">阶段：{display(currentAction?.stageTitle || activeStageKey)}</span>
          </div>
        </div>
      </div>

      <div className="replay-glowing-track" aria-hidden="true">
        <div className="replay-glowing-fill" style={{ width: `${progressPercent}%` }} />
      </div>

      <div className="replay-controls-bar">
        <div className="controls-button-group">
          <button type="button" className="ctrl-btn primary-glow" disabled={controlsDisabled} onClick={() => onTogglePlay(!isPlaying)}>
            {isPlaying ? '暂停' : '播放'}
          </button>
          <button type="button" className="ctrl-btn secondary" disabled={controlsDisabled} onClick={onReset}>重来</button>
          <button
            type="button"
            className={`ctrl-btn secondary ${isLoop ? 'active-loop' : ''}`}
            disabled={controlsDisabled}
            aria-pressed={isLoop}
            onClick={onToggleLoop}
          >循环</button>
          <div className="speed-selector" aria-label="播放速度">
            {SPEED_OPTIONS.map((option) => (
              <button
                type="button"
                key={option}
                className={`speed-btn ${speed === option ? 'active' : ''}`}
                disabled={controlsDisabled}
                aria-label={`${option} 倍速`}
                aria-pressed={speed === option}
                onClick={() => onSetSpeed(option)}
              >{option}x</button>
            ))}
          </div>
        </div>
        <div className="slider-wrapper">
          <input
            type="range"
            min={0}
            max={Math.max(0, stepCount - 1)}
            value={stepIndex}
            disabled={controlsDisabled}
            onChange={(event) => onStepChange(Number(event.target.value))}
            className="custom-range-slider"
            aria-label="回放进度"
            aria-valuetext={`第 ${displayedStep} 条，共 ${stepCount} 条`}
          />
        </div>
        <div className="replay-timer-label">时间：{display(currentAction?.timeOffset)} · {displayedStep}/{stepCount}</div>
      </div>

      <div className="stage-activity-container">
        <div className="stage-activity-title">阶段记录</div>
        <div className="stage-milestone-track">
          {stages.length === 0 && <span className="empty-state">无</span>}
          {stages.map((stage) => {
            const isActive = stage.key === activeStageKey;
            const firstIndex = stage.firstActionIndex;
            const canJump = Number.isInteger(firstIndex) && firstIndex >= 0 && firstIndex < stepCount;
            const count = Number.isInteger(stage.count) && stage.count >= 0 ? stage.count : null;
            return (
              <button
                type="button"
                key={stage.key}
                className={`stage-milestone-step ${isActive ? 'active' : ''}`}
                disabled={controlsDisabled || !canJump}
                aria-current={isActive ? 'step' : undefined}
                aria-label={`${display(stage.title || stage.key)}，${display(count)} 条记录`}
                title={`${display(stage.title || stage.key)} · ${display(count)} 条记录`}
                onClick={() => { if (canJump) onStepChange(firstIndex); }}
              >
                <div className="mini-histogram" aria-hidden="true">
                  <div className={`mini-bar ${isActive ? 'active-bar' : ''}`} style={{ height: `${Math.min(24, (count ?? 0) * 3)}px` }} />
                </div>
                <div className={isActive ? 'active-stage-glow-pill' : 'stage-dot-wrapper'}>
                  <span className={isActive ? 'glow-icon' : 'stage-node-dot'} aria-hidden="true">{isActive ? '●' : ''}</span>
                  <span className={isActive ? 'glow-text' : 'stage-name-label'}>{display(stage.key)}</span>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
