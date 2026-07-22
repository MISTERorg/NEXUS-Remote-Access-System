# NEXUS Remote Access System v2

A cross-platform, enterprise-grade remote access and control system built on WebSocket transport with end-to-end encryption, ghost-mode agent deployment, and OS service integration.

## Features

- **Ghost Mode** — agents run silently as system services, starting before user login with no GUI artifacts
- **End-to-End Encryption** — ECDH (P-256) key exchange + AES-256-GCM per-session encryption
- **Multi-Agent Types** — desktop (screen/input), server (headless terminal), IoT/mobile (constrained)
- **Role-Based Access Control** — ADMIN, OPERATOR, VIEWER, AGENT roles with JWT authentication
- **Cross-Platform Services** — Windows (NSSM) and Linux (systemd) service installers
- **Browser Dashboard** — FastAPI-powered REST API + real-time WebSocket proxy console
- **CLI Controller** — Interactive remote shell, file transfer, device management
- **MFA Support** — Optional TOTP two-factor authentication
- **Audit Logging** — Structured JSON logs with full security event trail
- **Rate Limiting** — IP-based auth throttling and per-connection message limits

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  OPERATOR                                                    │
│  Browser / CLI / Dashboard                                   │
└───────────────────┬──────────────────────────────────────────┘
                    │ HTTPS/WSS + JWT
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  DASHBOARD API  (FastAPI)                                    │
│  /auth  /devices  /sessions  /ws/controller                  │
└───────────────────┬──────────────────────────────────────────┘
                    │ WSS relay proxy
                    ▼
┌──────────────────────────────────────────────────────────────┐
│  RELAY SERVER  (core/relay.py)                               │
│  WebSocket hub · Device registry · Session manager          │
│  Rate limiting · Heartbeat monitor · Binary frame routing    │
└────────────┬────────────────┬──────────────────┬─────────────┘
             │                │                  │
             ▼                ▼                  ▼
        DesktopAgent     ServerAgent         IoTAgent
        (ghost mode)     (headless)         (constrained)
```

**Encryption flow:** ECDH public key exchange → HKDF shared secret (scoped to session ID) → AES-256-GCM on all subsequent frames with prepended nonce.

## Tech Stack

| Layer | Libraries |
|-------|-----------|
| Web framework | FastAPI, Uvicorn |
| WebSocket | websockets (asyncio) |
| Encryption | cryptography (AES-256-GCM, ECDH P-256) |
| Auth | PyJWT (HS256), bcrypt, TOTP |
| Config | Pydantic Settings, YAML |
| Database | SQLite + aiosqlite |
| CLI | Click, Rich |
| Screen capture | MSS |
| Input injection | pynput |
| System metrics | psutil |
| Builds | PyInstaller |

## Project Structure

```
nexus-ras-v2/
├── agents/               # Remote access agents (desktop, server, IoT/mobile)
├── config/               # Pydantic settings + defaults.yaml
├── core/                 # Relay server, auth, device registry, session lifecycle
├── transport/            # WebSocket/TCP transport, TLS context, port tunneling
├── ui/                   # FastAPI dashboard, browser console HTML, Click CLI
├── utils/                # Crypto helpers, structured logger, heartbeat
├── service/              # Windows NSSM + Linux systemd service installers
├── setup_and_launch.py   # One-click setup and launcher
├── connect_remote.py     # Zero-touch remote agent connector
└── build_standalone.py   # PyInstaller .exe builder
```

## Quick Start

**Requirements:** Python 3.8+

```bash
git clone <repo-url>
cd nexus-ras-v2
pip install -r requirements.txt

# One-click setup: generates certs, initialises DB, starts relay + dashboard
python setup_and_launch.py
# Opens browser to http://localhost:8080
```

Reset everything and start fresh:

```bash
python setup_and_launch.py --reset
```

Start services without launching the browser:

```bash
python setup_and_launch.py --no-browser
```

## Configuration

Configuration is loaded in priority order: environment variables > `.env` file > `config/defaults.yaml`.

Copy `.env` and adjust for your environment:

```bash
cp .env .env.local
```

Key variables:

```env
NEXUS_RELAY_HOST=0.0.0.0
NEXUS_RELAY_PORT=7000
NEXUS_AUTH_SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
NEXUS_TLS_CERT_FILE=certs/nexus-relay-server.crt
NEXUS_TLS_KEY_FILE=certs/nexus-relay-server.key
NEXUS_TLS_CA_FILE=certs/ca.crt
NEXUS_AGENT_SCREEN_FPS=15
NEXUS_AGENT_SCREEN_QUALITY=75
NEXUS_DEBUG=false
NEXUS_LOG_LEVEL=INFO
NEXUS_LOG_FORMAT=json
```

## Running Components Individually

```bash
# Relay server
python -m core.relay --host 0.0.0.0 --port 7000

