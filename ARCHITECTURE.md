# NEXUS Architecture & File Reference

This document is the detailed technical companion to the README. It covers the full directory layout, what every file is responsible for, the wire protocol between browser/relay/agent, and the security model. If you're onboarding onto this codebase, read this once end to end before touching `core/relay.py` or `core/session.py` — the session lifecycle has a specific ownership model that's easy to accidentally violate.

---

## 1. Directory layout

```
nexus/
├── agents/
│   ├── base_agent.py          # Abstract base: transport, auth, heartbeat, reconnect, dispatch
│   ├── desktop_agent.py        # DesktopAgent(BaseAgent) — screen/input/terminal/files/clipboard
│   └── server_agent.py         # ServerAgent / IoTAgent / MobileAgent (BaseAgent) — headless variants
│
├── core/
│   ├── relay.py                 # RelayServer — the ONE WebSocket listener. Owns session lifecycle.
│   ├── session.py                # Session / SessionManager / SessionMessage / MessageType
│   ├── auth.py                   # AuthService, JWT issuance/verification, RBAC roles+permissions
│   └── registry.py               # DeviceRegistry — tracks every agent that's ever connected
│
├── config/
│   ├── settings.py                # Pydantic Settings — env-var driven, with YAML defaults loader
│   └── defaults.yaml              # Baseline config values (overridden by NEXUS_* env vars)
│
├── utils/
│   ├── crypto.py                   # AESGCMCipher, ECDHKeyExchange, password hashing
│   ├── logger.py                   # structlog setup + AuditLogger (security event trail)
│   └── heartbeat.py                # HeartbeatManager (liveness) + RateLimiter (not yet wired up — see §6)
│
├── transport/
│   ├── websocket_transport.py       # WSServer/WSClient/ConnectionPool wrappers (generic, reusable)
│   ├── tcp_transport.py              # Raw TCP + length-prefixed framing (fallback transport, unused by default path)
│   ├── tls_context.py                # TLSContextFactory — builds server/client/mTLS SSL contexts
│   └── tunnel.py                     # ReverseTunnel, SSHTunnel, TunnelManager (NAT traversal helpers)
│
├── ui/
│   ├── dashboard.py                  # FastAPI REST API: auth, device listing, session listing, health
│   └── dashboard.html                # Operator console — connects DIRECTLY to the relay's WebSocket
│
├── connect_remote.py                 # Agent-side connector: auto-discovers SEND_TO_REMOTE_PC.txt, tests endpoints, launches DesktopAgent
├── nexus_agent_entry.py               # PyInstaller entry point for nexus_agent.exe (prompts for relay/token if not given)
├── build_standalone.py                 # PyInstaller build script that produces nexus_agent.exe
└── setup_and_launch.py                  # Host-side setup: IP discovery, cert gen, UPnP, tunnel, launches relay+dashboard
```

---

## 2. The core design decision: the relay owns sessions, not the dashboard

This is the single most important thing to understand before changing anything.

Early in this project's life, session creation lived in the dashboard's REST API (`POST /sessions`), and the browser talked to a separate WebSocket endpoint hosted by the dashboard process. That split turned out to be a real, load-bearing bug: the dashboard process had no way to reach the relay's in-memory table of live agent connections, so a session "created" via REST could never actually notify the agent it existed. The session would sit forever in a `PENDING` state, and no frame, keystroke, or file byte would ever move.

**The fix, and the current design:** `core/relay.py` is the *only* WebSocket endpoint either the browser or the agent ever connects to. It owns the entire session lifecycle — creation, handshake, routing, and teardown — in one process, with direct access to every live connection. `ui/dashboard.py` is now pure REST: login, device listing, read-only session listing, health. It has no WebSocket route at all and creates zero sessions. If you're tempted to add session-related logic to `dashboard.py` again, don't — route it through `relay.py` instead, or the same bug comes back.

---

