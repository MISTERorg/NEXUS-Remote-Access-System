# NEXUS Remote Access

Self-hosted remote desktop, terminal, and file-transfer platform. One relay server brokers encrypted sessions between operators (a browser dashboard) and agents (a small Python process running on the machine you want to control) — no port-forwarding required on the agent side, and no third-party cloud service sits in the middle of your traffic.

> **Status: v1.** This is the first working version. It's had real debugging mileage (see [Known Limitations](#known-limitations-v1) below) but hasn't been used at scale or audited by anyone outside the team that built it. Read that section before deciding what you trust it with.

---
## DEMO

### LOGIN
- ![img.png](img.png)
- if your credentials are not correct you wont have access
- ![img_1.png](img_1.png)
### DASHBOARD
![img_2.png](img_2.png)
## What it does

- **Remote desktop** — live screen streaming (JPEG over WebSocket) with full mouse and keyboard input forwarding.
![img_3.png](img_3.png)

- for presentation purposes, the demo was made on one node

![img_4.png](img_4.png)

- **Remote terminal** — a real shell (PowerShell on Windows, `$SHELL` elsewhere) attached per session, streamed both ways.
- **File browser & transfer** — list remote directories; upload/download between operator and agent.
![img_5.png](img_5.png)
you get access to the target whole file system

- **Clipboard sync** — pull or push clipboard contents between operator and agent.
- **Multi-device fleet** — a registry tracks every agent that's ever connected, online/offline/busy status, and per-device capabilities.
- **Role-based access** — JWT-authenticated operators with `admin` / `operator` / `viewer` roles; agents authenticate separately with per-device tokens.
- **End-to-end session encryption** — each session gets its own ECDH-derived AES-256-GCM key between the relay and the agent, independent of the outer TLS transport.
- **NAT traversal by design** — the agent always makes an *outbound* connection to the relay, so a machine behind a home router or corporate NAT can still be reached without forwarding a single port on that end.

## Architecture at a glance

```
┌─────────────┐        wss://          ┌──────────────┐        wss://          ┌─────────────┐
│  Browser     │ ─────────────────────▶│  Relay        │◀───────────────────── │  Agent       │
│  Dashboard   │  (operator, one       │  (owns every  │   (outbound only —    │  (runs on    │
│  (dashboard  │   persistent WS       │   session's   │    agent never        │   target     │
│   .html)     │   connection)         │   lifecycle)  │    listens)           │   machine)   │
└─────────────┘                        └──────┬───────┘                        └─────────────┘
                                               │
                                        ┌──────┴───────┐
                                        │  Dashboard    │   REST only: login, device
                                        │  API          │   listing, session listing,
                                        │  (dashboard   │   health. No session traffic
                                        │   .py)        │   passes through here.
                                        └──────────────┘
```

The relay is the only thing either side ever has to reach: the agent dials out to it, and the operator's browser connects straight to it too. The dashboard's REST API is a separate, much smaller surface used only for login and read-only listings — it never sees screen frames, keystrokes, or file contents.

Full component-by-component breakdown, wire protocol, and file-by-file reference: see **[ARCHITECTURE.md](./ARCHITECTURE.md)**.

## Quick start

**On the machine that will host the relay + dashboard** (the "controller" side):

```bash
python setup_and_launch.py
```

This will, in order: discover your LAN and public IP, generate a self-signed TLS certificate if one doesn't exist, attempt UPnP port mapping and a public tunnel (best-effort — safe to ignore if either isn't available on your network), start the relay and the dashboard API, and print a `SEND_TO_REMOTE_PC.txt` file with everything needed to connect an agent.

Then open the dashboard:

```
https://localhost:8080/console
```

Default login is `admin` / `admin123` — **change this immediately**, it's a seeded placeholder, not a production credential.

**On the machine you want to control** (the "agent" side), send that person `SEND_TO_REMOTE_PC.txt` and have them run the one-line command it contains, or:

```bash
python connect_remote.py --relay wss://<relay-address>:7000 --token <agent-token>
```

**First connection from a new browser?** If the relay is using a self-signed certificate (the default), your browser will refuse the WebSocket silently. Visit `https://<relay-address>:7000/` directly first, accept the certificate warning there, then reload the dashboard. This is a one-time step per browser per relay address — see [ARCHITECTURE.md § TLS](./ARCHITECTURE.md#tls--certificates) for the permanent fix (a real or internal-CA-signed cert).

## Project structure

```
agents/       — BaseAgent + DesktopAgent/ServerAgent/IoTAgent/MobileAgent
core/         — relay, session, auth, device registry (the actual server logic)
config/       — settings.py + defaults.yaml
utils/        — crypto, logging, heartbeat, rate limiting
transport/    — TCP/TLS/tunnel/WebSocket transport primitives
ui/           — dashboard.py (REST API) + dashboard.html (operator console)
connect_remote.py, nexus_agent_entry.py, build_standalone.py, setup_and_launch.py
              — agent launcher, PyInstaller entry point/build, host setup script
```

See **[ARCHITECTURE.md](./ARCHITECTURE.md)** for what every single file does and how they connect.

## Known limitations (v1)

Being direct about this so nobody finds out the hard way:

- **Single-process, in-memory state.** `device_registry` and `session_manager` are process-local singletons. One relay process, one source of truth — no horizontal scaling, no shared state across multiple relay instances, and a process restart forgets every device and session.
- **Rate limiting exists but isn't wired up.** `utils/heartbeat.py` has a working `RateLimiter`, and `defaults.yaml` has rate-limit config values, but nothing in `relay.py` or `dashboard.py` actually calls it yet. Don't rely on it being enforced.
- **Self-signed certs by default.** Fine for a LAN or a small trusted team; every new browser has to manually accept the cert once (see Quick Start). For anything wider, get a real certificate or stand up an internal CA.
- **No horizontal/HA story.** This is built for one relay serving one team, not a multi-tenant SaaS deployment.
- **UPnP/public-tunnel auto-setup is best-effort.** If your router doesn't support UPnP or the tunnel binary isn't available, `setup_and_launch.py` falls back to LAN-only and tells you so — it won't silently pretend WAN access works when it doesn't (this was a real bug in an earlier build; it's fixed, but worth knowing the fallback exists).
- **First real version.** Treat it as exactly that. Test in your own environment before pointing it at anything you can't afford to lose access to.

## Contributing

Standard flow: fork, branch, PR. If you're touching `core/relay.py` or `core/session.py`, read the wire protocol section of `ARCHITECTURE.md` first — the session lifecycle is entirely relay-owned by design, and it's easy to reintroduce a split-brain between the relay and the dashboard API if you're not careful about which one is supposed to own what.

## License

### Any product or software which was build or designed from part or all the code base of NEXUS should reference it's creator DAUDET IKEORAH ELAD ANEDO
