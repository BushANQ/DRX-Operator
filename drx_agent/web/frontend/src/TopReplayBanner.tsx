import { ArrowClockwise, ArrowsIn, ArrowsOut, TreeStructure, Graph, Sidebar, Terminal } from '@phosphor-icons/react';

interface HeaderProps {
  sessionName: string | null;
  sessionCount: number | null;
  sessionsVisible: boolean;
  activityVisible: boolean;
  view: 'execution' | 'causal';
  loading: boolean;
  isFullscreen: boolean;
  onToggleSessions: () => void;
  onToggleActivity: () => void;
  onViewChange: (view: 'execution' | 'causal') => void;
  onRefresh: () => void;
  onToggleFullscreen: () => void;
}

export default function TopReplayBanner({ sessionName, sessionCount, sessionsVisible, activityVisible, view,
  loading, isFullscreen, onToggleSessions, onToggleActivity, onViewChange, onRefresh, onToggleFullscreen }: HeaderProps) {
  return (
    <header className="workspace-header">
      <div className="workspace-brand"><Graph size={23} weight="duotone" /><span>DRX<span className="brand-secondary">OPERATOR</span></span></div>
      <div className="header-divider" />
      <button className="icon-button header-mobile-menu" aria-label={sessionsVisible ? '收起会话列表' : '展开会话列表'} aria-expanded={sessionsVisible} onClick={onToggleSessions}><Sidebar size={19} /></button>
      <nav className="workspace-views" aria-label="工作区视图">
        <button className={view === 'execution' ? 'is-active' : ''} aria-pressed={view === 'execution'} onClick={() => onViewChange('execution')}><Graph size={16} />执行图</button>
        <button className={view === 'causal' ? 'is-active' : ''} aria-pressed={view === 'causal'} onClick={() => onViewChange('causal')}><TreeStructure size={16} />因果图</button>
      </nav>
      <div className="header-session" title={sessionName ?? '无会话'}><span className="header-session-caption">已保存会话</span><span>{sessionName ?? '无会话'}</span></div>
      <div className="workspace-header-actions">
        <span className="header-records">{sessionCount == null ? '会话列表未读取' : `${sessionCount} 个会话`}</span>
        <button className="header-button" disabled={loading} onClick={onRefresh} aria-label="刷新会话"><ArrowClockwise size={16} className={loading ? 'is-spinning' : ''} /><span>刷新</span></button>
        <button className={`icon-button ${activityVisible ? 'is-active' : ''}`} aria-label={activityVisible ? '收起事件日志' : '展开事件日志'} aria-expanded={activityVisible} onClick={onToggleActivity}><Terminal size={18} /></button>
        <button className="icon-button" aria-label={isFullscreen ? '退出全屏' : '全屏'} onClick={onToggleFullscreen}>{isFullscreen ? <ArrowsIn size={18} /> : <ArrowsOut size={18} />}</button>
      </div>
    </header>
  );
}
