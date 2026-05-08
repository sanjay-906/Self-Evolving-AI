import json
import base64
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import run_agent, SANDBOX_DIR, OUTPUTS_DIR, TOOLS_DIR


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    response: str
    artifacts: list[dict]
    tool_history: list[str]
    steps: list[dict]


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Sandbox ready at: {SANDBOX_DIR}")
    yield

    print("Server shutting down")


app = FastAPI(
    title="Self-Evolving AI Agent",
    description="An agent that writes its own tools at runtime",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for React dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "Self-Evolving AI Agent is running", "sandbox": str(SANDBOX_DIR)}


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """
    HTTP endpoint for chat. Use this for simple integrations.
    """
    try:
        result = run_agent(request.message, request.history)
        return ChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """
    WebSocket endpoint for real-time streaming chat.
    Receives: JSON {"message": "...", "history": [...]}
    Sends: JSON {"type": "...", "data": ...}
    """
    await manager.connect(websocket)
    try:
        while True:
            # Receive message from client
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            message = payload.get("message", "")
            history = payload.get("history", [])

            # Send "thinking" status
            await websocket.send_json({
                "type": "status",
                "data": {"status": "thinking", "message": "Agent is analyzing the task..."}
            })

            # Run the agent
            result = run_agent(message, history)

            # Send the final response
            await websocket.send_json({
                "type": "complete",
                "data": result
            })

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "data": {"error": str(e)}
        })
        manager.disconnect(websocket)


@app.get("/artifacts/{filename}")
async def get_artifact(filename: str):
    """
    Serve an artifact file (PDF, PNG, etc.) from the outputs directory.
    """
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream"
    )


@app.get("/artifacts/base64/{filename}")
async def get_artifact_base64(filename: str):
    """
    Get an artifact as base64 (useful for embedding in chat UI).
    """
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    data = file_path.read_bytes()
    b64 = base64.b64encode(data).decode()

    # Determine media type
    ext = file_path.suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return {
        "filename": filename,
        "base64": b64,
        "media_type": media_type,
        "size_bytes": len(data),
    }


@app.get("/tools")
async def list_tools():
    """
    List all dynamically created tools.
    """
    from agent import tool_registry
    return {
        "tools": tool_registry.list_names(),
        "sources": {name: tool_registry._tool_source.get(name, "") for name in tool_registry.list_names()}
    }


@app.delete("/tools/{tool_name}")
async def delete_tool(tool_name: str):
    """
    Delete a dynamically created tool.
    """
    from agent import tool_registry
    if tool_name not in tool_registry._tools:
        raise HTTPException(status_code=404, detail="Tool not found")

    del tool_registry._tools[tool_name]
    if tool_name in tool_registry._tool_source:
        del tool_registry._tool_source[tool_name]

    tool_file = TOOLS_DIR / f"{tool_name}.py"
    if tool_file.exists():
        tool_file.unlink()

    return {"message": f"Tool '{tool_name}' deleted"}


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