# Dashboard API
uvicorn ui.dashboard:app --host 0.0.0.0 --port 8080

# Desktop agent (connects to relay)
python -m agents.desktop_agent \
  --relay wss://your-server:7000 \
  --device-id workstation-01 \
  --token <agent-token>

# Server/headless agent
python -m agents.server_agent \
  --relay wss://your-server:7000 \
  --device-id server-01 \
  --token <agent-token>
```

## CLI Usage

```bash
python -m ui.cli devices              # List registered devices
python -m ui.cli devices --online-only
python -m ui.cli sessions             # Active sessions
python -m ui.cli stats                # Relay health and metrics
python -m ui.cli shell --device <id>  # Interactive remote terminal
python -m ui.cli upload --device <id> --src ./file --dst /remote/path
python -m ui.cli download --device <id> --src /remote/file --dst ./local/
```

## Service Deployment

### Windows (NSSM)

```bash
# Install as a Windows service (runs before user login)
python service/windows_service.py install \
  --relay wss://your-server:7000 \
  --device-id workstation-01 \
  --token <agent-token>

python service/windows_service.py start
python service/windows_service.py status
python service/windows_service.py uninstall
```

### Linux (systemd)

```bash
sudo python service/linux_service.py install \
  --relay wss://your-server:7000 \
  --device-id server-01 \
  --token <agent-token> \
  --user nexus

sudo python service/linux_service.py start
sudo python service/linux_service.py logs
sudo python service/linux_service.py uninstall
```

The generated systemd unit includes security hardening: `NoNewPrivileges`, `ProtectSystem=strict`, 256 MB memory limit, and 25% CPU quota.

## Zero-Touch Remote Deployment

Send `connect_remote.py` to a remote machine along with the connection instructions in `SEND_TO_REMOTE_PC.txt`. The script reads credentials from the state file and registers the agent automatically:

```bash
python connect_remote.py
```

## Standalone Build

Build a single-file executable (no Python required on target):

```bash
python build_standalone.py
# Output: dist/nexus-agent.exe (Windows) or dist/nexus-agent (Linux/macOS)
```

## Security

| Control | Implementation |
|---------|---------------|
| Transport encryption | TLS 1.2+ (optional mTLS with client certificates) |
| Session encryption | ECDH P-256 + HKDF → AES-256-GCM |
| Password storage | bcrypt |
| Tokens | JWT HS256, 60-minute TTL with refresh |
| MFA | TOTP (RFC 6238) |
| Login lockout | 5 failed attempts → 15-minute block |
| Rate limiting | 10 auth req/min per IP, 500 msg/10s per connection |
| Audit log | `logs/audit.log` — all auth and session events |
| RBAC | ADMIN / OPERATOR / VIEWER / AGENT |

> **Note:** OS-level recording indicators (Windows, macOS) cannot be suppressed by design — this is intentional for endpoint transparency.

**Before deploying to production:**
- Rotate `NEXUS_AUTH_SECRET_KEY` to a fresh 32-byte secret
- Replace self-signed certs with CA-issued certificates or configure mTLS
- Set `NEXUS_DEBUG=false`

## API Reference

The dashboard exposes a REST + WebSocket API when running:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/auth/login` | POST | Obtain JWT access + refresh tokens |
| `/auth/refresh` | POST | Refresh access token |
| `/auth/logout` | POST | Invalidate session |
| `/devices` | GET | List registered devices |
| `/devices/{id}` | GET | Device detail and metrics |
| `/sessions` | GET/POST | List or create sessions |
| `/sessions/{id}` | DELETE | Close a session |
| `/ws/controller` | WS | Real-time screen + input proxy |
| `/health` | GET | Relay health check |

Interactive docs available at `http://localhost:8080/docs` when `NEXUS_DEBUG=true`.

## Known Limitations

- No P2P (WebRTC roadmap) — all traffic routes through the relay
- File transfer optimised for files under ~500 MB
- Clipboard sync is text-only (no binary objects)
- Session recording infrastructure exists but playback is not yet implemented

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the v1 → v2 migration guide and full list of bug fixes and new features.

## License

Private / proprietary. All rights reserved.
