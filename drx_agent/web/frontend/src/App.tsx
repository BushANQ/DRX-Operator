import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { ReactFlow, Background, Controls, MiniMap } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { nodeTypes } from './nodes';
import TopReplayBanner from './TopReplayBanner';
import ActionStream from './ActionStream';
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
  const [detailRequestId, setDetailRequestId] = useState(0);
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
    setSidebarVisible(true);
    setDetailRequestId((old) => old + 1);
  }, []);
  const projected = useMemo(() => {
    const projection = projectReplay(graph, currentStep, canvasWidth, positions, measurements);
    return { ...projection, nodes: projection.nodes.map((node) => ({
      ...node, focusable: false,
      data: { ...node.data, onOpen: () => openNode(node.data.step) },
    })) };
  }, [graph, currentStep, canvasWidth, positions, measurements, openNode]);
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
    flow.setCenter(focusNode.position.x + NODE_WIDTH / 2, focusNode.position.y + NODE_HEIGHT / 2, { zoom: 1, duration });
  }, [flow, focusNode]);

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

  return (
    <div className="drx-replay-app">
      <TopReplayBanner
        sessionName={summary.name ?? sessions.find((item) => item.id === selectedSessionId)?.name ?? selectedSessionId}
        sessions={sessions} selectedSessionId={selectedSessionId} onSelectSession={onSelectSession}
        currentAction={currentAction} currentStep={currentStep} totalSteps={actions.length}
        stages={stages} activeStageKey={currentAction?.stageKey} disabled={disabled} loading={loading}
        isPlaying={isPlaying && !disabled} onTogglePlay={togglePlay} speed={speed} onSetSpeed={setSpeed}
        isLoop={isLoop} onToggleLoop={() => setIsLoop((value) => !value)}
        onReset={() => { selectStep(0); setFollow(true); }} onStepChange={selectStep}
        isFullscreen={isFullscreen} onToggleFullscreen={toggleFullscreen}
      />
      {fullscreenError && <div className="app-notice" role="alert">{fullscreenError}</div>}
      {loading || error || !selectedSessionId ? (
        <div className="workspace-empty" role={error ? 'alert' : 'status'}>
          <p>{loading ? '正在加载会话…' : error || '无会话'}</p>
          {!loading && <button className="ctrl-btn secondary" onClick={listError || !selectedSessionId ? onRetryList : () => setRetry((value) => value + 1)}>重新加载</button>}
        </div>
      ) : (
        <div className="replay-workspace">
          <section className="flow-canvas-container" aria-label="会话记录图">
            <div className="canvas-toolbar">
              <div><strong>会话记录图</strong><span className="canvas-explanation">连线表示记录顺序 · 已展示 {projected.nodes.length}/{actions.length}</span></div>
              <div className="canvas-actions">
                <button disabled={disabled} aria-pressed={follow} onClick={() => setFollow((value) => !value)}>跟随当前{follow ? '：开' : '：关'}</button>
                <button disabled={disabled} onClick={locateCurrent}>定位当前</button>
                <button disabled={disabled} onClick={() => { setFollow(false); flow?.fitView({ includeHiddenNodes: true, minZoom: 0.001, padding: 0.15, duration: 250 }); }}>查看全图</button>
                <button aria-expanded={sidebarVisible} aria-controls="session-sidebar" onClick={() => setSidebarVisible((value) => !value)}>{sidebarVisible ? '收起详情' : '显示详情'}</button>
              </div>
            </div>
            <div className="graph-surface" ref={canvasRef}>
              {!actions.length ? <div className="workspace-empty">无动作记录</div> : (
                <ReactFlow<ReplayNode, ReplayEdge>
                  nodes={projected.nodes} edges={projected.edges} nodeTypes={nodeTypes}
                  onInit={setFlow} onNodesChange={onNodesChange}
                  onMoveStart={(event) => { if (event) setFollow(false); }}
                  onNodeClick={(_event, node) => openNode(node.data.step)}
                  nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
                  onlyRenderVisibleElements minZoom={0.001} maxZoom={2}
                  proOptions={{ hideAttribution: true }}
                >
                  <Background color="#203041" gap={24} size={1} />
                  <Controls showFitView={false} showInteractive={false} />
                  <MiniMap nodeColor={(node) => typeof node.data.color === 'string' ? node.data.color : '#4897ad'} maskColor="rgba(4, 9, 17, 0.72)" pannable zoomable />
                </ReactFlow>
              )}
            </div>
          </section>
          {sidebarVisible && (
            <div className="workspace-sidebar" id="session-sidebar">
              <ActionStream actions={actions} currentStep={currentStep} onSelectStep={selectStep}
                selectedAction={currentAction} detailRequestId={detailRequestId} summary={summary} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
