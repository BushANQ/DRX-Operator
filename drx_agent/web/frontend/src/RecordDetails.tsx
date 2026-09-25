import { useEffect, useRef, useState } from 'react';
import { Check, Copy, TerminalWindow } from '@phosphor-icons/react';
import CodeBlock from './CodeBlock.tsx';
import { eventPresentation } from './presentation.ts';
import type { ReplayAction } from './types.ts';

interface RecordDetailsProps {
  action: ReplayAction | null;
  sessionId: string;
}

function hasValue(value: unknown): boolean {
  return value != null && value !== '';
}

function display(value: unknown): string {
  if (value == null || value === '') return '无';
  if (typeof value === 'object') return Object.keys(value).length ? JSON.stringify(value, null, 2) : '无';
  return String(value);
}

export function RawRecord({ value, label }: { value: unknown; label: string }) {
  return (
    <details className="record-source-details">
      <summary>完整{label}记录</summary>
      <pre className="detail-code-pre"><code>{display(value)}</code></pre>
    </details>
  );
}

export default function RecordDetails({ action, sessionId }: RecordDetailsProps) {
  const [copyFeedback, setCopyFeedback] = useState<{
    actionId: string;
    sessionId: string;
    label: string;
    success: boolean;
    message: string;
  } | null>(null);
  const detailBodyRef = useRef<HTMLDivElement>(null);
  const feedback = copyFeedback?.actionId === action?.id && copyFeedback?.sessionId === sessionId
    ? copyFeedback
    : null;

  useEffect(() => {
    if (detailBodyRef.current) detailBodyRef.current.scrollTop = 0;
  }, [action?.id, sessionId]);

  async function handleCopy(label: string, value: unknown): Promise<void> {
    if (!action) return;
    const source = { actionId: action.id, sessionId, label };
    try {
      await navigator.clipboard.writeText(display(value));
      setCopyFeedback({ ...source, success: true, message: `${label}已复制` });
    } catch {
      setCopyFeedback({ ...source, success: false, message: '复制失败，请选择文本手动复制' });
    }
  }

  function dataBlock(label: string, value: unknown, output = false) {
    const copied = feedback?.label === label && feedback.success;
    return (
      <section className="detail-block">
        <div className="detail-block-header">
          <h3 className="detail-block-title">{label}</h3>
          {hasValue(value) && (
            <button type="button" className="detail-copy-btn" onClick={() => { void handleCopy(label, value); }} aria-label={`复制${label}`}>
              {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
              <span>{copied ? '已复制' : '复制'}</span>
            </button>
          )}
        </div>
        <CodeBlock value={display(value)} output={output} />
      </section>
    );
  }

  return (
    <div className="detail-panel-body" ref={detailBodyRef}>
      {!action ? <p className="empty-state">详细信息：无</p> : (
        <>
          <div className="detail-meta-card">
            <div className="detail-meta-top">
              <span className="detail-category-pill" style={{ color: eventPresentation(action).color }}>{display(action.category || action.kind)}</span>
              <span className="detail-time-stamp">时间：{display(action.timeOffset)}</span>
            </div>
            <h2 className="detail-title-large">{display(action.title)}</h2>
            <dl className="detail-metadata-list">
              <div><dt>执行者</dt><dd>{display(action.actor)}</dd></div>
              <div><dt>消息角色</dt><dd>{display(action.role)}</dd></div>
              <div><dt>工具</dt><dd className="detail-tool-name"><TerminalWindow size={13} aria-hidden="true" />{display(action.tool)}</dd></div>
              <div><dt>状态</dt><dd>{display(action.status)}</dd></div>
              <div><dt>阶段</dt><dd>{display(action.stageTitle || action.stageKey)}</dd></div>
              <div><dt>记录 ID</dt><dd>{display(action.sourceId ?? action.id)}</dd></div>
            </dl>
          </div>
          {hasValue(action.text) && dataBlock('消息内容', action.text)}
          {hasValue(action.thought) && action.thought !== action.text && dataBlock('记录中的推理内容', action.thought)}
          {dataBlock('输入参数', hasValue(action.fullInput) ? action.fullInput : action.input)}
          {dataBlock('输出结果', hasValue(action.fullOutput) ? action.fullOutput : action.output, true)}
          <RawRecord key={`${sessionId}:${action.id}`} value={action.data} label="事件" />
          <p className="copy-feedback" role="status">{feedback?.message ?? ''}</p>
        </>
      )}
    </div>
  );
}
