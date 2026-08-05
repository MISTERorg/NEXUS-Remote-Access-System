"""
ui/dashboard.py
---------------
NEXUS Web Dashboard — FastAPI REST API for auth, device listing, and
read-only session listing.

Session creation, session closing, and all session-scoped traffic (screen
frames, input, terminal, file, clipboard) are now owned ENTIRELY by
core/relay.py's own WebSocket listener — the browser connects directly to
the relay (see relay.py's _handle_session_request / _CONTROLLER_SESSION_MSG_TYPES),
not to this process. This file used to also expose a POST /sessions +
/ws/controller pair that created Session objects in-process, but that path
had no way to reach relay.py's in-memory agent-connection registry to
actually notify the agent a session existed — sessions created that way
were created (visible in session_manager) but never went ACTIVE. Removed
rather than left as a trap; GET /sessions is kept since it's a harmless
read of the same shared session_manager singleton regardless of which
process created the session.

Endpoints:
  POST   /auth/login            — get JWT tokens
  POST   /auth/refresh          — refresh access token
  POST   /auth/logout           — revoke token
  POST   /auth/register         — admin-only: create a new operator user
  GET    /devices               — list all registered devices
  GET    /devices/{id}          — device detail + metrics
  GET    /devices/search/{query}— search devices by query
  GET    /sessions              — list active sessions (read-only)
  GET    /health                — health check
  GET    /console                — serve dashboard.html
  GET    /static/*               — serve dashboard.css + the per-feature
                                    dashboard JS modules (ui/static/js/*.js)

Static assets (ui/static/) are one feature-module-per-file (state, api,
auth, views, relay, sessions, remote_desktop, clipboard, terminal,
file_manager, devices, poller, av_control — loaded in that dependency
order by dashboard.html, plus boot.js last). They're plain classic
<script> files sharing one global scope on purpose, matching how
dashboard.html's inline onclick="Module.method()" handlers already
call them — splitting them into ES modules would require rewriting
every onclick handler in the HTML for no functional benefit here.

Start with:
    uvicorn ui.dashboard:app --host 0.0.0.0 --port 8080 --reload
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.settings import settings
from core.auth import AuthError, Role, auth_service
from core.registry import device_registry
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


def _get_bundle_dir() -> Path:
    """Get base directory for static assets, supporting frozen builds and dev mode."""
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


_BUNDLE_DIR = _get_bundle_dir()
_MODULE_DIR = Path(__file__).resolve().parent

# Every candidate is a "ui/static" directory (or its frozen-build
# equivalent) — dashboard.html, dashboard.css, and js/*.js all live
# together under whichever one of these actually exists, so one lookup
# finds the whole asset set instead of three independent ones.
_STATIC_DIR_CANDIDATES = [
    # PyInstaller / frozen executable paths
    _BUNDLE_DIR / "ui" / "static",
    _BUNDLE_DIR / "static",
    Path(sys.executable).parent / "ui" / "static",
    Path(sys.executable).parent / "static",
    # Local dev / source paths
    _MODULE_DIR / "static",
    _MODULE_DIR.parent / "static",
]


def _find_static_dir() -> Optional[Path]:
    for candidate in _STATIC_DIR_CANDIDATES:
        if (candidate / "dashboard.html").exists():
            return candidate
    return None


_STATIC_DIR = _find_static_dir()

if _STATIC_DIR:
    # Serves dashboard.css and js/*.js (one file per dashboard feature
    # module — see ui/static/js/) at /static/...
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
else:
    log.warning(
        "dashboard.static_dir_not_found",
        checked=[str(p) for p in _STATIC_DIR_CANDIDATES],
    )


@app.get("/console", include_in_schema=False)
async def console():
    if not _STATIC_DIR:
        raise HTTPException(
            status_code=404,
            detail=(
                "ui/static (dashboard.html) not found. Checked: "
                + ", ".join(str(p) for p in _STATIC_DIR_CANDIDATES)
            ),
        )
    return FileResponse(_STATIC_DIR / "dashboard.html", media_type="text/html")


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


@app.get("/sessions")
async def list_sessions(payload=Depends(require_auth)):
    """
    Read-only. Sessions themselves are created/closed via relay.py's own
    WebSocket listener now — this just reads the same shared session_manager
    singleton (same process, see setup_and_launch.py) so the dashboard's
    Active Sessions view stays accurate regardless of which connection
    created a given session.
    """
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