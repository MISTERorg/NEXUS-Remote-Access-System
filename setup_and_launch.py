"""
setup_and_launch.py
--------------------
NEXUS One-Click Setup & Launcher.

Fixes vs previous version:
  - Runs relay + dashboard in the SAME process (no separate windows that
    silently fail due to missing imports or wrong working directory)
  - Installs ALL missing pip packages automatically before starting
  - Verifies each service is actually listening before opening browser
  - Forces NEXUS_DEBUG=true so /docs page is always available
  - Handles the relay WS server and FastAPI app in parallel async tasks
  - No dependency on uvicorn being on PATH — imports it directly

Run from the nexus-ras root directory:
    python setup_and_launch.py

Options:
    --launch-only   Skip setup, use existing .env
    --reset         Regenerate .env and certs from scratch
    --no-browser    Don't open any browser window
    --no-dashboard  Don't open the operator console (dashboard.html)
    --no-docs       Don't open the FastAPI /docs page
    --yes, -y       Skip the "what should launch" prompt, open console + docs
    --port-api N    Dashboard port (default 8080)
    --port-relay N  Relay port (default 7000)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# ── paths ────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).resolve().parent
ENV_FILE   = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / ".nexus_state.json"
CERTS_DIR  = BASE_DIR / "certs"

# Operator console (dashboard.html). Checked in a few likely spots so this
# works whether you dropped it in the project root or under ui/.
DASHBOARD_HTML_CANDIDATES = [
    BASE_DIR / "dashboard.html",
    BASE_DIR / "ui" / "dashboard.html",
    BASE_DIR / "ui" / "console.html",
]


def find_dashboard_html() -> Path | None:
    for candidate in DASHBOARD_HTML_CANDIDATES:
        if candidate.exists():
            return candidate
    return None

# Make sure Python can find our modules
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ── colours ──────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")   # enable ANSI on Windows

def _c(code, text): return f"\033[{code}m{text}\033[0m"
def ok(m):   print(_c("92", f"  ✓  {m}"))
def info(m): print(_c("96", f"  →  {m}"))
def warn(m): print(_c("93", f"  ⚠  {m}"))
def err(m):  print(_c("91", f"  ✗  {m}")); 
def hdr(m):  print(_c("1;96", f"\n{'─'*54}\n  {m}\n{'─'*54}"))
def dim(m):  print(_c("2",  f"     {m}"))


# ════════════════════════════════════════════════════════════════════════════
# 1. Package installer — runs BEFORE any project imports
# ════════════════════════════════════════════════════════════════════════════

REQUIRED_PACKAGES = {
    "fastapi":           "fastapi",
    "uvicorn":           "uvicorn[standard]",
    "websockets":        "websockets",
    "cryptography":      "cryptography",
    "jwt":               "PyJWT",
    "bcrypt":            "bcrypt",
    "pydantic":          "pydantic",
    "pydantic_settings": "pydantic-settings",
    "httpx":             "httpx",
    "psutil":            "psutil",
    "mss":               "mss",
    "PIL":               "Pillow",
    "pynput":            "pynput",
    "click":             "click",
    "rich":              "rich",
    "aiofiles":          "aiofiles",
    "aiosqlite":         "aiosqlite",
    "sqlalchemy":        "sqlalchemy",
    "structlog":         "structlog",
}

def install_missing_packages() -> None:
    hdr("Checking Dependencies")
    missing_installs = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            missing_installs.append(pip_name)

    if not missing_installs:
        ok("All packages already installed")
        return

    warn(f"Missing: {', '.join(missing_installs)}")
    info("Installing now — this may take a minute...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + missing_installs,
        capture_output=False
    )
    if result.returncode == 0:
        ok("All packages installed successfully")
    else:
        err("Some packages failed to install — check output above")
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# 2. IP detection
# ════════════════════════════════════════════════════════════════════════════

def get_public_ip() -> str:
    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com",
    ]

    # Try curl (available on Win10+ and most Linux/Mac)
    for url in services:
        try:
            r = subprocess.run(
                ["curl", "-s", "--max-time", "5", url],
                capture_output=True, text=True
            )
            ip = r.stdout.strip()
            if _valid_ip(ip):
                return ip
        except FileNotFoundError:
            break

    # Try urllib
    try:
        import urllib.request
        for url in services:
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    ip = resp.read().decode().strip()
                    if _valid_ip(ip):
                        return ip
            except Exception:
                continue
    except Exception:
        pass

    # Fall back to LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        warn(f"Could not reach internet IP services — using LAN IP: {ip}")
        return ip
    except Exception:
        return "127.0.0.1"


def _valid_ip(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


# ════════════════════════════════════════════════════════════════════════════
# 3. Setup: secrets, .env, certs
# ════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if sys.platform == "win32":
        try:
            subprocess.run(["attrib", "+H", str(STATE_FILE)], capture_output=True)
        except Exception:
            pass


def write_env(public_ip: str, agent_token: str, jwt_secret: str,
              relay_port: int, api_port: int) -> None:
    ENV_FILE.write_text(f"""# NEXUS Remote Access — auto-generated by setup_and_launch.py