## 3. Wire protocol

### 3.1 Connection roles

Every WebSocket connection to the relay identifies itself as exactly one of:

| Identity | Who | Auth message |
|---|---|---|
| `controller` | The browser dashboard (one persistent connection per logged-in operator) | `{"type": "auth.controller", "token": "<JWT>"}` |
| `agent` | The DesktopAgent/ServerAgent/etc. running on a managed machine | `{"type": "auth.agent", "device_id": ..., "token": ..., "name": ..., "capabilities": {...}, ...}` |

A controller connection is looked up later by `user_id` (the JWT subject) — one browser connection per logged-in operator, reused for every device that operator connects to. An agent connection is looked up by `device_id`.

### 3.2 Session lifecycle (all over the controller's WebSocket connection)

```
Browser                          Relay                              Agent
   │                                │                                  │
   │── session.request ────────────▶│                                  │
   │   {device_id}                  │                                  │
   │                                │── session.request ──────────────▶│
   │◀── session.pending ─────────────│   {session_id, ecdh_key}         │
   │   {session_id}                 │                                  │
   │                                │◀── session.accept ────────────────│
   │                                │   {session_id, ecdh_key}          │
   │◀── session.active ──────────────│  (or session.reject → relayed    │
   │   {session_id, ecdh_key}        │   as session.rejected)           │
   │                                │                                  │
   │  ── session-scoped traffic (see §3.3) flows both ways, relayed ──  │
   │                                │                                  │
   │── session.close ───────────────▶│                                  │
   │   {session_id}      (either side can send this; relay.py's        │
   │                       _notify_session_closed fires for EVERY       │
   │                       close path — explicit close, idle timeout,   │
   │                       or an agent disconnecting — and tells        │
   │                       whichever side didn't initiate it)           │
```

**Where the encryption actually happens:** the `ecdh_key` in `session.request`/`session.accept` is real ECDH key material — but it's exchanged between the `Session` object (server-side, inside the relay process) and the **agent**. The browser never participates in that exchange and never holds the derived AES-256-GCM key. This is intentional: `Session.attach_controller()` wires the browser side to plain JSON in/out, while `Session.attach_agent()` wires the agent side to encrypted bytes in/out. The AES-GCM layer protects the one hop that actually leaves the relay process and crosses a real network boundary to a remote machine; the browser↔relay hop is protected by WSS/TLS instead. See `core/session.py`'s `Session.__init__` docstring for the full reasoning — this was a specific, considered design decision, not an oversight.

### 3.3 Session-scoped messages

Once a session is `ACTIVE`, both sides exchange flat JSON matching `SessionMessage`: `{"type": ..., "session_id": ..., "payload": {...}}`. The full `MessageType` set (`core/session.py`):

| Type | Direction | Payload |
|---|---|---|
| `screen_request` | Controller → Agent | `{quality, scale}` — pull-mode single-frame request |
| `screen_frame` | Agent → Controller | `{frame: <hex JPEG>, timestamp}` — pushed continuously (~15 fps) once the session starts, *and* sent on-demand in response to `screen_request` |
| `mouse_event` | Controller → Agent | `{action: move\|press\|release\|click\|scroll, x, y, button, ...}` |
| `key_event` | Controller → Agent | `{action: press\|release\|tap, key}` |
| `terminal_open` | Controller → Agent | `{terminal_id}` — spawns a real shell subprocess |
| `terminal_data` | both directions | `{terminal_id, data}` — keystrokes in, stdout/stderr out |
| `terminal_close` | both directions | `{terminal_id}` |
| `file_list` | Controller → Agent | `{path}` → agent replies `file_list` with `{path, entries: [...]}` |
| `file_download_start` / `file_upload_start` / `_chunk` / `_end` | both | chunked file transfer |
| `clipboard_get` / `clipboard_set` | both | `{text}` |
| `ping` / `pong` | both | keepalive at the session level (separate from the WS-level heartbeat) |

