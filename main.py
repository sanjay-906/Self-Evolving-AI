import json
import base64
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from agent import run_agent, SANDBOX_DIR, OUTPUTS_DIR, TOOLS_DIR


class AsyncQueueHandler(logging.Handler):
    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self.queue = queue

    def emit(self, record: logging.LogRecord):
        try:
            self.queue.put_nowait({
                "type": "log",
                "data": {
                    "level": record.levelname,
                    "logger": record.name,
                    "message": self.format(record),
                    "ts": record.created,
                }
            })
        except asyncio.QueueFull:
            pass


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    response: str
    artifacts: list[dict]
    tool_history: list[str]
    steps: list[dict]


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
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"status": "Agent is running", "sandbox": str(SANDBOX_DIR)}


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    try:
        result = run_agent(request.message, request.history)
        return ChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/artifacts/{filename}")
async def get_artifact(filename: str):
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=file_path, filename=filename, media_type="application/octet-stream")


@app.get("/artifacts/base64/{filename}")
async def get_artifact_base64(filename: str):
    file_path = OUTPUTS_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    data = file_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    ext = file_path.suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
    }
    return {
        "filename": filename,
        "base64": b64,
        "media_type": media_types.get(ext, "application/octet-stream"),
        "size_bytes": len(data),
    }


@app.get("/tools")
async def list_tools():
    from agent import tool_registry
    return {
        "tools": tool_registry.list_names(),
        "sources": {
            name: tool_registry._tool_source.get(name, "")
            for name in tool_registry.list_names()
        }
    }


@app.delete("/tools/{tool_name}")
async def delete_tool(tool_name: str):
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


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            message = payload.get("message", "")
            history = payload.get("history", [])

            log_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
            handler = AsyncQueueHandler(log_queue)
            handler.setFormatter(logging.Formatter("%(message)s"))

            agent_logger = logging.getLogger("agent")
            agent_logger.addHandler(handler)

            await websocket.send_json({
                "type": "status",
                "data": {"status": "thinking", "message": "Initialising agent…"}
            })

            loop = asyncio.get_event_loop()
            agent_task = loop.run_in_executor(None, run_agent, message, history)

            SENTINEL = object()

            async def drain_logs():
                while True:
                    try:
                        item = await asyncio.wait_for(log_queue.get(), timeout=0.05)
                        if item is SENTINEL:
                            break
                        await websocket.send_json(item)
                    except asyncio.TimeoutError:
                        if agent_task.done():
                            while not log_queue.empty():
                                try:
                                    item = log_queue.get_nowait()
                                    if item is not SENTINEL:
                                        await websocket.send_json(item)
                                except asyncio.QueueEmpty:
                                    break
                            break

            drain_task = asyncio.ensure_future(drain_logs())

            try:
                result = await agent_task
            except Exception as e:
                agent_logger.removeHandler(handler)
                await drain_task
                await websocket.send_json({
                    "type": "error",
                    "data": {"error": str(e)}
                })
                continue

            agent_logger.removeHandler(handler)
            await drain_task

            if result.get("artifacts"):
                await websocket.send_json({
                    "type": "artifacts",
                    "data": result["artifacts"]
                })

            await websocket.send_json({
                "type": "complete",
                "data": result
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "data": {"error": str(e)}})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