NEXUS_ENVIRONMENT=development
NEXUS_DEBUG=true
NEXUS_APP_NAME=NEXUS Remote Access

NEXUS_AUTH_SECRET_KEY={jwt_secret}
NEXUS_AUTH_MFA_ENABLED=false
NEXUS_AUTH_MAX_LOGIN_ATTEMPTS=10
NEXUS_AUTH_ALLOWED_ORIGINS=*

NEXUS_RELAY_HOST=0.0.0.0
NEXUS_RELAY_PORT={relay_port}
NEXUS_RELAY_HEARTBEAT_INTERVAL=30
NEXUS_RELAY_SESSION_TIMEOUT=7200

NEXUS_TLS_REQUIRE_CLIENT_CERT=false

NEXUS_AGENT_SCREEN_FPS=15
NEXUS_AGENT_SCREEN_QUALITY=75
NEXUS_AGENT_RECONNECT_MAX_RETRIES=20

NEXUS_LOG_LEVEL=INFO
NEXUS_LOG_FORMAT=console

# Reference info
# Public IP   : {public_ip}
# Agent token : {agent_token}
# Relay port  : {relay_port}
# API port    : {api_port}
""", encoding="utf-8")


def generate_certs() -> None:
    cert_file = CERTS_DIR / "nexus-relay-server.crt"
    if cert_file.exists():
        ok("Certificates already exist — skipping")
        return
    info("Generating TLS certificates...")
    result = subprocess.run(
        [sys.executable, "-m", "utils.crypto",
         "--generate-certs", "--out", str(CERTS_DIR)],
        capture_output=True, text=True, cwd=str(BASE_DIR)
    )
    if result.returncode == 0:
        ok("Certificates generated")
    else:
        warn("Certificate generation failed — continuing without TLS (OK for LAN)")
        dim(result.stderr[:300])


def ensure_dirs() -> None:
    for d in ["certs", "data", "logs", "recordings"]:
        (BASE_DIR / d).mkdir(parents=True, exist_ok=True)


def run_setup(relay_port: int, api_port: int, force_reset: bool) -> dict:
    """Full first-time setup. Returns state dict with ip + token."""
    state = load_state()

    if ENV_FILE.exists() and not force_reset and state.get("agent_token"):
        hdr("Existing Config Detected")
        ok(f"Using saved config  (run --reset to regenerate)")
        ok(f"Public IP   : {state.get('public_ip','?')}")
        ok(f"Agent token : {state['agent_token'][:20]}…")
        return state

    if force_reset and ENV_FILE.exists():
        ENV_FILE.unlink()
        info("Cleared existing .env")

    hdr("Step 1 — Detecting Your Public IP")
    public_ip = get_public_ip()
    ok(f"Public IP: {public_ip}")

    hdr("Step 2 — Generating Secrets")
    agent_token = secrets.token_urlsafe(32)
    jwt_secret  = secrets.token_urlsafe(48)
    ok(f"Agent token : {agent_token[:20]}…")
    ok(f"JWT secret  : {jwt_secret[:16]}…")

    hdr("Step 3 — Writing .env")
    write_env(public_ip, agent_token, jwt_secret, relay_port, api_port)
    ok(f".env written → {ENV_FILE}")

    state = {"public_ip": public_ip, "agent_token": agent_token,
             "jwt_secret": jwt_secret, "relay_port": relay_port,
             "api_port": api_port}
    save_state(state)
    ok("State saved to .nexus_state.json")

    hdr("Step 4 — TLS Certificates")
    generate_certs()

    return state


# ════════════════════════════════════════════════════════════════════════════
# 4. Port checker
# ════════════════════════════════════════════════════════════════════════════

def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port: int, timeout: int = 30, label: str = "") -> bool:
    info(f"Waiting for {label or f'port {port}'} to be ready...")
    for i in range(timeout):
        if port_in_use(port):
            ok(f"{label or f'Port {port}'} is up ✓")
            return True
        time.sleep(1)
        if i % 5 == 4:
            dim(f"  still waiting… ({i+1}s)")
    err(f"{label} did not start within {timeout}s")
    return False


# ════════════════════════════════════════════════════════════════════════════
# 5. Launch relay + dashboard as subprocesses with visible output
# ════════════════════════════════════════════════════════════════════════════

def launch_services(relay_port: int, api_port: int) -> tuple:
    """
    Launch relay and dashboard as subprocesses.
    Output goes to log files AND to new CMD windows on Windows.
    Returns (relay_proc, dashboard_proc).
    """
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)

    relay_log     = open(logs_dir / "relay.log", "w")
    dashboard_log = open(logs_dir / "dashboard.log", "w")

    env = {**os.environ, "PYTHONPATH": str(BASE_DIR), "PYTHONUNBUFFERED": "1"}

    hdr("Step 5 — Starting Relay Server")

    if port_in_use(relay_port):
        warn(f"Port {relay_port} already in use — relay may already be running")
        relay_proc = None
    else:
        relay_cmd = [
            sys.executable, "-m", "core.relay",
            "--host", "0.0.0.0",
            "--port", str(relay_port),
        ]
        relay_proc = subprocess.Popen(
            relay_cmd,
            cwd=str(BASE_DIR),
            env=env,
            stdout=relay_log,
            stderr=subprocess.STDOUT,
        )
        dim(f"Relay PID: {relay_proc.pid}  |  Log: logs/relay.log")

        if not wait_for_port(relay_port, timeout=15, label="Relay Server"):
            err("Relay failed to start. Check logs/relay.log for errors.")
            relay_log.close()
            _print_log_tail(logs_dir / "relay.log")
            sys.exit(1)

    hdr("Step 6 — Starting Dashboard API")

    if port_in_use(api_port):
        warn(f"Port {api_port} already in use — dashboard may already be running")
        dashboard_proc = None
    else:
        dashboard_cmd = [
            sys.executable, "-m", "uvicorn",
            "ui.dashboard:app",
            "--host", "0.0.0.0",
            "--port", str(api_port),
            "--log-level", "info",
        ]
        dashboard_proc = subprocess.Popen(
            dashboard_cmd,
            cwd=str(BASE_DIR),
            env=env,
            stdout=dashboard_log,
            stderr=subprocess.STDOUT,
        )
        dim(f"Dashboard PID: {dashboard_proc.pid}  |  Log: logs/dashboard.log")

        if not wait_for_port(api_port, timeout=20, label="Dashboard API"):
            err("Dashboard failed to start. Check logs/dashboard.log for errors.")
            dashboard_log.close()
            _print_log_tail(logs_dir / "dashboard.log")
            sys.exit(1)

    return relay_proc, dashboard_proc


def _print_log_tail(log_path: Path, lines: int = 30) -> None:
    """Print last N lines of a log file for quick diagnosis."""
    if not log_path.exists():
        return
    content = log_path.read_text(errors="replace").strip().splitlines()
    print(_c("93", f"\n  ── Last {lines} lines of {log_path.name} ──"))
    for line in content[-lines:]:
        print(_c("2", f"  {line}"))
    print()


# ════════════════════════════════════════════════════════════════════════════
# 6. Print agent command
# ════════════════════════════════════════════════════════════════════════════

def print_agent_command(public_ip: str, agent_token: str,
                        relay_port: int, api_port: int,
                        dashboard_path: Path | None = None) -> None:
    one_liner = (
        f"python connect_remote.py "
        f"--relay ws://{public_ip}:{relay_port} "
        f"--token {agent_token}"
    )

    hdr("HOW TO CONNECT THE REMOTE PC")
    print(f"""
{_c('1', '  The remote person needs to do ONLY 2 things:')}

  {_c('1;93', 'STEP 1')} — Install Python (only needed once):
  {_c('96',   '  https://www.python.org/downloads/')}
  {_c('2',    '  (Tick "Add Python to PATH" during install)')}

  {_c('1;93', 'STEP 2')} — Send them the nexus-ras-v2 folder, then ask them
            to open Command Prompt inside it and run:

  {_c('1;92', one_liner)}

  {_c('2', "That's it. Everything else (packages, config, connection)")}
  {_c('2', 'is handled automatically by connect_remote.py.')}
