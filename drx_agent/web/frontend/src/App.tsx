import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { ReactFlow, Background, MiniMap } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { nodeTypes } from './nodes';
import TopReplayBanner from './TopReplayBanner';
import ActionStream from './ActionStream';
import SessionSidebar from './SessionSidebar.tsx';
import ReplayDock from './ReplayDock.tsx';
import NodeInspector from './NodeInspector.tsx';
import { ArrowClockwise, ArrowsDownUp, Circle, CaretLeft, CaretRight, CornersOut, Crosshair, Info, MapTrifold, Minus, Pause, Play, Plus, WarningCircle } from '@phosphor-icons/react';
import { eventPresentation } from './presentation.ts';
import { useSessionList, useSessionGraph } from './useRemoteJSON.ts';
import type { ReactFlowInstance, NodeChange } from '@xyflow/react';
import type { GraphResponse, ReplayNode, ReplayEdge, NodePositions, NodeMeasurements, SessionListItem, RemoteStatus } from './types.ts';
import { projectReplay, isReplayShortcut, NODE_WIDTH, NODE_HEIGHT } from './replay.ts';

const EMPTY: never[] = [];
const EMPTY_GRAPH: GraphResponse = {
  nodes: [], edges: [], actions: [], timeline: [], stages: [],
  summary: { sessionId: '', name: null, createdAt: null, targetHost: null, targetUrl: null, targetNotes: null,
    totalActions: 0, totalStages: 0, targetsCount: 0, findingsCount: 0, verifiedFindingsCount: 0, credsCount: 0,
    targets: [], findings: [], creds: [] },
};

export default function App() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [listRetry, setListRetry] = useState(0);
  const list = useSessionList(listRetry);
  const sessions = list.data ?? EMPTY;
  const selectedSessionId = sessions.some((session) => session.id === selectedId)
    ? selectedId : sessions[0]?.id ?? null;
  return (
    <ReplayWorkspace
      key={selectedSessionId ?? 'no-session'}
      sessions={sessions}
      selectedSessionId={selectedSessionId}
      onSelectSession={setSelectedId}
      listStatus={list.status}
      listError={list.error}
      onRetryList={() => setListRetry((value) => value + 1)}
    />
  );
}

interface WorkspaceProps {
  sessions: SessionListItem[];
  selectedSessionId: string | null;
  onSelectSession: (id: string) => void;
  listStatus: RemoteStatus;
  listError: string;
  onRetryList: () => void;
}

