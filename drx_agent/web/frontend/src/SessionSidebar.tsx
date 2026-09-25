import { useId, useMemo, useRef, useState } from 'react';
import {
  ArrowClockwise,
  CalendarBlank,
  Circle,
  CaretLeft,
  ClockCounterClockwise,
  FolderOpen,
  MagnifyingGlass,
  X,
} from '@phosphor-icons/react';
import type { SessionListItem } from './types.ts';

export interface SessionSidebarProps {
  sessions: SessionListItem[];
  selectedSessionId: string | null;
  onSelectSession: (id: string) => void;
  loading: boolean;
  error?: string;
  onRefresh: () => void;
  onClose: () => void;
}

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
});

function sessionDate(timestamp: number | null): Date | null {
  if (timestamp === null || !Number.isFinite(timestamp)) return null;
  const date = new Date(timestamp * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
}

function shortId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

export default function SessionSidebar({
  sessions,
  selectedSessionId,
  onSelectSession,
  loading,
  error = '',
  onRefresh,
  onClose,
}: SessionSidebarProps) {
  const headingId = useId();
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState('');
  const searchTerm = query.trim().toLocaleLowerCase();
  const filteredSessions = useMemo(() => sessions.filter((session) => (
    !searchTerm
    || session.id.toLocaleLowerCase().includes(searchTerm)
    || session.name?.toLocaleLowerCase().includes(searchTerm)
  )), [sessions, searchTerm]);

  function clearSearch() {
    setQuery('');
    searchRef.current?.focus();
  }

  return (
    <aside className="session-sidebar" aria-labelledby={headingId} aria-busy={loading}>
      <header className="session-sidebar-header">
        <div className="session-sidebar-heading">
          <h2 id={headingId}>会话列表</h2>
          <span className="session-sidebar-count" aria-label={loading ? '正在读取会话数量' : error ? '会话数量不可用' : `共 ${sessions.length} 个会话`}>
            {loading ? '…' : error ? '无' : sessions.length}
          </span>
        </div>
        <div className="session-sidebar-tools">
          <button
            type="button"
            className="session-sidebar-icon-button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="刷新会话列表"
            title="刷新会话列表"
          >
            <ArrowClockwise size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="session-sidebar-icon-button"
            onClick={onClose}
            aria-label="收起会话列表"
            title="收起会话列表"
          >
            <CaretLeft size={16} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="session-sidebar-search">
        <MagnifyingGlass size={16} aria-hidden="true" />
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && query) {
              event.stopPropagation();
              clearSearch();
            }
          }}
          placeholder="搜索名称或 ID"
          aria-label="搜索会话名称或 ID"
          autoComplete="off"
          spellCheck={false}
          disabled={loading}
        />
        {query && (
          <button
            type="button"
            className="session-sidebar-search-clear"
            onClick={clearSearch}
            aria-label="清空搜索"
            title="清空搜索"
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="session-sidebar-body">
        {loading ? (
          <div className="session-sidebar-state" role="status">
            <ClockCounterClockwise size={24} aria-hidden="true" />
            <p>正在读取会话</p>
          </div>
        ) : error ? (
          <div className="session-sidebar-state session-sidebar-state-error" role="alert">
            <p>会话列表读取失败</p>
            <span>{error}</span>
            <button type="button" className="session-sidebar-retry" onClick={onRefresh}>
              <ArrowClockwise size={14} aria-hidden="true" />
              重新读取
            </button>
          </div>
        ) : sessions.length === 0 ? (
          <div className="session-sidebar-state" role="status">
            <FolderOpen size={24} aria-hidden="true" />
            <p>暂无已保存会话</p>
            <span>保存的会话将在这里显示</span>
          </div>
        ) : filteredSessions.length === 0 ? (
          <div className="session-sidebar-state" role="status">
            <MagnifyingGlass size={24} aria-hidden="true" />
            <p>没有匹配的会话</p>
            <span>尝试其他名称或 ID</span>
            <button type="button" className="session-sidebar-retry" onClick={clearSearch}>清空搜索</button>
          </div>
        ) : (
          <ul className="session-sidebar-list" aria-label="已保存的会话">
            {filteredSessions.map((session) => {
              const selected = session.id === selectedSessionId;
              const name = session.name?.trim() || '无';
              const createdAt = sessionDate(session.created_at);
              return (
                <li key={session.id}>
                  <button
                    type="button"
                    className={`session-sidebar-item${selected ? ' is-selected' : ''}`}
                    onClick={() => onSelectSession(session.id)}
                    aria-pressed={selected}
                    aria-label={`选择会话：${name}，ID：${session.id}`}
                  >
                    <span className="session-sidebar-item-topline">
                      <span className="session-sidebar-item-id" title={session.id}>{shortId(session.id)}</span>
                      {selected && <Circle size={6} weight="fill" className="session-sidebar-selection-dot" aria-hidden="true" />}
                    </span>
                    <span className="session-sidebar-item-name" title={name}>{name}</span>
                    <span className="session-sidebar-item-date">
                      <CalendarBlank size={12} aria-hidden="true" />
                      <span className="session-sidebar-sr-only">创建时间：</span>
                      {createdAt ? (
                        <time dateTime={createdAt.toISOString()} title={createdAt.toLocaleString('zh-CN')}>
                          {dateFormatter.format(createdAt)}
                        </time>
                      ) : '无'}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <footer className="session-sidebar-footer">
        <ClockCounterClockwise size={14} aria-hidden="true" />
        <span>
          {loading ? '正在读取本地会话' : error ? '本地会话暂不可用' : `本地回放 · ${sessions.length} 个已保存会话`}
        </span>
      </footer>
    </aside>
  );
}