""")

    dashboard_line = (
        f"http://localhost:{api_port}/console" if dashboard_path
        else "not found — see warning above"
    )

    print(_c("1;96", "  ╔══════════════════════════════════════════════════╗"))
    print(_c("1;96", f"  ║  Relay URL   :  ws://{public_ip}:{relay_port}"))
    print(_c("1;93", f"  ║  Agent Token :  {agent_token}"))
    print(_c("1;92", f"  ║  Dashboard   :  {dashboard_line}"))
    print(_c("2",    f"  ║  API docs    :  http://localhost:{api_port}/docs"))
    print(_c("2",    f"  ║  API Login   :  admin / admin123"))
    print(_c("1;96", "  ╚══════════════════════════════════════════════════╝"))
    print()

    # Write the one-liner to a text file so it's easy to copy/paste/send
    snippet_file = BASE_DIR / "SEND_TO_REMOTE_PC.txt"
    snippet_file.write_text(
        f"NEXUS Remote Support — Instructions for the Remote PC\n"
        f"{'='*54}\n\n"
        f"STEP 1: Install Python from https://www.python.org/downloads/\n"
        f"        Tick 'Add Python to PATH' during install.\n\n"
        f"STEP 2: Open Command Prompt inside the nexus-ras-v2 folder and run:\n\n"
        f"  {one_liner}\n\n"
        f"That's it — everything else is automatic.\n\n"
        f"{'='*54}\n"
        f"Relay URL   : ws://{public_ip}:{relay_port}\n"
        f"Agent Token : {agent_token}\n",
        encoding="utf-8",
    )
    ok(f"Instructions also saved → SEND_TO_REMOTE_PC.txt  (easy to email/share)")


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS One-Click Setup & Launcher")
    parser.add_argument("--launch-only", action="store_true",
                        help="Skip setup, use existing .env")
    parser.add_argument("--reset", action="store_true",
                        help="Force regenerate .env and certs")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't open browser automatically")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Don't open the operator console (dashboard.html) on launch")
    parser.add_argument("--no-docs", action="store_true",
                        help="Don't open the FastAPI /docs page on launch")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the launch-mode prompt and open both console + docs")
    parser.add_argument("--port-api",   type=int, default=8080)
    parser.add_argument("--port-relay", type=int, default=7000)
    args = parser.parse_args()

    RELAY_PORT = args.port_relay
    API_PORT   = args.port_api

    print(_c("1;96", """
  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
  ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
  ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
  ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
  ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
  ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
  Remote Access System — One-Click Launcher
