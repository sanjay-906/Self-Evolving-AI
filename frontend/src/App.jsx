import React, { useState, useRef, useEffect, useCallback } from 'react';
import './App.css';

const API_BASE = 'http://localhost:8000';
const MAX_LOGS = 400;

// ─── SVG Logo ────────────────────────────────────────────────────────────────
const AgentLogo = () => (
  <svg viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">
    <polygon points="18,2 34,10 34,26 18,34 2,26 2,10" stroke="#00d2ff" strokeWidth="1.5" fill="rgba(0,210,255,0.06)" />
    <circle cx="18" cy="18" r="5" stroke="#00d2ff" strokeWidth="1.2" fill="rgba(0,210,255,0.15)" />
    <line x1="18" y1="2" x2="18" y2="13" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
    <line x1="18" y1="23" x2="18" y2="34" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
    <line x1="2" y1="10" x2="13.3" y2="14.5" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
    <line x1="22.7" y1="21.5" x2="34" y2="26" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
    <line x1="34" y1="10" x2="22.7" y2="14.5" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
    <line x1="13.3" y1="21.5" x2="2" y2="26" stroke="#00d2ff" strokeWidth="1" opacity="0.5" />
  </svg>
);

// ─── Streaming text hook ─────────────────────────────────────────────────────
function useStreamText(target, isStreaming) {
  const [displayed, setDisplayed] = useState('');
  const posRef = useRef(0);
  const timerRef = useRef(null);

  useEffect(() => {
    // When target grows, stream new chars
    if (target.length > posRef.current) {
      const stream = () => {
        setDisplayed(prev => {
          const next = target.slice(0, posRef.current + 1);
          posRef.current += 1;
          return next;
        });
        if (posRef.current < target.length) {
          timerRef.current = setTimeout(stream, 8);
        }
      };
      if (!timerRef.current) stream();
    }
    return () => {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    };
  }, [target]);

  // Fast-forward when streaming stops
  useEffect(() => {
    if (!isStreaming && target) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
      posRef.current = target.length;
      setDisplayed(target);
    }
  }, [isStreaming, target]);

  return displayed;
}

// ─── Message component ────────────────────────────────────────────────────────
function Message({ msg, isLast, isLoading, artifactCache }) {
  const isStreaming = isLast && isLoading && msg.role === 'assistant';
  const displayed = useStreamText(msg.content || '', isStreaming);
  const showCursor = isStreaming && displayed.length < (msg.content || '').length;

  return (
    <div className={`message ${msg.role}`}>
      <div className="message-meta">
        <div className="meta-avatar">
          {msg.role === 'user' ? '▲' : '◈'}
        </div>
        <span>{msg.role === 'user' ? 'YOU' : msg.role === 'assistant' ? 'AGENT' : 'SYS'}</span>
        {msg.timestamp && (
          <span style={{ opacity: 0.5 }}>
            {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
          </span>
        )}
      </div>

      <div className="bubble">
        {displayed}
        {showCursor && <span className="streaming-cursor" />}
      </div>

      {/* Artifacts */}
      {msg.artifacts && msg.artifacts.length > 0 && (
        <div className="artifacts-row">
          {msg.artifacts.map((art, i) => (
            <ArtifactCard key={i} artifact={art} cache={artifactCache} />
          ))}
        </div>
      )}

      {/* Steps */}
      {msg.steps && msg.steps.length > 0 && (
        <div className="steps-accordion">
          <details>
            <summary>⚙ Tool Execution Trace ({msg.steps.length} events)</summary>
            <div style={{ marginTop: 6 }}>
              {msg.steps.map((step, i) => (
                <div key={i} className="step-item">
                  <span className={`step-badge ${step.type === 'tool_call' ? 'call' : 'result'}`}>
                    {step.type === 'tool_call' ? 'CALL' : 'RESULT'}
                  </span>
                  <code style={{ color: 'var(--cyan)' }}>{step.tool}</code>
                  {step.type === 'tool_call' && step.args && (
                    <pre>{JSON.stringify(step.args, null, 2)}</pre>
                  )}
                  {step.type === 'tool_result' && step.preview && (
                    <pre style={{ opacity: 0.6 }}>{step.preview}</pre>
                  )}
                </div>
              ))}
            </div>
          </details>
        </div>
      )}
    </div>
  );
}

// ─── Artifact card ────────────────────────────────────────────────────────────
function ArtifactCard({ artifact, cache }) {
  const { filename, extension, size_bytes } = artifact;
  const b64 = cache[filename];
  const kb = (size_bytes / 1024).toFixed(1);

  if (['png', 'jpg', 'jpeg', 'gif'].includes(extension)) {
    return (
      <div className="artifact-card">
        {b64
          ? <img src={`data:image/${extension};base64,${b64}`} alt={filename} />
          : <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: '0.75rem' }}>
              📷 loading {filename}…
            </div>
        }
        <div className="artifact-footer">
          <span>{filename}</span>
          <span>{kb} KB</span>
        </div>
      </div>
    );
  }

  if (extension === 'pdf') {
    return (
      <div className="artifact-card" style={{ minWidth: 260 }}>
        <div style={{ padding: '20px', textAlign: 'center', fontSize: '2rem' }}>📄</div>
        <div className="artifact-footer">
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.72rem', color: 'var(--text-muted)' }}>
            {filename} · {kb} KB
          </span>
          <a
            href={`${API_BASE}/artifacts/${filename}`}
            target="_blank"
            rel="noopener noreferrer"
            className="artifact-download"
          >↓ Download</a>
        </div>
        {b64 && (
          <iframe
            src={`data:application/pdf;base64,${b64}`}
            width="100%"
            height="340"
            style={{ border: 'none', display: 'block' }}
            title={filename}
          />
        )}
      </div>
    );
  }

  return (
    <div className="artifact-card">
      <div className="artifact-footer">
        <span>📎 {filename}</span>
        <a href={`${API_BASE}/artifacts/${filename}`} target="_blank" rel="noopener noreferrer" className="artifact-download">
          ↓ {kb} KB
        </a>
      </div>
    </div>
  );
}