Permission enforcement happens per message type in `Session._route_to_agent`, checked against the controller's RBAC role (`core/auth.py`) before anything reaches the agent — a `viewer` role, for example, can open a session and watch the screen but every input/terminal/file message is rejected server-side regardless of what the browser sends.

### 3.4 Binary framing (agent ↔ relay only)

The controller side never sees this — it's plain JSON per §3.3. Between the relay and the agent, `BaseAgent._send_session` prepends the 36-byte ASCII session_id to every encrypted binary WebSocket frame:

```
[ 36 bytes: session_id (ASCII UUID) ][ AES-256-GCM ciphertext ]
```

`relay.py`'s `_route_binary` strips that prefix to identify which `Session` to route through, then hands the *bare* ciphertext to `Session.handle_from_agent()` for decryption (AAD = the session_id's UTF-8 bytes). Frames going the other direction (relay → agent) do **not** carry the prefix — the agent already knows its own current session_id. If you're debugging a "screen never appears" issue, this asymmetry is the first thing to check: agent→relay frames are prefixed, relay→agent frames are not.

---

## 4. File-by-file reference

### `agents/base_agent.py`
Abstract base class every device type subclasses. Owns: WebSocket connection + reconnect with exponential backoff, `auth.agent` handshake, the heartbeat/ping loop, ECDH handshake completion on `session.request`, and the `_dispatch_session_msg` table that turns incoming `MessageType` values into calls to abstract hooks (`on_screen_request`, `on_mouse_event`, `on_terminal_open`, etc.) that subclasses implement. Also handles the top-level `session.close` message from the relay — this is what tears down a subclass's resources (screen loop, open terminals) when a session ends from the *relay's* side, as opposed to the subclass's own session-scoped `MessageType.CLOSE` handling.

### `agents/desktop_agent.py`
The full-capability agent for desktop/laptop machines. Implements every `BaseAgent` hook: MSS-based screen capture (with a persistent capture context reused across frames, not recreated per-frame), a continuous ~15fps push loop started in `on_session_start()`, pynput-based mouse/keyboard replay, a real subprocess-backed terminal (PowerShell on Windows, `$SHELL` elsewhere), directory listing/file read-write, and platform-specific clipboard access (PowerShell/pbcopy-pbpaste/wl-copy-xclip depending on OS).

### `agents/server_agent.py`
`ServerAgent`, `IoTAgent`, and `MobileAgent` — thinner `BaseAgent` subclasses for headless or constrained device types. Same hook pattern as `DesktopAgent`, with capabilities scaled down appropriately (e.g. no screen share on a pure IoT device).

### `core/relay.py`
The center of the system. A single `websockets`-based server that both agents and controllers connect to. Owns: connection authentication (`auth.controller` / `auth.agent`), the entire session lifecycle (§3.2), binary frame routing between agent and session (§3.4), device registry updates on connect/disconnect, and a centralized close-notification listener (`_notify_session_closed`, registered via `session_manager.on_close()`) that fires for *every* way a session can end — explicit close, idle timeout, or an agent disconnecting — so both sides always find out, not just whichever path happened to trigger it.

### `core/session.py`
`Session` — one instance per active remote-control session. Holds the ECDH keypair and derived AES-GCM cipher, tracks state (`PENDING → HANDSHAKING → ACTIVE → CLOSING → CLOSED`), and provides two distinct attachment interfaces: `attach_agent()` (encrypted bytes, for the genuinely remote leg) and `attach_controller()` (plain JSON, for the in-process/WSS-protected browser leg — see §3.2). `SessionManager` is the process-wide registry of active sessions plus the `on_close` listener mechanism `relay.py` hooks into.