function ReplayWorkspace({ sessions, selectedSessionId, onSelectSession, listStatus, listError, onRetryList }: WorkspaceProps) {
  const [retry, setRetry] = useState(0);
  const session = useSessionGraph(selectedSessionId, retry);
  const graph = session.data ?? EMPTY_GRAPH;
  const { actions, stages, summary } = graph;
  const [step, setStep] = useState(0);
  const currentStep = Math.max(0, Math.min(step, actions.length - 1));
  const currentAction = actions[currentStep] ?? null;
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [isLoop, setIsLoop] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [sessionsVisible, setSessionsVisible] = useState(() => window.innerWidth >= 1100);
  const [panelView, setPanelView] = useState<'stream' | 'findings'>('stream');
  const [mapVisible, setMapVisible] = useState(false);
  const [layout, setLayout] = useState<'vertical' | 'wrap'>('vertical');
  const [sidebarVisible, setSidebarVisible] = useState(() => window.innerWidth >= 850);
  const [follow, setFollow] = useState(true);
  const [positions, setPositions] = useState<NodePositions>({});
  const [measurements, setMeasurements] = useState<NodeMeasurements>({});
  const [flow, setFlow] = useState<ReactFlowInstance<ReplayNode, ReplayEdge> | null>(null);
  const [canvasWidth, setCanvasWidth] = useState(900);
  const [canvasHeight, setCanvasHeight] = useState(600);
  const [isFullscreen, setIsFullscreen] = useState(Boolean(document.fullscreenElement));
  const [fullscreenError, setFullscreenError] = useState('');
  const canvasRef = useRef<HTMLDivElement>(null);
  const loading = listStatus === 'loading' || session.status === 'loading';
  const error = listError || session.error;
  const disabled = loading || Boolean(error) || !actions.length;
  const hasActions = actions.length > 0;
  const openNode = useCallback((value: number) => {
    setStep(value);
    setIsPlaying(false);
    setInspectorOpen(true);
  }, []);
  const projected = useMemo(() => {
    const projection = projectReplay(graph, currentStep, canvasWidth, positions, measurements, layout);
    return { ...projection, nodes: projection.nodes.map((node) => {
      const action = actions[node.data.step];
      const presentation = action ? eventPresentation(action) : null;
      return { ...node, focusable: false,
        data: { ...node.data, color: presentation?.color ?? node.data.color,
          kind: action?.kind, category: presentation?.label,
          title: action?.tool ?? node.data.title, onOpen: () => openNode(node.data.step) },
      };
    }) };
  }, [graph, actions, currentStep, canvasWidth, positions, measurements, openNode, layout]);
  const focusNode = projected.nodes.find((node) => node.id === currentAction?.cardId);

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setCanvasWidth(entry.contentRect.width);
      setCanvasHeight(entry.contentRect.height);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [loading, error, hasActions]);

  const locateCurrent = useCallback(() => {
    if (!flow || !focusNode) return;
    const duration = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 250;
    const reservedWidth = inspectorOpen && canvasWidth > 620 ? 330 : 0;
    flow.setViewport({ x: (canvasWidth + reservedWidth) / 2 - focusNode.position.x - NODE_WIDTH / 2,
      y: Math.max(140, (canvasHeight - 100) / 2) - focusNode.position.y - NODE_HEIGHT / 2, zoom: 1 }, { duration });
  }, [flow, focusNode, inspectorOpen, canvasWidth, canvasHeight]);

  useEffect(() => {
    if (follow) locateCurrent();
  }, [follow, canvasWidth, canvasHeight, locateCurrent]);

  // Replay advances one saved record per beat; displayed timestamps remain the original times.
  useEffect(() => {
    if (!isPlaying || disabled) return undefined;
    const timer = setTimeout(() => {
      if (currentStep < actions.length - 1) setStep(currentStep + 1);
      else if (isLoop && actions.length > 1) setStep(0);
      else setIsPlaying(false);
    }, 1000 / speed);
    return () => clearTimeout(timer);
  }, [isPlaying, disabled, currentStep, actions.length, isLoop, speed]);

  const selectStep = useCallback((value: number) => {
    if (!Number.isFinite(value) || !actions.length) return;
    setStep(Math.max(0, Math.min(Math.round(value), actions.length - 1)));
    setIsPlaying(false);
  }, [actions.length]);

  const togglePlay = useCallback((playing: boolean) => {
    if (disabled) return;
    if (playing && currentStep === actions.length - 1) setStep(0);
    setIsPlaying(playing);
  }, [disabled, currentStep, actions.length]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (disabled || !isReplayShortcut(event)) return;
      if (event.code === 'Space') {
        event.preventDefault();
        togglePlay(!isPlaying);
      } else if (event.code === 'ArrowLeft' || event.code === 'ArrowRight') {
        event.preventDefault();
        selectStep(currentStep + (event.code === 'ArrowLeft' ? -1 : 1));
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [disabled, togglePlay, isPlaying, currentStep, selectStep]);

  useEffect(() => {
    const update = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', update);
    return () => document.removeEventListener('fullscreenchange', update);
  }, []);

  const toggleFullscreen = async () => {
    setFullscreenError('');
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      setFullscreenError('全屏切换失败，请重试。');
    }
  };

  const onNodesChange = useCallback((changes: NodeChange<ReplayNode>[]) => {
    const measured: NodeMeasurements = {};
    const moved: NodePositions = {};
    for (const change of changes) {
      if (change.type === 'dimensions' && change.dimensions) measured[change.id] = change.dimensions;
      if (change.type === 'position' && change.position) moved[change.id] = change.position;
    }
    if (Object.keys(measured).length) setMeasurements((old) => {
      const changed = Object.entries(measured).filter(([id, dimensions]) => old[id]?.width !== dimensions.width || old[id]?.height !== dimensions.height);
      if (!changed.length) return old;
      return { ...old, ...Object.fromEntries(changed) };
    });
    if (!Object.keys(moved).length) return;
    setFollow(false);
    setPositions((old) => ({ ...old, ...Object.fromEntries(Object.entries(moved).map(([id, position]) => [`${projected.columns}:${id}`, position])) }));
  }, [projected.columns]);

  const sessionName = summary.name ?? sessions.find((item) => item.id === selectedSessionId)?.name ?? selectedSessionId;
  const closeInspector = useCallback(() => setInspectorOpen(false), []);
  const changeView = (view: 'stream' | 'findings') => {
    setPanelView(view);
    setSidebarVisible(true);
    if (view === 'findings') setInspectorOpen(false);
  };
  const playbackLabel = !actions.length ? '无回放记录' : isPlaying ? '回放中' : currentStep === actions.length - 1 ? '回放结束' : '回放已暂停';

  return (
    <div className="drx-replay-app">
      <TopReplayBanner sessionName={listStatus === 'loading' ? '正在读取' : listError ? '列表未加载' : sessionName} sessionCount={listStatus === 'ready' ? sessions.length : null}
        sessionsVisible={sessionsVisible} activityVisible={sidebarVisible} view={panelView} loading={loading}
        isFullscreen={isFullscreen} onToggleSessions={() => setSessionsVisible((value) => !value)}
        onToggleActivity={() => setSidebarVisible((value) => !value)} onViewChange={changeView}
        onRefresh={onRetryList} onToggleFullscreen={toggleFullscreen} />
      {fullscreenError && <div className="app-notice" role="alert">{fullscreenError}</div>}
      <div className={`workspace-body ${sessionsVisible ? '' : 'sessions-collapsed'} ${sidebarVisible ? '' : 'activity-collapsed'}`}>
        {sessionsVisible && <SessionSidebar sessions={sessions} selectedSessionId={selectedSessionId} onSelectSession={onSelectSession}
          loading={listStatus === 'loading'} error={listError} onRefresh={onRetryList} onClose={() => setSessionsVisible(false)} />}
        <main className="canvas-stage" aria-label="会话记录图">
          <div className="graph-surface" ref={canvasRef}>
            {loading || error || !selectedSessionId || !actions.length ? (
              <div className="workspace-empty" role={error ? 'alert' : 'status'}>
                {error ? <WarningCircle size={34} /> : loading ? <ArrowClockwise size={30} className="is-spinning" /> : <Info size={34} />}
                <h2>{loading ? '正在读取会话' : error || (selectedSessionId ? '无动作记录' : '无会话')}</h2>
                <p>{loading ? '正在加载已保存的数据…' : error ? '原始记录保持不变，请重试或切换会话。' : selectedSessionId ? '此会话没有可回放的动作。' : '已保存的会话会显示在左侧列表。'}</p>
                {!loading && <button className="header-button" onClick={listError || !selectedSessionId ? onRetryList : () => setRetry((value) => value + 1)}><ArrowClockwise size={15} />重新加载</button>}
              </div>
            ) : (
              <ReactFlow<ReplayNode, ReplayEdge> nodes={projected.nodes} edges={projected.edges} nodeTypes={nodeTypes}
                onInit={setFlow} onNodesChange={onNodesChange} onMoveStart={(event) => { if (event) setFollow(false); }}
                onNodeClick={(_event, node) => openNode(node.data.step)}
                nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
                onlyRenderVisibleElements minZoom={0.001} maxZoom={2} proOptions={{ hideAttribution: true }}>
                <Background color="#29384e" gap={24} size={1} />
                {mapVisible && <MiniMap nodeColor={(node) => typeof node.data.color === 'string' ? node.data.color : '#64748b'} maskColor="rgba(7, 13, 24, 0.8)" pannable zoomable />}
              </ReactFlow>
            )}
          </div>
          <div className="canvas-context"><span>会话记录图</span><span>记录顺序</span><span>{loading || error ? '未读取' : `${projected.nodes.length} / ${actions.length}`}</span></div>
          {!loading && !error && <div className={`canvas-playback-state ${isPlaying ? 'is-playing' : ''}`} aria-live="polite">{isPlaying ? <Play size={13} weight="fill" /> : <Pause size={13} />}<span>{playbackLabel}</span></div>}
          <div className="graph-tools" role="group" aria-label="图谱操作">
            <button className="icon-button" disabled={disabled} aria-label="放大图谱" title="放大" onClick={() => { setFollow(false); void flow?.zoomIn({ duration: 160 }); }}><Plus size={17} /></button>
            <button className="icon-button" disabled={disabled} aria-label="缩小图谱" title="缩小" onClick={() => { setFollow(false); void flow?.zoomOut({ duration: 160 }); }}><Minus size={17} /></button>
            <button className="icon-button" disabled={disabled} aria-label="查看全图" title="查看全图" onClick={() => { setFollow(false); void flow?.fitView({ includeHiddenNodes: true, minZoom: 0.001, padding: 0.22, duration: 220 }); }}><CornersOut size={18} /></button>
            <button className={`icon-button ${layout === 'vertical' ? 'is-active' : ''}`} disabled={disabled} aria-label={layout === 'vertical' ? '切换折行布局' : '切换纵向布局'} title={layout === 'vertical' ? '折行布局' : '纵向布局'} onClick={() => { setLayout((value) => value === 'vertical' ? 'wrap' : 'vertical'); setFollow(true); }}><ArrowsDownUp size={18} /></button>
            <div className="tool-separator" />
            <button className={`icon-button ${follow ? 'is-active' : ''}`} disabled={disabled} aria-label={follow ? '关闭跟随当前记录' : '跟随当前记录'} aria-pressed={follow} title="跟随当前记录" onClick={() => setFollow((value) => !value)}><Crosshair size={18} /></button>
            <button className={`icon-button ${mapVisible ? 'is-active' : ''}`} disabled={disabled} aria-label="显示缩略图" aria-pressed={mapVisible} title="缩略图" onClick={() => setMapVisible((value) => !value)}><MapTrifold size={18} /></button>
          </div>
          <button className="panel-handle panel-handle-left" aria-label={sessionsVisible ? '收起会话列表' : '展开会话列表'} aria-expanded={sessionsVisible} onClick={() => setSessionsVisible((value) => !value)}>{sessionsVisible ? <CaretLeft size={14} /> : <CaretRight size={14} />}</button>
          <button className="panel-handle panel-handle-right" aria-label={sidebarVisible ? '收起事件日志' : '展开事件日志'} aria-expanded={sidebarVisible} onClick={() => setSidebarVisible((value) => !value)}>{sidebarVisible ? <CaretRight size={14} /> : <CaretLeft size={14} />}</button>
          {!mapVisible && <div className="graph-legend" aria-label="记录状态图例"><span>状态图例</span><span><Circle size={8} weight="fill" className="legend-complete" />已完成</span><span><Circle size={8} weight="fill" className="legend-running" />进行中</span><span><Circle size={8} weight="fill" className="legend-error" />失败 / 拒绝</span><span><Circle size={8} weight="fill" className="legend-unknown" />无状态记录</span></div>}
          {inspectorOpen && currentAction && !loading && !error && <NodeInspector action={currentAction} sessionId={summary.sessionId} onClose={closeInspector} />}
          <ReplayDock currentAction={currentAction} currentStep={currentStep} totalSteps={actions.length} stages={stages}
            pendingLabel={loading ? '正在读取记录…' : error ? '记录未加载' : null}
            isPlaying={isPlaying && !disabled} speed={speed} isLoop={isLoop} disabled={disabled}
            onTogglePlay={togglePlay} onSetSpeed={setSpeed} onToggleLoop={() => setIsLoop((value) => !value)}
            onReset={() => { selectStep(0); setFollow(true); }} onStepChange={selectStep} />
        </main>
        {sidebarVisible && <div className="workspace-activity" id="session-sidebar">
          {loading || error ? <div className="workspace-empty"><Info size={24} /><p>{loading ? '正在加载记录…' : '会话记录未加载'}</p></div> :
            <ActionStream actions={actions} currentStep={currentStep} onSelectStep={selectStep} onOpenAction={openNode}
              summary={summary} view={panelView} onViewChange={setPanelView} />}
        </div>}
      </div>
    </div>
  );
}