"""))

    ensure_dirs()

    # Step 0: install packages FIRST before any project imports
    install_missing_packages()

    # Step 1-4: setup or load existing config
    if args.launch_only and ENV_FILE.exists():
        state = load_state()
        if not state:
            warn("No saved state found — running full setup")
            state = run_setup(RELAY_PORT, API_PORT, force_reset=False)
    else:
        state = run_setup(RELAY_PORT, API_PORT, force_reset=args.reset)

    public_ip   = state.get("public_ip",   "127.0.0.1")
    agent_token = state.get("agent_token", "")
    RELAY_PORT  = int(state.get("relay_port", RELAY_PORT))
    API_PORT    = int(state.get("api_port",   API_PORT))

    # Step 5-6: launch relay + dashboard
    relay_proc, dashboard_proc = launch_services(RELAY_PORT, API_PORT)

    # Step 7: open browser — operator console and/or FastAPI /docs
    docs_url = f"http://localhost:{API_PORT}/docs"
    health_url = f"http://localhost:{API_PORT}/health"
    dashboard_path = find_dashboard_html()

    open_console = not args.no_dashboard
    open_docs = not args.no_docs

    if not args.no_browser:
        hdr("Step 7 — Opening Browser")

        # Ask which to open, unless the caller already decided via flags.
        if not args.yes and not args.no_dashboard and not args.no_docs:
            print(f"""
  {_c('1', 'What should launch?')}

  {_c('1;93', '[1]')} Operator console (dashboard.html)  {_c('2', '— recommended')}
  {_c('1;93', '[2]')} FastAPI /docs page (raw API reference)
  {_c('1;93', '[3]')} Both
  {_c('1;93', '[4]')} Neither — I'll open things myself
