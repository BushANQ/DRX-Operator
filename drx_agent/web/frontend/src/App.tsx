import { useState, useCallback, useEffect, useMemo, useRef, useLayoutEffect } from 'react';
import { ReactFlow, Background, MiniMap } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import SemanticEdge from './SemanticEdge.tsx';
import SemanticNodeCard, { SemanticKindChip } from './SemanticNodeCard.tsx';
import { projectSemanticGraph, expandAncestorsForAction, SEMANTIC_NODE_WIDTH, SEMANTIC_NODE_HEIGHT } from './semanticLayout.ts';
import type { SemanticNode, SemanticGraph, SemanticFlowNode, SemanticFlowEdge, SemanticPositions, SemanticMeasurements } from './semanticTypes.ts';
import TopReplayBanner from './TopReplayBanner';
import ActionStream from './ActionStream';
import SessionSidebar from './SessionSidebar.tsx';
import ReplayDock from './ReplayDock.tsx';
import NodeInspector from './NodeInspector.tsx';
import { ArrowClockwise, TreeStructure, Circle, CaretLeft, CaretRight, CornersOut, Crosshair, Info, MapTrifold, Minus, Pause, Play, Plus, WarningCircle } from '@phosphor-icons/react';
import { useSessionList, useSessionGraph } from './useRemoteJSON.ts';
import type { ReactFlowInstance, NodeChange } from '@xyflow/react';
import type { GraphResponse, SessionListItem, RemoteStatus } from './types.ts';
import { overviewViewport, isReplayShortcut } from './replay.ts';

