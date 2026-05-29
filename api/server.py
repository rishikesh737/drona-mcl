"""
api.server — FastAPI application for streaming Drona to a frontend dashboard.
"""
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pathlib import Path

from core.agent import run_agent

logger = logging.getLogger("api.server")
app = FastAPI(title="Drona-MCL Streaming API")

BASE_DIR = Path(__file__).parent.parent

@app.get("/")
async def get_dashboard() -> FileResponse:
    """Serve the React dashboard on the root endpoint."""
    return FileResponse(BASE_DIR / "frontend" / "index.html")

@app.websocket("/ws/chat")
async def chat_endpoint(websocket: WebSocket) -> None:
    """
    WebSocket endpoint for streaming agent executions.
    
    Expects an initial message: {"task": "..."}
    Streams back JSON objects:
      - {"type": "start", "task": "..."}
      - {"type": "iteration", "iteration": 1, "max_iterations": 10}
      - {"type": "token", "content": "..."}
      - {"type": "think_token", "content": "..."}
      - {"type": "tool_call", "path": "A", "tool_name": "...", "arguments": {...}, "output": "..."}
      - {"type": "final_answer", "content": "..."}
      - {"type": "error", "content": "..."}
    """
    await websocket.accept()
    
    try:
        # Wait for the first message containing the task
        data = await websocket.receive_text()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            await websocket.send_json({"type": "error", "content": "Invalid JSON format."})
            await websocket.close()
            return
            
        task = payload.get("task")
        if not task:
            await websocket.send_json({"type": "error", "content": "Missing 'task' in payload."})
            await websocket.close()
            return

        logger.info(f"WebSocket client connected. Task: {task}")
        
        # Stream the agent's execution directly to the client
        async for event in run_agent(task):
            await websocket.send_json(event)
            
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.send_json({"type": "error", "content": str(e)})
        except:
            pass

@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
