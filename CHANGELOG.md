# NEXUS Remote Access System — v2 Changelog & Upgrade Guide

## What Changed (v1 → v2)

---

### 🔧 Bug Fixes

| File | Issue | Fix |
|------|-------|-----|
| `config/settings.py` | `RelaySettings` used `env_prefix="nexus-relay-"` — hyphens in env var names are invalid | Changed to `env_prefix="NEXUS_RELAY_"` |
| `config/settings.py` | TLS cert paths pointed to `server.crt/server.key` but `crypto.py` generates `nexus-relay-server.crt/key` | Paths now match actual generated filenames |
| `core/relay.py` | Binary frame routing had no ownership check — any authenticated connection could send frames to any session | Added controller_id / device_id verification before routing |
| `core/relay.py` | Used raw `websockets.serve()` bypassing the `WSServer` transport abstraction | Now uses `WSServer` from `transport/websocket_transport.py` |
| `agents/base_agent.py` | Used `await self._ws.send("ping")` text frame for heartbeat instead of native WS ping frames | Changed to `await self._ws.ping()` |
| `agents/server_agent.py` | `MobileAgent.on_terminal_open` called `ServerAgent.on_terminal_open(self, ...)` with `# type: ignore` — broken delegation | Refactored to use `_MobileDelegateAgent` composition |
| `ui/dashboard.py` | `create_session` endpoint called `auth_service.require_permission(token=..., ...)` with `token=...` which was `...` (Ellipsis) | Fixed to inline RBAC check via `has_permission()` |
| `ui/cli.py` | `shell` command ECDH didn't pass `info=` parameter — derived a different key than the agent | Now passes `info=f"nexus-session-{session_id}".encode()` matching agent derivation |

---

### 🆕 New Features

#### Ghost Mode (agents)
- All agents now support `--ghost` CLI flag or `NEXUS_GHOST=1` env var
- Redirects `stdout`/`stderr` to `/dev/null` / `NUL` **before any imports**
- Logging goes to `~/.nexus/agent.log` (file only, never console)
- `on_session_start()` / `on_session_end()` lifecycle hooks added to `BaseAgent` and all subclasses
- Desktop agent: push-mode screen streaming loop starts in `on_session_start()`, stops in `on_session_end()`

#### Service Deployment (`service/`)
- **`service/windows_service.py`** — NSSM-based Windows service installer
  - `install / uninstall / start / stop / status` commands
  - Sets `AppNoConsole=1`, log rotation, auto-restart
- **`service/linux_service.py`** — systemd unit generator + installer
  - Generates a complete `.service` file with `StandardOutput=null`
  - Security hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem`
  - Resource limits: `MemoryMax=256M`, `CPUQuota=25%`
  - `install / uninstall / start / stop / status / logs` commands

#### Relay improvements
- **Rate limiting**: IP-based auth rate limiter (10 req/min) + per-connection message limiter (500/10s)
- **`RelayStats`** dataclass: tracks connections, sessions, bytes relayed
- **`relay.stats`** WebSocket command: controller can query relay telemetry
- **Background sweep task**: stale devices + expired sessions cleaned up every 60s
- **Device busy check**: relay rejects session requests to `BUSY` devices (already in session)
- **Graceful session close notification**: both sides are notified when the other closes

#### CLI improvements
- `stats` command: shows relay health, device counts, active sessions
- `upload` command: scaffolded with Rich progress bar
- `download` command: scaffolded
- `resolve_device()`: matches by name OR ID (case-insensitive)
- Better error messages with `sys.exit(1)` on fatal errors

#### Config improvements
- `load_yaml_defaults()` function: merges `defaults.yaml` into environment before settings load
- `relay_url` property: returns correct `wss://` or `ws://` URL
- `api_url` property: returns correct `https://` or `http://` URL

#### Transport improvements
- `WSClient`: exponential back-off capped at 120s (was linear)
- `ConnectionPool.broadcast()` returns count of successful sends
- `ConnectionPool` stores per-connection metadata dict

---

### ⚠️ OS Indicator Warning (by design)

When a session is active:
- **Windows 10/11**: may show Remote Desktop / screen recording indicator in system tray
- **macOS**: green dot in menu bar for screen recording
- **Linux**: depends on desktop environment

These OS-level indicators **cannot be suppressed** — they are security features. Ghost Mode only means:
- No application window
- No popup or dialog
- No taskbar entry
- Agent starts before user login (system service)

---

## Deployment Quick-Start

### Step 1: Generate certificates
```bash
python -m utils.crypto --generate-certs --out ./certs
```

### Step 2: Start relay server
```bash
python -m core.relay --host 0.0.0.0 --port 7000
```

### Step 3: Start dashboard API
```bash
uvicorn ui.dashboard:app --host 0.0.0.0 --port 8080
```

### Step 4a: Run desktop agent (dev mode)
```bash
python -m agents.desktop_agent \
  --relay wss://your-relay:7000 \
  --device-id workstation-01 \
  --token YOUR_TOKEN
```

### Step 4b: Install as Ghost service (Windows)
```bash
# Run as Administrator
python service/windows_service.py install \
  --relay wss://your-relay:7000 \
  --device-id workstation-01 \
  --token YOUR_TOKEN

python service/windows_service.py start
```

### Step 4c: Install as Ghost service (Linux)
```bash
sudo python service/linux_service.py install \
  --relay wss://your-relay:7000 \
  --device-id server-01 \
  --token YOUR_TOKEN \
  --user nexus
```

### Step 5: Connect from CLI
```bash
# List devices
python -m ui.cli devices

# Open remote shell
python -m ui.cli shell --device workstation-01

# Check relay stats
python -m ui.cli stats
```

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXUS_RELAY_HOST` | `0.0.0.0` | Relay bind address |
| `NEXUS_RELAY_PORT` | `7000` | Relay WebSocket port |
| `NEXUS_AUTH_SECRET_KEY` | *(change this!)* | JWT signing key |
| `NEXUS_TLS_CERT_FILE` | `certs/nexus-relay-server.crt` | Server TLS cert |
| `NEXUS_TLS_KEY_FILE` | `certs/nexus-relay-server.key` | Server TLS key |
| `NEXUS_TLS_CA_FILE` | `certs/ca.crt` | CA cert (for mTLS) |
| `NEXUS_AGENT_SCREEN_FPS` | `15` | Screen capture FPS |
| `NEXUS_AGENT_SCREEN_QUALITY` | `75` | JPEG quality (1-100) |
| `NEXUS_GHOST` | `0` | Set `1` to enable ghost mode |
| `NEXUS_LOG_LEVEL` | `INFO` | Log level |
| `NEXUS_LOG_FORMAT` | `json` | `json` or `console` |
| `NEXUS_DEBUG` | `false` | Enable FastAPI /docs |