""")
            choice = input(_c("96", "  Choose [1/2/3/4, default 1]: ")).strip() or "1"
            open_console = choice in ("1", "3")
            open_docs = choice in ("2", "3")

        if open_console:
            console_url = f"http://localhost:{API_PORT}/console"
            if dashboard_path:
                webbrowser.open(console_url)
                ok(f"Operator console opened → {console_url}")
            else:
                warn("dashboard.html not found — checked: " +
                     ", ".join(str(p) for p in DASHBOARD_HTML_CANDIDATES))
                dim("Save the console file to one of those paths, then reload the page.")

        if open_docs:
            webbrowser.open(docs_url)
            ok(f"Browser opened → {docs_url}")

    # Step 8: print remote agent command
    print_agent_command(public_ip, agent_token, RELAY_PORT, API_PORT, dashboard_path)

    ok("All services running. Press Ctrl+C to stop everything.\n")
    dim(f"  Relay log     → {BASE_DIR / 'logs' / 'relay.log'}")
    dim(f"  Dashboard log → {BASE_DIR / 'logs' / 'dashboard.log'}")
    dim(f"  Health check  → {health_url}")
    if dashboard_path:
        dim(f"  Console       → http://localhost:{API_PORT}/console")
    else:
        dim(f"  Console       → not found (place dashboard.html in project root or ui/)")
    print()

    # Keep the script alive — show live log tail & handle Ctrl+C
    relay_log_path     = BASE_DIR / "logs" / "relay.log"
    dashboard_log_path = BASE_DIR / "logs" / "dashboard.log"

    try:
        while True:
            time.sleep(5)
            # Check processes haven't died
            if relay_proc and relay_proc.poll() is not None:
                err("Relay process died unexpectedly!")
                _print_log_tail(relay_log_path)
                break
            if dashboard_proc and dashboard_proc.poll() is not None:
                err("Dashboard process died unexpectedly!")
                _print_log_tail(dashboard_log_path)
                break
    except KeyboardInterrupt:
        print(_c("93", "\n\n  Shutting down NEXUS services..."))
        for proc in [relay_proc, dashboard_proc]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        ok("All services stopped. Goodbye.")


if __name__ == "__main__":
    main()
