import React, { useState, useRef, useEffect } from 'react';
import './App.css';

const API_BASE = 'http://localhost:8000';

function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [connected, setConnected] = useState(false);
  const [toolHistory, setToolHistory] = useState([]);
  // NEW: Store fetched base64 data separately, keyed by filename
  const [artifactCache, setArtifactCache] = useState({});
  const messagesEndRef = useRef(null);
  const wsRef = useRef(null);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // WebSocket connection
  useEffect(() => {
    const ws = new WebSocket(`ws://localhost:8000/ws/chat`);
    
    ws.onopen = () => {
      setConnected(true);
      console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      handleWebSocketMessage(payload);
    };

    ws.onclose = () => {
      setConnected(false);
      console.log('WebSocket disconnected');
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      setConnected(false);
    };

    wsRef.current = ws;

    return () => ws.close();
  }, []);

  // NEW: Fetch base64 content separately via HTTP when artifacts arrive
  // This keeps base64 OUT of the LLM context (saves tokens)
  const fetchArtifactBase64 = async (filename) => {
    // Don't fetch if we already have it
    if (artifactCache[filename]) return;
    
    try {
      const response = await fetch(`${API_BASE}/artifacts/base64/${filename}`);
      if (!response.ok) throw new Error('Failed to fetch artifact');
      const data = await response.json();
      
      setArtifactCache(prev => ({
        ...prev,
        [filename]: data.base64
      }));
    } catch (err) {
      console.error('Error fetching artifact:', err);
    }
  };

  const handleWebSocketMessage = (payload) => {
    if (payload.type === 'status') {
      setIsLoading(true);
    } else if (payload.type === 'complete') {
      setIsLoading(false);
      const result = payload.data;
      
      // NEW: Trigger HTTP fetch for any artifacts (base64 NOT in WebSocket payload)
      if (result.artifacts && result.artifacts.length > 0) {
        result.artifacts.forEach(art => {
          fetchArtifactBase64(art.filename);
        });
      }
      
      // Add assistant message
      const assistantMsg = {
        role: 'assistant',
        content: result.response,
        artifacts: result.artifacts || [],
        steps: result.steps || [],
        timestamp: new Date().toISOString(),
      };
      
      setMessages(prev => [...prev, assistantMsg]);
      setToolHistory(prev => [...prev, ...result.tool_history]);
    } else if (payload.type === 'error') {
      setIsLoading(false);
      setMessages(prev => [...prev, {
        role: 'system',
        content: `Error: ${payload.data.error}`,
        timestamp: new Date().toISOString(),
      }]);
    }
  };

  const sendMessage = () => {
    if (!input.trim() || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    const userMsg = {
      role: 'user',
      content: input,
      timestamp: new Date().toISOString(),
    };

    setMessages(prev => [...prev, userMsg]);
    setIsLoading(true);

    // Build history for the agent
    const history = messages.map(m => ({
      role: m.role,
      content: m.content,
    }));

    wsRef.current.send(JSON.stringify({
      message: input,
      history: history,
    }));

    setInput('');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };


  const renderArtifact = (artifact) => {
    const { filename, extension, size_bytes } = artifact;
    const b64 = artifactCache[filename];
    
    if (['png', 'jpg', 'jpeg', 'gif'].includes(extension)) {
      return (
        <div className="artifact image-artifact">
          {b64 ? (
            <img 
              src={`data:image/${extension};base64,${b64}`} 
              alt={filename}
              style={{ maxWidth: '100%', borderRadius: '8px' }}
            />
          ) : (
            <div className="artifact-loading">
              📷 {filename} ({(size_bytes / 1024).toFixed(1)} KB)
              <br/><small>Click to load image...</small>
            </div>
          )}
        </div>
      );
    }
    
    if (extension === 'pdf') {
      return (
        <div className="artifact pdf-artifact">
          <div className="pdf-preview">📄 PDF Document</div>
          <a 
            href={`${API_BASE}/artifacts/${filename}`}
            target="_blank"
            rel="noopener noreferrer"
            className="download-link"
          >
            ⬇️ Download {filename} ({(size_bytes / 1024).toFixed(1)} KB)
          </a>
          {b64 && (
            <iframe
              src={`data:application/pdf;base64,${b64}`}
              width="100%"
              height="400px"
              style={{ border: '1px solid #ddd', borderRadius: '8px', marginTop: '8px' }}
              title={filename}
            />
          )}
        </div>
      );
    }

    return (
      <div className="artifact">
        📎 {filename} ({(size_bytes / 1024).toFixed(1)} KB)
      </div>
    );
  };

  // Render tool execution steps
  const renderSteps = (steps) => {
    if (!steps || steps.length === 0) return null;
    
    return (
      <div className="steps-container">
        <details>
          <summary>🔧 Tool Execution Steps ({steps.length})</summary>
          <div className="steps-list">
            {steps.map((step, idx) => (
              <div key={idx} className={`step step-${step.type}`}>
                {step.type === 'tool_call' ? (
                  <>
                    <span className="step-badge call">CALL</span>
                    <code>{step.tool}</code>
                    <pre>{JSON.stringify(step.args, null, 2)}</pre>
                  </>
                ) : (
                  <>
                    <span className="step-badge result">RESULT</span>
                    <code>{step.tool}</code>
                    <p>{step.preview}...</p>
                  </>
                )}
              </div>
            ))}
          </div>
        </details>
      </div>
    );
  };

  return (
    <div className="app">
      {/* Header */}
      <header className="header">
        <h1>🧬 Self-Evolving AI Agent</h1>
        <div className={`status ${connected ? 'online' : 'offline'}`}>
          {connected ? '🟢 Connected' : '🔴 Disconnected'}
        </div>
      </header>

      {/* Tool History Sidebar */}
      {toolHistory.length > 0 && (
        <div className="tool-history">
          <h3>🛠️ Evolved Tools</h3>
          <div className="tool-chips">
            {toolHistory.map((tool, idx) => (
              <span key={idx} className="tool-chip">{tool}</span>
            ))}
          </div>
        </div>
      )}

      {/* Messages */}
      <div className="messages">
        {messages.length === 0 && (
          <div className="welcome">
            <h2>Welcome!</h2>
            <p>I start with zero tools and evolve by writing my own.</p>
            <div className="examples">
              <p>Try asking me:</p>
              <button onClick={() => setInput('Create a PDF report about the solar system')}>
                "Create a PDF report about the solar system"
              </button>
              <button onClick={() => setInput('Draw a line chart of monthly sales: Jan=100, Feb=150, Mar=200, Apr=180')}>
                "Draw a line chart of monthly sales..."
              </button>
              <button onClick={() => setInput('Write a tool that calculates fibonacci numbers and compute fib(20)')}>
                "Write a fibonacci calculator tool..."
              </button>
            </div>
          </div>
        )}

        {messages.map((msg, idx) => (
          <div key={idx} className={`message ${msg.role}`}>
            <div className="message-header">
              {msg.role === 'user' ? '👤 You' : msg.role === 'assistant' ? '🤖 Agent' : '⚠️ System'}
            </div>
            <div className="message-content">
              {msg.content}
            </div>
            
            {/* Artifacts */}
            {msg.artifacts && msg.artifacts.length > 0 && (
              <div className="artifacts">
                {msg.artifacts.map((art, artIdx) => (
                  <div key={artIdx}>
                    {renderArtifact(art)}
                  </div>
                ))}
              </div>
            )}

            {/* Steps */}
            {msg.steps && renderSteps(msg.steps)}
          </div>
        ))}

        {isLoading && (
          <div className="message assistant loading">
            <div className="typing-indicator">
              <span></span>
              <span></span>
              <span></span>
            </div>
            <p>Agent is evolving tools...</p>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="input-area">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask me anything... (e.g., 'Create a PDF about...' or 'Draw a chart of...')"
          rows={2}
          disabled={isLoading}
        />
        <button 
          onClick={sendMessage} 
          disabled={isLoading || !input.trim()}
          className="send-btn"
        >
          {isLoading ? '⏳' : '➤'}
        </button>
      </div>
    </div>
  );
}

export default App;