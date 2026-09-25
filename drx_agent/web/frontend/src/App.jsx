import { useState, useCallback, useEffect, useRef } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { nodeTypes } from './nodes';
import TopReplayBanner from './TopReplayBanner';
import ActionStream from './ActionStream';

const API_BASE = ''; // Proxied in dev, same origin in prod

export default function App() {
  // Sessions
  const [sessions, setSessions] = useState([]);
  const [selectedSessionId, setSelectedSessionId] = useState(null);

  // Graph Data
  const [graphData, setGraphData] = useState(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // Replay State
  const [currentStep, setCurrentStep] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [isLoop, setIsLoop] = useState(false);
  const [selectedNode, setSelectedNode] = useState(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [fullscreenError, setFullscreenError] = useState('');

  const timerRef = useRef(null);

  // ---------------- 1. Fetch sessions ----------------
  useEffect(() => {
    fetch(`${API_BASE}/api/sessions`)
      .then((r) => r.json())
      .then((data) => {
        setSessions(data || []);
        if (data && data.length > 0) {
          // Default to the richest session or first
          const target = data.find((s) => s.id === '74b7e646-5aa') || data[0];
          setSelectedSessionId(target.id);
        }
      })
      .catch((err) => console.error('Failed to fetch sessions:', err));
  }, []);

  // ---------------- 2. Fetch session graph ----------------
  useEffect(() => {
    if (!selectedSessionId) return;
    setGraphLoading(true);
    setSelectedNode(null);
    setCurrentStep(0);
    setIsPlaying(false);

    fetch(`${API_BASE}/api/sessions/${selectedSessionId}`)
      .then((r) => r.json())
      .then((data) => {
        setGraphData(data);
        setNodes(data.nodes || []);
        setEdges(data.edges || []);
        setGraphLoading(false);
      })
      .catch((err) => {
        console.error('Failed to fetch session graph:', err);
        setGraphLoading(false);
      });
  }, [selectedSessionId, setNodes, setEdges]);

  const actions = graphData?.actions || [];
  const stages = graphData?.stages || [];
  const summary = graphData?.summary || {};
  const totalSteps = actions.length || 1;
  const currentAction = actions[currentStep] || {};

  // ---------------- 3. Replay: Update node highlight / lit states ----------------
  useEffect(() => {
    if (!graphData || !graphData.nodes) return;

    const curAct = actions[currentStep];
    const currentCardId = curAct?.cardId || 'session-root';
    const currentStageIdx = curAct?.stageIndex || 1;

    // Card order across stages:
    // 1: session-root
    // 2-4: card-page, card-surface
    // 5-6: card-hypo, card-fp
    // 7-8: card-vuln, card-priv
    // 9: card-evidence, card-report
    const cardLitOrder = [
      'session-root',
      'card-page',
      'card-surface',
      'card-hypo',
      'card-fp',
      'card-vuln',
      'card-priv',
      'card-evidence',
      'card-report',
    ];

    const currentCardIndex = cardLitOrder.indexOf(currentCardId);
    const litCardIds = new Set(
      cardLitOrder.slice(0, Math.max(1, currentCardIndex + 1))
    );

    // Update nodes: lit, current, pending
    setNodes((nds) =>
      nds.map((node) => {
        if (node.type === 'phaseGroupNode') {
          return node;
        }

        const isCurrent = node.id === currentCardId;
        const isLit = litCardIds.has(node.id);
        const isPending = !isLit;

        return {
          ...node,
          data: {
            ...node.data,
            _current: isCurrent,
            _lit: isLit,
            _pending: isPending,
          },
        };
      })
    );

    // Update edges: active glow for traversed path
    setEdges(
      (graphData.edges || []).map((edge) => {
        const sourceLit = litCardIds.has(edge.source);
        const targetLit = litCardIds.has(edge.target);
        const isEdgeLit = sourceLit && targetLit;

        return {
          ...edge,
          style: {
            ...edge.style,
            stroke: isEdgeLit ? (edge.style?.stroke || '#06b6d4') : '#1e293b',
            opacity: isEdgeLit ? 1 : 0.25,
          },
        };
      })
    );
  }, [currentStep, graphData, actions, setNodes, setEdges]);

  // ---------------- 4. Replay auto-play loop ----------------
  useEffect(() => {
    if (isPlaying) {
      const delay = Math.max(150, Math.round(1000 / speed));
      timerRef.current = setInterval(() => {
        setCurrentStep((prev) => {
          if (prev >= totalSteps - 1) {
            if (isLoop) {
              return 0;
            } else {
              setIsPlaying(false);
              return prev;
            }
          }
          return prev + 1;
        });
      }, delay);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [isPlaying, speed, totalSteps, isLoop]);

  // ---------------- 5. Keyboard shortcuts ----------------
  useEffect(() => {
    function handleKeyDown(e) {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

      if (e.code === 'Space') {
        e.preventDefault();
        setIsPlaying((p) => !p);
      } else if (e.code === 'ArrowLeft') {
        e.preventDefault();
        setCurrentStep((p) => Math.max(0, p - 1));
      } else if (e.code === 'ArrowRight') {
        e.preventDefault();
        setCurrentStep((p) => Math.min(totalSteps - 1, p + 1));
      }
    }
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [totalSteps]);

  // Fullscreen toggle
  useEffect(() => {
    const updateFullscreen = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', updateFullscreen);
    return () => document.removeEventListener('fullscreenchange', updateFullscreen);
  }, []);

  const handleToggleFullscreen = useCallback(async () => {
    setFullscreenError('');
    try {
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      setFullscreenError('全屏切换失败，请重试。');
    }
  }, []);

  // Handle node click
  const onNodeClick = useCallback((_, node) => {
    setSelectedNode(node);
  }, []);

  return (
    <div className={`drx-replay-app ${isFullscreen ? 'fullscreen' : ''}`}>
      {fullscreenError && <div role="alert">{fullscreenError}</div>}
      {/* ---------------- Top Replay Dashboard Banner ---------------- */}
      <TopReplayBanner
        sessionName={summary.name || selectedSessionId || 'test'}
        sessions={sessions}
        selectedSessionId={selectedSessionId}
        onSelectSession={setSelectedSessionId}
        currentAction={currentAction}
        currentStep={currentStep}
        totalSteps={totalSteps}
        stages={stages}
        actions={actions}
        activeStageKey={currentAction.stageKey || '推理'}
        isPlaying={isPlaying}
        onTogglePlay={setIsPlaying}
        speed={speed}
        onSetSpeed={setSpeed}
        isLoop={isLoop}
        onToggleLoop={() => setIsLoop(!isLoop)}
        onReset={() => {
          setCurrentStep(0);
          setIsPlaying(false);
        }}
        onStepChange={setCurrentStep}
        isFullscreen={isFullscreen}
        onToggleFullscreen={handleToggleFullscreen}
      />

      {/* ---------------- Main Content: Flow Canvas + Action Stream ---------------- */}
      <div className="replay-workspace">
        {/* React Flow Graph Area */}
        <div className="flow-canvas-container">
          {/* Canvas Subtitle & Legend Overlay */}
          <div className="canvas-header-overlay">
            <div className="canvas-title-row">
              <span className="canvas-title">研判成果图谱</span>
              <span className="canvas-subtitle-hint">
                节点随真实进度点亮 · 未执行阶段保持灰态 · 联动 · 深度推理 · 多轮假设推演
              </span>
            </div>

            <div className="canvas-legend-row">
              <span className="legend-item">
                <span className="legend-dot green" />
                <span>分区</span>
              </span>
              <span className="legend-item">
                <span className="legend-dot cyan" />
                <span>已证实</span>
              </span>
              <span className="legend-item">
                <span className="legend-dot purple" />
                <span>过程</span>
              </span>
              <span className="legend-item">
                <span className="legend-dot red" />
                <span>已打通</span>
              </span>
              <span className="legend-item">
                <span className="legend-dot gray" />
                <span>探索中</span>
              </span>
            </div>
          </div>

          {graphLoading ? (
            <div className="loading-container">
              <div className="cyber-spinner" />
              <div className="loading-text">正在加载研判会话图谱...</div>
            </div>
          ) : (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              nodeTypes={nodeTypes}
              onNodeClick={onNodeClick}
              fitView
              fitViewOptions={{ padding: 0.15, minZoom: 0.35, maxZoom: 1.15 }}
              proOptions={{ hideAttribution: true }}
              minZoom={0.2}
              maxZoom={2.5}
            >
              <Background color="#10192d" gap={32} size={1} />
              <Controls
                style={{
                  background: '#0d1527',
                  border: '1px solid rgba(56, 189, 248, 0.25)',
                  borderRadius: 8,
                }}
              />
              <MiniMap
                nodeColor={(n) => n.data?.color || '#38bdf8'}
                maskColor="rgba(8, 12, 20, 0.85)"
                style={{
                  background: '#0a0f1d',
                  border: '1px solid rgba(56, 189, 248, 0.2)',
                  borderRadius: 8,
                }}
              />
            </ReactFlow>
          )}
        </div>

        {/* Action Stream & Detail Panel */}
        <ActionStream
          actions={actions}
          currentStep={currentStep}
          onSelectStep={(step) => {
            setCurrentStep(step);
            const act = actions[step];
            if (act) {
              const matchedNode = nodes.find((n) => n.id === act.cardId);
              if (matchedNode) {
                setSelectedNode(matchedNode);
              }
            }
          }}
          selectedNode={selectedNode}
          summary={summary}
        />
      </div>
    </div>
  );
}
