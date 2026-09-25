import { ArrowCounterClockwise, CaretRight, Pause, Play, Repeat } from '@phosphor-icons/react';
import type { ReplayAction, ReplayStage } from './types.ts';

interface ReplayDockProps {
  currentAction: ReplayAction | null;
  currentStep: number;
  totalSteps: number;
  stages: ReplayStage[];
  isPlaying: boolean;
  speed: number;
  isLoop: boolean;
  disabled: boolean;
  onTogglePlay: (playing: boolean) => void;
  onSetSpeed: (speed: number) => void;
  onToggleLoop: () => void;
  onReset: () => void;
  onStepChange: (step: number) => void;
}

export default function ReplayDock({ currentAction, currentStep, totalSteps, stages, isPlaying, speed,
  isLoop, disabled, onTogglePlay, onSetSpeed, onToggleLoop, onReset, onStepChange }: ReplayDockProps) {
  const blocked = disabled || totalSteps === 0;
  const index = totalSteps ? Math.min(currentStep + 1, totalSteps) : 0;
  const percent = totalSteps ? index / totalSteps * 100 : 0;
  return (
    <section className="replay-dock" aria-label="会话回放控制">
      <div className="replay-dock-head">
        <span className="replay-label"><CaretRight size={13} weight="fill" />回放</span>
        <span className="replay-current-title" title={currentAction?.title ?? '无动作记录'}>{currentAction?.title ?? '无动作记录'}</span>
        <span className="replay-count">{index}<span> / {totalSteps}</span></span>
      </div>
      <div className="replay-dock-controls">
        <button className="replay-play" disabled={blocked} aria-label={isPlaying ? '暂停回放' : '播放回放'} onClick={() => onTogglePlay(!isPlaying)}>{isPlaying ? <Pause size={17} weight="fill" /> : <Play size={17} weight="fill" />}</button>
        <button className="icon-button" disabled={blocked} aria-label="重新回放" onClick={onReset}><ArrowCounterClockwise size={16} /></button>
        <button className={`icon-button ${isLoop ? 'is-active' : ''}`} disabled={blocked} aria-label="循环回放" aria-pressed={isLoop} onClick={onToggleLoop}><Repeat size={17} /></button>
        <label className="replay-speed"><span className="sr-only">播放速度</span><select value={speed} disabled={blocked} onChange={(event) => onSetSpeed(Number(event.target.value))}>{[0.5, 1, 2, 4].map((value) => <option key={value} value={value}>{value}x</option>)}</select></label>
        <div className="replay-slider-wrap"><div className="replay-slider-track" aria-hidden="true"><span style={{ width: `${percent}%` }} /></div><input type="range" min={0} max={Math.max(0, totalSteps - 1)} value={totalSteps ? currentStep : 0} disabled={blocked} onChange={(event) => onStepChange(Number(event.target.value))} aria-label="回放进度" aria-valuetext={`第 ${index} 条，共 ${totalSteps} 条`} /></div>
        <span className="replay-time">{currentAction?.timeOffset ?? '时间：无'}</span>
        <label className="replay-stage-select"><span className="sr-only">跳转阶段</span><select disabled={blocked || !stages.length} value={currentAction?.stageKey ?? ''} onChange={(event) => { const stage = stages.find((item) => item.key === event.target.value); if (stage?.firstActionIndex != null) onStepChange(stage.firstActionIndex); }}><option value="" disabled>阶段：无</option>{stages.map((stage) => <option key={stage.key} value={stage.key} disabled={stage.firstActionIndex == null}>{stage.title}</option>)}</select></label>
      </div>
    </section>
  );
}