### `core/auth.py`
JWT issuance and verification (`TokenManager`), password hashing/verification (delegated to `utils/crypto.py`), TOTP MFA support (via `pyotp` if installed, otherwise MFA checks are skipped with a warning — see §6), login attempt tracking with lockout, and the RBAC model: `Role` enum (`admin`/`operator`/`viewer`/`agent`) plus `ROLE_PERMISSIONS` mapping each role to a permission list, checked by `has_permission()`.

### `core/registry.py`
`DeviceRegistry` — an in-memory, process-local store of every `DeviceInfo` (name, OS, capabilities, status, last-seen) any agent has ever registered with, plus a listener/notify pattern (`on_change`) that `core/session.py`'s close-notification design mirrors.

### `config/settings.py` / `config/defaults.yaml`
Pydantic-based settings, one sub-model per concern (TLS, relay, auth, database, agent, log), each reading from `NEXUS_<SECTION>_<KEY>` environment variables. `defaults.yaml` provides baseline values that `load_yaml_defaults()` can pre-populate into `os.environ` before the settings singleton is constructed, so YAML and env vars compose rather than conflict (env vars always win).

### `utils/crypto.py`
`ECDHKeyExchange` (SECP256R1, DER-encoded public keys, HKDF-SHA256 key derivation) and `AESGCMCipher` (256-bit key, random 12-byte nonce per message, AAD support). Also password hashing/verification used by `core/auth.py`. This is the one file where a subtle encoding mismatch (DER vs. the older commented-out X9.62 format still visible at the top of the file) would silently break every session handshake — if you ever touch this file, make sure `agents/base_agent.py` and `core/session.py` are still deriving compatible keys afterward.