// ─── Log entry ────────────────────────────────────────────────────────────────
function LogEntry({ entry }) {
  const ts = new Date(entry.ts * 1000).toISOString().slice(11, 23);
  return (
    <div className={`log-entry level-${entry.level}`}>
      <span className="log-ts">{ts}</span>
      <span className={`log-level ${entry.level}`}>{entry.level.slice(0, 4)}</span>
      <span className="log-msg">{entry.message}</span>
    </div>
  );
}

// ─── Main App ─────────────────────────────────────────────────────────────────
export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [connected, setConnected] = useState(false);
  const [toolHistory, setToolHistory] = useState([]);
  const [logs, setLogs] = useState([]);
  const [artifactCache, setArtifactCache] = useState({});

  const messagesEndRef = useRef(null);
  const logEndRef = useRef(null);
  const wsRef = useRef(null);
  const pendingAsstRef = useRef(null); // tracks the in-progress assistant message

  // Auto-scroll messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Auto-scroll logs
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'instant' });
  }, [logs]);

  // Fetch base64 for artifacts separately (keeps it out of LLM context)
  const fetchArtifact = useCallback(async (filename) => {
    if (artifactCache[filename]) return;
    try {
      const res = await fetch(`${API_BASE}/artifacts/base64/${filename}`);
      if (!res.ok) return;
      const data = await res.json();
      setArtifactCache(prev => ({ ...prev, [filename]: data.base64 }));
    } catch { /* ignore */ }
  }, [artifactCache]);

  // WebSocket
  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws/chat');
    ws.onopen = () => setConnected(true);

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);

      if (payload.type === 'log') {
        setLogs(prev => {
          const next = [...prev, payload.data];
          return next.length > MAX_LOGS ? next.slice(next.length - MAX_LOGS) : next;
        });
        return;
      }

      if (payload.type === 'status') {
        setIsLoading(true);
        // Add a placeholder assistant message that we'll stream into
        const placeholder = {
          role: 'assistant',
          content: '',
          artifacts: [],
          steps: [],
          timestamp: new Date().toISOString(),
          _id: Date.now(),
        };
        pendingAsstRef.current = placeholder._id;
        setMessages(prev => [...prev, placeholder]);
        return;
      }

      if (payload.type === 'artifacts') {
        payload.data.forEach(art => fetchArtifact(art.filename));
        // Update the last assistant message's artifacts
        setMessages(prev => prev.map((m, i) =>
          i === prev.length - 1 && m.role === 'assistant'
            ? { ...m, artifacts: payload.data }
            : m
        ));
        return;
      }

      if (payload.type === 'complete') {
        const result = payload.data;
        result.artifacts?.forEach(art => fetchArtifact(art.filename));

        // Replace the placeholder with the full message
        setMessages(prev => prev.map((m, i) =>
          i === prev.length - 1 && m.role === 'assistant'
            ? {
                ...m,
                content: result.response,
                artifacts: result.artifacts || [],
                steps: result.steps || [],
              }
            : m
        ));
        setToolHistory(prev => {
          const fresh = (result.tool_history || []).filter(t => !prev.includes(t));
          return [...prev, ...fresh];
        });
        setIsLoading(false);
        pendingAsstRef.current = null;
        return;
      }

      if (payload.type === 'error') {
        setIsLoading(false);
        pendingAsstRef.current = null;
        setMessages(prev => [...prev, {
          role: 'system',
          content: `ERROR: ${payload.data.error}`,
          timestamp: new Date().toISOString(),
        }]);
      }
    };

    ws.onclose = () => setConnected(false);
    ws.onerror = () => setConnected(false);
    wsRef.current = ws;
    return () => ws.close();
  }, [fetchArtifact]);

  const sendMessage = () => {
    const trimmed = input.trim();
    if (!trimmed || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || isLoading) return;

    const userMsg = { role: 'user', content: trimmed, timestamp: new Date().toISOString() };
    setMessages(prev => [...prev, userMsg]);
    setInput('');

    const history = messages.map(m => ({ role: m.role, content: m.content }));
    wsRef.current.send(JSON.stringify({ message: trimmed, history }));
  };

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  };

  const examples = [
    'Create a PDF report about the solar system with facts about each planet',
    'Draw a line chart of monthly sales: Jan=100, Feb=150, Mar=200, Apr=180, May=220',
    'Write a Fibonacci calculator tool and compute fib(30)',
    'Fetch and summarize the latest Python version changelog',
  ];

  return (
    <div className="app">
      {/* ── Header ── */}
      <header className="header">
        <div className="header-brand">
          <div className="header-logo"><AgentLogo /></div>
          <div>
            <div className="header-title">Self-Evolving AI</div>
            <div className="header-sub">Evolve during action</div>
          </div>
        </div>
        <div className="header-right">
          <div className={`status-pill ${connected ? 'online' : 'offline'}`}>
            <span className="status-dot" />
            {connected ? 'ONLINE' : 'OFFLINE'}
          </div>
        </div>
      </header>

      {/* ── Tool badges ── */}
      {toolHistory.length > 0 && (
        <div className="tool-bar">
          <span className="tool-bar-label">EVOLVED TOOLS</span>
          {toolHistory.map((t, i) => (
            <span key={i} className="tool-badge">{t}</span>
          ))}
        </div>
      )}

      {/* ── Two-column area ── */}
      <div className="content-area">
        {/* Messages */}
        <div className="messages-pane">
          {messages.length === 0 && (
            <div className="welcome">
              <div className="welcome-glyph">◈</div>
              <h2>Zero Tools. Full Potential.</h2>
              <p>I start bare and evolve by writing my own tools at runtime. Every task shapes my capabilities.</p>
              <div className="example-prompts">
                {examples.map((ex, i) => (
                  <button key={i} className="example-btn" onClick={() => setInput(ex)}>
                    {ex}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, idx) => (
            <Message
              key={msg._id || idx}
              msg={msg}
              isLast={idx === messages.length - 1}
              isLoading={isLoading}
              artifactCache={artifactCache}
            />
          ))}

          {isLoading && messages[messages.length - 1]?.role !== 'assistant' && (
            <div className="msg-loading">
              <div className="pulse-ring" />
              <span>PROCESSING…</span>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Live log pane */}
        <div className="log-pane">
          <div className="log-header">
            <div className="log-title">
              {isLoading && <span className="log-title-dot" />}
              RUNTIME LOGS
            </div>
            <button className="log-clear-btn" onClick={() => setLogs([])}>CLEAR</button>
          </div>

          <div className="log-scroll">
            {logs.length === 0 && !isLoading && (
              <div className="log-empty">// awaiting agent activity</div>
            )}
            {isLoading && logs.length === 0 && (
              <div className="log-thinking">
                <div className="log-thinking-dots">
                  <span /><span /><span />
                </div>
                <span>waiting for logs…</span>
              </div>
            )}
            {logs.map((entry, i) => (
              <LogEntry key={i} entry={entry} />
            ))}
            <div ref={logEndRef} />
          </div>
        </div>
      </div>

      {/* ── Input ── */}
      <div className="input-bar">
        <div className="input-wrap">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
            placeholder="Instruct the agent… (Shift+Enter for newline)"
            rows={2}
            disabled={isLoading}
          />
        </div>
        <button className="send-btn" onClick={sendMessage} disabled={isLoading || !input.trim()}>
          {isLoading ? (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <circle cx="9" cy="9" r="7" stroke="white" strokeWidth="2" strokeDasharray="22" strokeLinecap="round">
                <animateTransform attributeName="transform" type="rotate" from="0 9 9" to="360 9 9" dur="0.8s" repeatCount="indefinite" />
              </circle>
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
              <path d="M2 9h14M10 3l6 6-6 6" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </button>
      </div>
    </div>
  );
}