const EMPTY: never[] = [];
const EMPTY_SEMANTIC: SemanticGraph = { nodes: [], edges: [] };
const semanticNodeTypes = { semanticNode: SemanticNodeCard };
const semanticEdgeTypes = { semanticEdge: SemanticEdge };
const EMPTY_GRAPH: GraphResponse = {
  nodes: [], edges: [], actions: [], timeline: [], stages: [], graphs: null,
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
  const [step, setStep] = useState<number | null>(null);
  const currentStep = Math.max(0, Math.min(step ?? actions.length - 1, actions.length - 1));
  const currentAction = actions[currentStep] ?? null;
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [isLoop, setIsLoop] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [sessionsVisible, setSessionsVisible] = useState(() => window.innerWidth >= 1100);
  const [panelView, setPanelView] = useState<'stream' | 'findings'>('stream');
  const [mapVisible, setMapVisible] = useState(false);
  const [graphView, setGraphView] = useState<'execution' | 'causal'>('execution');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const semanticGraph = graph.graphs?.[graphView] ?? EMPTY_SEMANTIC;
  const [sidebarVisible, setSidebarVisible] = useState(() => window.innerWidth >= 850);
  const [follow, setFollow] = useState(true);
  const [positions, setPositions] = useState<SemanticPositions>({});
  const [measurements, setMeasurements] = useState<SemanticMeasurements>({});
  const [flow, setFlow] = useState<ReactFlowInstance<SemanticFlowNode, SemanticFlowEdge> | null>(null);
  const [canvasWidth, setCanvasWidth] = useState(900);
  const [canvasHeight, setCanvasHeight] = useState(600);
  const [isFullscreen, setIsFullscreen] = useState(Boolean(document.fullscreenElement));
  const [fullscreenError, setFullscreenError] = useState('');
  const canvasRef = useRef<HTMLDivElement>(null);
  const collapseAnchor = useRef<{ id: string; x: number; y: number; zoom: number } | null>(null);
  useEffect(() => {
    const tablet = window.matchMedia('(max-width: 1099px)');
    const mobile = window.matchMedia('(max-width: 849px)');
    const collapsePanels = () => {
      if (tablet.matches) setSessionsVisible(false);
      if (mobile.matches) setSidebarVisible(false);
    };
    tablet.addEventListener('change', collapsePanels);
    mobile.addEventListener('change', collapsePanels);
    return () => {
      tablet.removeEventListener('change', collapsePanels);
      mobile.removeEventListener('change', collapsePanels);
    };
  }, []);
  const loading = listStatus === 'loading' || session.status === 'loading';
  const error = listError || session.error;
  const disabled = loading || Boolean(error) || !actions.length;
  const hasGraph = semanticGraph.nodes.length > 0;
  const graphDisabled = loading || Boolean(error) || !hasGraph;
  const graphMissing = session.status === 'ready' && graph.graphs === null;
  const openAction = useCallback((value: number) => {
    const action = actions[value];
    if (!action) return;
    setStep(value);
    setSelectedNodeId(null);
    setIsPlaying(false);
    setInspectorOpen(true);
    setFollow(true);
    setCollapsed((old) => expandAncestorsForAction(semanticGraph, action.id, old));
    if (window.innerWidth < 850) setSidebarVisible(false);
  }, [actions, semanticGraph]);
  const openEntity = useCallback((node: SemanticNode) => {
    if (node.actionId !== null && node.step !== null) setStep(node.step);
    setSelectedNodeId(node.id);
    setInspectorOpen(true);
    setIsPlaying(false);
    setFollow(false);
    if (window.innerWidth < 850) setSidebarVisible(false);
  }, []);
  const toggleBranch = useCallback((id: string) => {
    const node = flow?.getNode(id);
    const viewport = flow?.getViewport();
    if (node && viewport) collapseAnchor.current = { id, x: node.position.x * viewport.zoom + viewport.x, y: node.position.y * viewport.zoom + viewport.y, zoom: viewport.zoom };
    setFollow(false);
    setCollapsed((old) => { const next = new Set(old); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  }, [flow]);
  const projected = useMemo(() => {
    const projection = projectSemanticGraph(semanticGraph, currentStep, collapsed, positions, measurements);
    return { ...projection, nodes: projection.nodes.map((node) => ({
      ...node, focusable: false,
      data: { ...node.data, onOpen: () => openEntity(node.data), onToggle: () => toggleBranch(node.id) },
    })) };
  }, [semanticGraph, currentStep, collapsed, positions, measurements, openEntity, toggleBranch]);
  const focusNode = projected.nodes.find((node) => projected.currentNodeIds.includes(node.id));
  useLayoutEffect(() => {
    const anchor = collapseAnchor.current;
    if (!anchor || !flow) return;
    const node = projected.nodes.find((item) => item.id === anchor.id);
    collapseAnchor.current = null;
    if (node) void flow.setViewport({ x: anchor.x - node.position.x * anchor.zoom, y: anchor.y - node.position.y * anchor.zoom, zoom: anchor.zoom }, { duration: 0 });
  }, [flow, projected.nodes]);
  const inspectorNode = selectedNodeId ? semanticGraph.nodes.find((node) => node.id === selectedNodeId) ?? null
    : semanticGraph.nodes.find((node) => node.actionId === currentAction?.id) ?? null;
  const inspectorAction = selectedNodeId ? actions.find((action) => action.id === inspectorNode?.actionId) ?? null : currentAction;
  const initialView = useRef('');

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setCanvasWidth(entry.contentRect.width);
      setCanvasHeight(entry.contentRect.height);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [loading, error, hasGraph]);

  const locateCurrent = useCallback(() => {
    if (!flow || !focusNode) return;
    const duration = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 250;
    const reservedWidth = inspectorOpen && canvasWidth > 620 ? 330 : 0;
    flow.setViewport({ x: (canvasWidth + reservedWidth) / 2 - focusNode.position.x - (focusNode.width ?? SEMANTIC_NODE_WIDTH) / 2,
      y: Math.max(140, (canvasHeight - 100) / 2) - focusNode.position.y - (focusNode.height ?? SEMANTIC_NODE_HEIGHT) / 2, zoom: 1 }, { duration });
  }, [flow, focusNode, inspectorOpen, canvasWidth, canvasHeight]);

  useEffect(() => {
    if (!flow || !projected.nodes.length) return;
    const context = `${selectedSessionId}:${graphView}`;
    if (initialView.current !== context || (follow && step === null)) {
      initialView.current = context;
      const viewport = overviewViewport(projected.nodes, canvasWidth, canvasHeight, false);
      if (viewport) {
        if (graphView === 'execution' && viewport.zoom < 0.55) {
          const root = projected.nodes.find((node) => node.data.kind === 'session') ?? projected.nodes[0];
          viewport.zoom = 0.55;
          viewport.x = canvasWidth / 2 - (root.position.x + (root.width ?? SEMANTIC_NODE_WIDTH) / 2) * viewport.zoom;
          viewport.y = 115 - root.position.y * viewport.zoom;
        }
        void flow.setViewport(viewport, { duration: 0 });
      }
    } else if (follow && step !== null) locateCurrent();
  }, [flow, projected.nodes, selectedSessionId, graphView, follow, step, canvasWidth, canvasHeight, locateCurrent]);

  // Replay advances one saved record per beat; displayed timestamps remain the original times.
  useEffect(() => {
    if (!isPlaying || disabled) return undefined;
    const timer = setTimeout(() => {
      const next = currentStep < actions.length - 1 ? currentStep + 1 : isLoop && actions.length > 1 ? 0 : null;
      if (next === null) setIsPlaying(false);
      else {
        setStep(next);
        const action = actions[next];
        if (action && follow) setCollapsed((old) => expandAncestorsForAction(semanticGraph, action.id, old));
      }
    }, 1000 / speed);
    return () => clearTimeout(timer);
  }, [isPlaying, disabled, currentStep, actions, isLoop, speed, follow, semanticGraph]);

  const selectStep = useCallback((value: number) => {
    if (!Number.isFinite(value) || !actions.length) return;
    const next = Math.max(0, Math.min(Math.round(value), actions.length - 1));
    setStep(next);
    setSelectedNodeId(null);
    setIsPlaying(false);
    const action = actions[next];
    if (action && follow) setCollapsed((old) => expandAncestorsForAction(semanticGraph, action.id, old));
  }, [actions, semanticGraph, follow]);

  const togglePlay = useCallback((playing: boolean) => {
    if (disabled) return;
    if (playing && currentStep === actions.length - 1) setStep(0);
    setSelectedNodeId(null);
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

  const onNodesChange = useCallback((changes: NodeChange<SemanticFlowNode>[]) => {
    const measured: SemanticMeasurements = {};
    const moved: SemanticPositions = {};
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
    setPositions((old) => ({ ...old, ...moved }));
  }, []);

  const sessionName = summary.name ?? sessions.find((item) => item.id === selectedSessionId)?.name ?? selectedSessionId;
  const closeInspector = useCallback(() => setInspectorOpen(false), []);
  const changeView = (view: 'execution' | 'causal') => {
    setGraphView(view);
    setSelectedNodeId(null);
    setInspectorOpen(false);
    setIsPlaying(false);
    setFollow(true);
  };
  const playbackLabel = graphView === 'causal' ? '因果快照' : step === null ? '执行结构' : isPlaying ? '回放中' : currentStep === actions.length - 1 ? '回放结束' : '回放已暂停';

  return (
    <div className="drx-replay-app">
      <TopReplayBanner sessionName={listStatus === 'loading' ? '正在读取' : listError ? '列表未加载' : sessionName} sessionCount={listStatus === 'ready' ? sessions.length : null}
        sessionsVisible={sessionsVisible} activityVisible={sidebarVisible} view={graphView} loading={loading}
        isFullscreen={isFullscreen} onToggleSessions={() => setSessionsVisible((value) => !value)}
        onToggleActivity={() => setSidebarVisible((value) => !value)} onViewChange={changeView}
        onRefresh={onRetryList} onToggleFullscreen={toggleFullscreen} />
      {fullscreenError && <div className="app-notice" role="alert">{fullscreenError}</div>}
      <div className={`workspace-body ${sessionsVisible ? '' : 'sessions-collapsed'} ${sidebarVisible ? '' : 'activity-collapsed'}`}>
        {sessionsVisible && <SessionSidebar sessions={sessions} selectedSessionId={selectedSessionId} onSelectSession={onSelectSession}
          loading={listStatus === 'loading'} error={listError} onRefresh={onRetryList} onClose={() => setSessionsVisible(false)} />}
        <main className={`canvas-stage ${graphView === 'causal' ? 'causal-view' : ''}`} aria-label={graphView === 'execution' ? '执行图' : '因果图'}>
          <div className="graph-surface" ref={canvasRef}>
            {loading || error || !selectedSessionId || graphMissing || !hasGraph ? (
              <div className="workspace-empty" role={error ? 'alert' : 'status'}>
                {error ? <WarningCircle size={34} /> : loading ? <ArrowClockwise size={30} className="is-spinning" /> : <Info size={34} />}
                <h2>{loading ? '正在读取会话' : error || (!selectedSessionId ? '无会话' : graphMissing ? '图数据未提供' : graphView === 'causal' ? '因果记录：无' : '执行结构：无')}</h2>
                <p>{loading ? '正在加载已保存的数据…' : error ? '原始记录保持不变，请重试或切换会话。' : !selectedSessionId ? '已保存的会话会显示在左侧列表。' : graphMissing ? '当前服务未提供结构化图数据，请重新启动服务后加载。' : graphView === 'causal' ? '此会话未保存事实、假设、发现或因果关联。' : '此会话未保存任务或执行记录。'}</p>
                {!loading && <button className="header-button" onClick={listError || !selectedSessionId ? onRetryList : () => setRetry((value) => value + 1)}><ArrowClockwise size={15} />重新加载</button>}
              </div>
            ) : (
              <ReactFlow<SemanticFlowNode, SemanticFlowEdge> nodes={projected.nodes} edges={projected.edges} nodeTypes={semanticNodeTypes} edgeTypes={semanticEdgeTypes}
                onInit={setFlow} onNodesChange={onNodesChange} onMoveStart={(event) => { if (event) setFollow(false); }}
                onNodeClick={(_event, node) => openEntity(node.data)}
                nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
                onlyRenderVisibleElements minZoom={0.001} maxZoom={2} proOptions={{ hideAttribution: true }}>
                <Background color="#29384e" gap={24} size={1} />
                {mapVisible && <MiniMap nodeColor={(node) => typeof node.data.color === 'string' ? node.data.color : '#64748b'} maskColor="rgba(7, 13, 24, 0.8)" pannable zoomable />}
              </ReactFlow>
            )}
          </div>
          <div className="canvas-context"><span>{graphView === 'execution' ? '执行图' : '因果图'}</span><span>{graphView === 'execution' ? '任务与记录归属' : '已保存的证据关系'}</span><span>{loading || error || graphMissing ? '未读取' : `${projected.nodes.length} / ${semanticGraph.nodes.length} 节点 · ${projected.edges.length} 条关系`}</span></div>
          {!loading && !error && <div className={`canvas-playback-state ${isPlaying ? 'is-playing' : ''}`} aria-live="polite">{isPlaying ? <Play size={13} weight="fill" /> : <Pause size={13} />}<span>{playbackLabel}</span></div>}
          <div className="graph-tools" role="group" aria-label="图谱操作">
            <button className="icon-button" disabled={graphDisabled} aria-label="放大图谱" title="放大" onClick={() => { setFollow(false); void flow?.zoomIn({ duration: 160 }); }}><Plus size={17} /></button>
            <button className="icon-button" disabled={graphDisabled} aria-label="缩小图谱" title="缩小" onClick={() => { setFollow(false); void flow?.zoomOut({ duration: 160 }); }}><Minus size={17} /></button>
            <button className="icon-button" disabled={graphDisabled} aria-label="查看全图" title="查看全图" onClick={() => { setFollow(false); const viewport = overviewViewport(projected.nodes, canvasWidth, canvasHeight, inspectorOpen); if (viewport) void flow?.setViewport(viewport, { duration: 220 }); }}><CornersOut size={18} /></button>
            <button className="icon-button" disabled={graphDisabled || !semanticGraph.nodes.some((node) => collapsed.has(node.id))} aria-label="展开全部分支" title="展开全部分支" onClick={() => { initialView.current = ''; setFollow(false); setCollapsed((old) => new Set([...old].filter((id) => !semanticGraph.nodes.some((node) => node.id === id)))); }}><TreeStructure size={18} /></button>
            <div className="tool-separator" />
            <button className={`icon-button ${follow ? 'is-active' : ''}`} disabled={graphDisabled} aria-label={follow ? '关闭跟随当前记录' : '跟随当前记录'} aria-pressed={follow} title="跟随当前记录" onClick={() => { setFollow((value) => !value); if (!follow && currentAction) { setStep(currentStep); setCollapsed((old) => expandAncestorsForAction(semanticGraph, currentAction.id, old)); locateCurrent(); } }}><Crosshair size={18} /></button>
            <button className={`icon-button ${mapVisible ? 'is-active' : ''}`} disabled={graphDisabled} aria-label="显示缩略图" aria-pressed={mapVisible} title="缩略图" onClick={() => setMapVisible((value) => !value)}><MapTrifold size={18} /></button>
          </div>
          <button className="panel-handle panel-handle-left" aria-label={sessionsVisible ? '收起会话列表' : '展开会话列表'} aria-expanded={sessionsVisible} onClick={() => setSessionsVisible((value) => !value)}>{sessionsVisible ? <CaretLeft size={14} /> : <CaretRight size={14} />}</button>
          <button className="panel-handle panel-handle-right" aria-label={sidebarVisible ? '收起事件日志' : '展开事件日志'} aria-expanded={sidebarVisible} onClick={() => setSidebarVisible((value) => !value)}>{sidebarVisible ? <CaretRight size={14} /> : <CaretLeft size={14} />}</button>
          {!mapVisible && <div className="graph-legend" aria-label={graphView === 'causal' ? '节点类型图例' : '记录状态图例'}>
            {graphView === 'causal' ? <><span>节点类型</span>{['fact', 'evidence', 'hypothesis', 'finding', 'reference'].map((kind) => <span key={kind}><SemanticKindChip kind={kind} /></span>)}</>
              : <><span>状态图例</span><span><Circle size={8} weight="fill" className="legend-complete" />已完成</span><span><Circle size={8} weight="fill" className="legend-running" />进行中</span><span><Circle size={8} weight="fill" className="legend-error" />失败 / 拒绝</span><span><Circle size={8} weight="fill" className="legend-unknown" />无状态记录</span></>}
          </div>}
          {inspectorOpen && (inspectorNode || inspectorAction) && !loading && !error && <NodeInspector node={inspectorNode} relations={semanticGraph.edges.filter((edge) => edge.source === inspectorNode?.id || edge.target === inspectorNode?.id)} action={inspectorAction} sessionId={summary.sessionId} onClose={closeInspector} />}
          {graphView === 'execution' && <ReplayDock currentAction={currentAction} currentStep={currentStep} totalSteps={actions.length} stages={stages}
            pendingLabel={loading ? '正在读取记录…' : error ? '记录未加载' : null}
            isPlaying={isPlaying && !disabled} speed={speed} isLoop={isLoop} disabled={disabled}
            onTogglePlay={togglePlay} onSetSpeed={setSpeed} onToggleLoop={() => setIsLoop((value) => !value)}
            onReset={() => { selectStep(0); setFollow(true); }} onStepChange={selectStep} />}
        </main>
        {sidebarVisible && <div className="workspace-activity" id="session-sidebar">
          {loading || error ? <div className="workspace-empty"><Info size={24} /><p>{loading ? '正在加载记录…' : '会话记录未加载'}</p></div> :
            <ActionStream actions={actions} currentStep={currentStep} onSelectStep={selectStep} onOpenAction={openAction}
              summary={summary} view={panelView} onViewChange={setPanelView} />}
        </div>}
      </div>
    </div>
  );
}