### `utils/logger.py`
Structured logging via `structlog`, rendered through stdlib `logging` handlers (not `structlog`'s standalone printer — see the module docstring for why that distinction matters for file-based logging to actually work). `AuditLogger` is a separate, always-JSON audit trail for security-relevant events (login success/failure, session open/close, permission denials, file transfers, commands executed) that writes to its own rotating file independent of the general app log.

### `utils/heartbeat.py`
`HeartbeatManager` — tracks per-connection liveness via pong timestamps, declares a connection dead after missing `max_misses` consecutive heartbeats. `RateLimiter` — a working token-bucket implementation that **is not currently called anywhere** in `relay.py` or `dashboard.py` (see §6, Known Gaps).

### `transport/*.py`
Generic, reusable transport primitives that exist independent of the relay's specific `websockets`-based implementation:
- `websocket_transport.py` — `WSServer`/`WSClient` wrappers with auto-reconnect and a `ConnectionPool` for broadcast scenarios.
- `tcp_transport.py` — raw length-prefixed TCP framing, for a fallback transport if WebSockets aren't available.
- `tls_context.py` — `TLSContextFactory` builds server/client/mTLS `ssl.SSLContext` objects consistently, used by both the relay and any transport that needs TLS.
- `tunnel.py` — `ReverseTunnel` (agent-initiated outbound tunnel, for NAT traversal without port-forwarding) and `SSHTunnel` (paramiko-based SSH port-forward, for environments where an SSH daemon is already reachable).

### `ui/dashboard.py`
Pure REST API (see §2 for why it lost its WebSocket route): `/auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/register` (admin-only), `/devices`, `/devices/{id}`, `/devices/search/{query}`, `/sessions` (read-only listing), `/health`, and `/console` (serves `dashboard.html`).

### `ui/dashboard.html`
The operator console. Single-file HTML/CSS/JS — no build step. Connects one persistent WebSocket directly to the relay (`RELAY_WS_URL`, configured separately from the REST `API_BASE` on the login screen, since they're genuinely different servers/ports), authenticates once as a controller, and multiplexes every device session that operator opens over that same connection. Renders screen frames by decoding the hex-encoded JPEG payload into a `Blob` and drawing it to a `<canvas>`, forwards full mouse/keyboard/scroll capture from that canvas, and implements the terminal, dual-pane file manager, and clipboard sync UI against the message types in §3.3.

### `connect_remote.py`
Runs on the machine being controlled. Auto-discovers `SEND_TO_REMOTE_PC.txt` (searching common locations — Desktop, Downloads, drive roots on Windows), parses relay URL(s) and the agent token out of it, tests each candidate endpoint with a bounded retry loop, and launches `agents.desktop_agent.DesktopAgent` against whichever one responds. Falls back to a harmless no-op loop with a clear warning if `agents.desktop_agent` can't be imported (e.g. a build that didn't bundle it), rather than crashing.

### `nexus_agent_entry.py`
The actual entry point compiled into `nexus_agent.exe` by `build_standalone.py`. If `--relay`/`--token` aren't passed as arguments, prompts for them interactively — this is what makes the "helper sends you an .exe, you double-click it, paste two values" support flow work without the recipient needing a terminal or any command-line arguments.

### `build_standalone.py`
PyInstaller build script that produces `nexus_agent.exe` from `nexus_agent_entry.py`, bundling `agents/`, `utils/`, and `config/` as data/hidden imports.

### `setup_and_launch.py`
Run on the host machine (whoever's providing the remote support). Discovers LAN and public IP (with an explicit, loud warning — not a silent fallback — if public IP discovery fails and degrades to the LAN IP; see the `NEXUS_PUBLIC_IP` override if your host has no direct internet egress), generates a self-signed TLS cert if one doesn't exist, best-effort attempts UPnP port mapping and a public tunnel, launches the relay and dashboard as two `asyncio` tasks in one process (this is *why* they can share one `session_manager` singleton — see §2), and writes/prints `SEND_TO_REMOTE_PC.txt` with the connect command(s) for whoever you're helping.

---

## 5. TLS & certificates

The relay defaults to a self-signed certificate generated by `setup_and_launch.py`. This is fine for a LAN or small trusted team, with one caveat every new browser will hit once: **a browser will silently refuse a `wss://` WebSocket connection to a certificate it doesn't trust, with no visible prompt** (unlike a normal page load, which shows a clickable warning). The fix is to manually visit `https://<relay-host>:<port>/` in a normal tab first, accept the certificate warning there, and only then load the dashboard — that one-time action registers a trust exception for that host:port that the WebSocket connection can then use.

For anything beyond a small trusted team, replace the self-signed cert with either a real CA-signed certificate (e.g. Let's Encrypt, if the relay has a resolvable domain) or your own internal CA whose root you install once into every operator's browser/OS trust store — either removes the per-browser manual trust step entirely.

---

## 6. Known gaps (be aware of these, don't be surprised by them)

- **`RateLimiter` (`utils/heartbeat.py`) is implemented but not invoked.** `defaults.yaml` even has rate-limit config values (`rate_limits.login`, `.api_default`, `.ws_messages`) waiting to be used. Nothing in `relay.py` or `dashboard.py` calls `RateLimiter.is_allowed()` yet.
- **Single relay process, in-memory state.** `device_registry` and `session_manager` are process-local singletons (see `setup_and_launch.py` — relay and dashboard run as two tasks in *one* process specifically so they can share these). No multi-instance/HA deployment story exists yet.
- **MFA silently no-ops without `pyotp`.** `AuthService._verify_totp` logs a warning and returns `True` if `pyotp` isn't installed — meaning MFA can appear "enabled" in config while not actually being enforced. Install `pyotp` if you're relying on `mfa_enabled: true`.
- **UPnP and public-tunnel setup in `setup_and_launch.py` are best-effort.** Not every router supports UPnP, and the tunnel step depends on external tooling being available. The script degrades to LAN-only and says so explicitly rather than pretending WAN access exists when it doesn't — but it's still worth confirming which mode you actually ended up in before relying on it for someone outside your network.
- **`transport/tcp_transport.py` is not on the default connection path.** It exists as an alternative/fallback transport but the relay and agents currently always use the WebSocket path.
