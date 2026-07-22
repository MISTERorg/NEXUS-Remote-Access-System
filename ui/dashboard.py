"""
ui/dashboard.py
---------------
NEXUS Web Dashboard — FastAPI-powered REST API + WebSocket relay for the browser UI.

Endpoints:
  POST   /auth/login            — get JWT tokens
  POST   /auth/refresh          — refresh access token
  POST   /auth/logout           — revoke token
  GET    /devices               — list all registered devices
  GET    /devices/{id}          — device detail + metrics
  GET    /devices/search/{query}— search devices by query
  POST   /sessions              — open a new remote session
  DELETE /sessions/{id}       — close a session
  GET    /sessions              — list active sessions
  WS     /ws/controller         — controller WebSocket (proxies to relay)
  GET    /health                — health check

Start with:
    uvicorn ui.dashboard:app --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from config.settings import settings
from core.auth import AuthError, Role, auth_service
from core.registry import DeviceStatus, device_registry
from core.session import session_manager
from utils.logger import get_logger, setup_logging

setup_logging(
    level=settings.log.level,
    fmt=settings.log.format,
    log_file=settings.log.file,
)
log = get_logger("nexus.dashboard")

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.auth.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bearer_scheme = HTTPBearer()


_CONSOLE_CANDIDATES = [
    Path(__file__).resolve().parent / "dashboard.html",
    Path(__file__).resolve().parent / "console.html",
    Path(__file__).resolve().parent.parent / "dashboard.html",
]


def _find_console_html() -> Optional[Path]:
    for candidate in _CONSOLE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


@app.get("/console", include_in_schema=False)
async def console():
    path = _find_console_html()
    if not path:
        raise HTTPException(
            status_code=404,
            detail=(
                "dashboard.html not found. Checked: "
                + ", ".join(str(p) for p in _CONSOLE_CANDIDATES)
            ),
        )
    return FileResponse(path, media_type="text/html")


def get_token(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> str:
    return creds.credentials


def require_auth(token: str = Depends(get_token)):
    try:
        return auth_service.verify_access_token(token)
    except AuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


def require_admin(payload=Depends(require_auth)):
    if payload.role != Role.ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin required")
    return payload


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = "operator"


@app.post("/auth/login")
async def login(req: LoginRequest):
    try:
        tokens = auth_service.authenticate(req.username, req.password, req.totp_code)
        return tokens
    except AuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@app.post("/auth/refresh")
async def refresh(token: str = Depends(get_token)):
    try:
        return auth_service.refresh(token)
    except AuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@app.post("/auth/logout")
async def logout(token: str = Depends(get_token)):
    auth_service.logout(token)
    return {"status": "logged_out"}


@app.post("/auth/register", dependencies=[Depends(require_admin)])
async def register_user(req: RegisterRequest):
    try:
        user = auth_service.register_user(req.username, req.password, Role(req.role))
        return {"user_id": user.user_id, "username": user.username, "role": user.role}
    except AuthError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.get("/devices")
async def list_devices(
    online_only: bool = False,
    payload=Depends(require_auth),
):
    if online_only:
        devices = await device_registry.list_online()
    else:
        devices = await device_registry.list_all()
    return {"devices": [d.model_dump() for d in devices], "total": len(devices)}


@app.get("/devices/{device_id}")
async def get_device(device_id: str, payload=Depends(require_auth)):
    device = await device_registry.get(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    metrics = await device_registry.get_metrics(device_id)
    return {
        "device": device.model_dump(),
        "metrics": metrics.model_dump() if metrics else None,
    }


@app.get("/devices/search/{query}")
async def search_devices(query: str, payload=Depends(require_auth)):
    devices = await device_registry.search(query)
    return {"devices": [d.model_dump() for d in devices]}


class SessionRequest(BaseModel):
    device_id: str


@app.post("/sessions")
async def create_session(req: SessionRequest, payload=Depends(require_auth)):
    from core.auth import Role, has_permission
    if not has_permission(Role(payload.role), "session.open"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permission denied: session.open")

    device = await device_registry.get(req.device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.status == DeviceStatus.OFFLINE:
        raise HTTPException(status_code=409, detail="Device is offline")

    session = await session_manager.create(
        controller_id=payload.sub,
        device_id=req.device_id,
        controller_role=Role(payload.role),
    )
    return {"session_id": session.session_id, "state": session.state}


@app.delete("/sessions/{session_id}")
async def close_session(session_id: str, payload=Depends(require_auth)):
    session = await session_manager.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.controller_id != payload.sub and payload.role != Role.ADMIN.value:
        raise HTTPException(status_code=403, detail="Not your session")
    await session_manager.close(session_id, reason="api_closed")
    return {"status": "closed", "session_id": session_id}


@app.get("/sessions")
async def list_sessions(payload=Depends(require_auth)):
    sessions = await session_manager.list_active()
    return {"sessions": [s.summary() for s in sessions]}


@app.get("/health")
async def health():
    all_devs = await device_registry.list_all()
    online_devs = await device_registry.list_online()
    active_sess = await session_manager.list_active()
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "version": settings.version,
        "devices_total": len(all_devs),
        "devices_online": len(online_devs),
        "active_sessions": len(active_sess),
    }


@app.websocket("/ws/controller")
async def websocket_controller(websocket: WebSocket):
    await websocket.accept()
    authenticated_payload = None
    try:
        init_data = await websocket.receive_text()
        msg = json.loads(init_data)
        token = msg.get("token")
        if not token:
            await websocket.send_json({"type": "error", "message": "Missing token"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        authenticated_payload = auth_service.verify_access_token(token)
        await websocket.send_json({"type": "auth.ok", "user_id": authenticated_payload.sub})

        while True:
            raw_text = await websocket.receive_text()
            data = json.loads(raw_text)
            msg_type = data.get("type")
            session_id = data.get("session_id")
            payload_data = data.get("payload")

            if session_id:
                session = await session_manager.get(session_id)
                if session:
                    if msg_type == "terminal_data" and payload_data:
                        log.info("terminal.command", session_id=session_id, cmd=str(payload_data)[:80])
                        await websocket.send_json({
                            "type": "terminal_data",
                            "session_id": session_id,
                            "payload": f"Executing: {payload_data}"
                        })
                    elif msg_type == "file_list":
                        await websocket.send_json({
                            "type": "file_list",
                            "session_id": session_id,
                            "payload": []
                        })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.error("websocket.error", error=str(e))
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass