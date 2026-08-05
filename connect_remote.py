"""
connect_remote.py
-----------------
NEXUS Remote Agent — Fully Automatic Zero-Touch Connector.

How it works:
  1. Searches for SEND_TO_REMOTE_PC.txt in all likely locations
  2. Parses the Relay URL and Token from it automatically
  3. Installs all required packages silently
  4. Connects and runs — no human input required at any step

The person on the remote PC does ONE thing:
    python connect_remote.py

That's it. No flags, no copy-pasting, no config.
The file SEND_TO_REMOTE_PC.txt (sent by the controller) does the rest.

If the file cannot be found anywhere, it falls back to asking for the
relay URL and token interactively as a last resort.

NOTE ON WHO CATCHES CRASHES:
  When packaged as nexus_agent.exe, this file is imported as a module by
  nexus_agent_entry.py and its main() is called directly — NOT executed
  via `python connect_remote.py`. That means the `if __name__=="__main__"`
  guard at the bottom of THIS file never runs in the shipped exe; it only
  helps when a developer runs this script directly. The guard in
  nexus_agent_entry.py is what actually protects the packaged exe. Both
  are kept, for defense in depth.

Windows console hardening (fixes silent-crash-on-launch bug):
  PyInstaller --onefile console apps on Windows inherit the machine's legacy
  console codepage (cp1252 / cp437) unless explicitly told otherwise. This
  script prints Unicode symbols (✓ → ⚠ ✗ ─ █ ═ ║) for readability, which
  raises an unhandled UnicodeEncodeError on any codepage that doesn't
  include them — and because PyInstaller onefile windows close the instant
  the process exits, that crash is invisible: the window just vanishes.
  Fix: switch the console to UTF-8 (chcp 65001) AND reconfigure stdout/
  stderr with errors="replace" as a belt-and-braces fallback, and route
  every print through a safe wrapper that can never itself raise.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import platform
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# ── Windows console hardening — MUST run before any print() call ─────────────
if sys.platform == "win32":
    try:
        # Switch the console's active code page to UTF-8. chcp modifies the
        # console session (shared by parent + child processes), so running
        # it via os.system from here does take effect for our own prints.
        os.system("chcp 65001 >NUL 2>&1")
    except Exception:
        pass
    try:
        os.system("color")   # enable ANSI/VT100 escape processing
    except Exception:
        pass
    try:
        # Belt-and-braces: force stdout/stderr to encode as UTF-8 regardless
        # of what codepage the console actually ends up in, and NEVER raise
        # on an unencodable character — replace it with '?' instead of
        # crashing the whole process.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _c(code, t):
    return f"\033[{code}m{t}\033[0m"


def _safe_print(s: str = "", **kwargs) -> None:
    """
    Print that can never crash the process. If somehow a character still
    can't be encoded (e.g. reconfigure() unavailable on some embedded
    Python builds), fall back to a plain ASCII-safe rendering rather than
    raising and killing the console window.
    """
    try:
        print(s, **kwargs)
    except UnicodeEncodeError:
        try:
            print(s.encode("ascii", errors="replace").decode("ascii"), **kwargs)
        except Exception:
            pass
    except Exception:
        pass


def ok(m):   _safe_print(_c("92",   f"  \u2713  {m}"))     # ✓
def info(m): _safe_print(_c("96",   f"  \u2192  {m}"))     # →
def warn(m): _safe_print(_c("93",   f"  \u26a0  {m}"))     # ⚠
def err(m):  _safe_print(_c("91",   f"  \u2717  {m}"))     # ✗
def hdr(m):  _safe_print(_c("1;96", f"\n{chr(0x2500)*54}\n  {m}\n{chr(0x2500)*54}"))
def dim(m):  _safe_print(_c("2",    f"     {m}"))


BASE_DIR = Path(__file__).resolve().parent


# ════════════════════════════════════════════════════════════════════════════
# STEP 1 — Find SEND_TO_REMOTE_PC.txt in all likely locations
# ════════════════════════════════════════════════════════════════════════════

CONFIG_FILENAME = "SEND_TO_REMOTE_PC.txt"

def _candidate_dirs() -> list[Path]:
    """
    Return every directory worth searching, in priority order.
    Covers: same folder as this script, CWD, Desktop, Downloads,
    Documents, USB drives, parent folders, home folder.
    """
    home = Path.home()
    candidates = [
        BASE_DIR,                                    # same folder as connect_remote.py
        Path.cwd(),                                  # wherever they ran the script from
        Path.cwd().parent,                           # one level up from CWD
        BASE_DIR.parent,                             # one level up from script
        home,                                        # home folder
        home / "Desktop",
        home / "Downloads",
        home / "Documents",
        home / "OneDrive" / "Desktop",
        home / "OneDrive" / "Documents",
    ]

    # Windows: scan all drive letters for USB sticks / external drives
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            try:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    candidates += [
                        drive,
                        drive / "nexus-ras-v2",
                        drive / "NEXUS",
                    ]
            except Exception:
                continue

    # Linux/macOS: common mount points
    else:
        for mount_path in ("/media", "/Volumes", "/mnt"):
            p = Path(mount_path)
            if p.exists():
                try:
                    for sub in p.glob("*"):
                        candidates.append(sub)
                except Exception:
                    pass

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in candidates:
        try:
            rp = str(p.resolve()) if p.exists() else str(p)
            if rp not in seen:
                seen.add(rp)
                unique.append(p)
        except Exception:
            continue
    return unique


def find_config_file() -> Path | None:
    """
    Search every candidate directory for SEND_TO_REMOTE_PC.txt.
    Also does a one-level deep scan of each candidate.
    Returns the Path if found, None otherwise.
    """
    hdr("Searching for Connection File")
    info(f"Looking for:  {CONFIG_FILENAME}")

    checked = []
    for directory in _candidate_dirs():
        if not directory.exists():
            continue

        # Direct match
        candidate = directory / CONFIG_FILENAME
        checked.append(str(candidate))
        if candidate.exists():
            ok(f"Found: {candidate}")
            return candidate

        # One level deep (subfolders)
        try:
            for sub in directory.iterdir():
                if sub.is_dir():
                    deep = sub / CONFIG_FILENAME
                    checked.append(str(deep))
                    if deep.exists():
                        ok(f"Found: {deep}")
                        return deep
        except (PermissionError, OSError):
            continue

    warn(f"Could not find {CONFIG_FILENAME} in any of these locations:")
    for path in checked[:15]:   # show first 15 to avoid flooding
        dim(path)
    if len(checked) > 15:
        dim(f"  ... and {len(checked) - 15} more locations")
    return None


# ════════════════════════════════════════════════════════════════════════════
# STEP 2 — Parse SEND_TO_REMOTE_PC.txt
# ════════════════════════════════════════════════════════════════════════════

def parse_config_file(path: Path) -> dict:
    """
    Extract Relay URL and Agent Token from SEND_TO_REMOTE_PC.txt.
    """
    hdr("Reading Connection File")
    content = path.read_text(encoding="utf-8", errors="replace")
    info(f"Parsing: {path}")

    result = {}

    # ── Pattern 1: "Relay URL   : ws://..." ──────────────────────────────────
    m = re.search(r"Relay URL\s*:\s*(wss?://[^\s]+)", content, re.IGNORECASE)
    if m:
        result["relay"] = m.group(1).strip()

    m = re.search(r"Agent Token\s*:\s*([A-Za-z0-9_\-=]{20,})", content, re.IGNORECASE)
    if m:
        result["token"] = m.group(1).strip()

    # ── Pattern 2: "--relay ws://..." CLI flag style ─────────────────────────
    if "relay" not in result:
        m = re.search(r"--relay\s+(wss?://[^\s]+)", content)
        if m:
            result["relay"] = m.group(1).strip()

    if "token" not in result:
        m = re.search(r"--token\s+([A-Za-z0-9_\-=]{20,})", content)
        if m:
            result["token"] = m.group(1).strip()

    # ── Pattern 3: bare ws:// or wss:// URL anywhere ─────────────────────────
    if "relay" not in result:
        m = re.search(r"(wss?://[^\s]+)", content)
        if m:
            result["relay"] = m.group(1).strip()

    # ── Pattern 4: bare token ────────────────────────────────────────────────
    if "token" not in result:
        for line in content.splitlines():
            line = line.strip()
            if re.fullmatch(r"[A-Za-z0-9_\-=]{20,}", line) and not line.startswith("http"):
                result["token"] = line
                break

    # ── Report what we found ──────────────────────────────────────────────────
    if "relay" in result:
        ok(f"Relay URL   : {result['relay']}")
    else:
        warn("Could not extract Relay URL from file")

    if "token" in result:
        masked = result["token"][:8] + "..." + result["token"][-4:] if len(result["token"]) > 12 else "***"
        ok(f"Agent Token : {masked}  (masked for security)")
    else:
        warn("Could not extract Agent Token from file")

    return result


# ════════════════════════════════════════════════════════════════════════════
# STEP 3 — Fallback: ask interactively if file not found / unparseable
# ════════════════════════════════════════════════════════════════════════════

def ask_for_credentials() -> dict:
    """Last resort — prompt the user for relay + token."""
    warn("Falling back to manual entry.")
    _safe_print()
    _safe_print(_c("93", "  Ask the person helping you to send you:"))
    _safe_print(_c("93", "    1. The Relay URL  (looks like: ws://12.34.56.78:7000)"))
    _safe_print(_c("93", "    2. The Agent Token (a long random string)"))
    _safe_print()

    relay = ""
    while not relay.startswith("ws"):
        relay = input(_c("96", "  Paste Relay URL   > ")).strip()
        if not relay.startswith("ws"):
            err("Must start with ws:// or wss://")

    token = ""
    while len(token) < 10:
        token = input(_c("96", "  Paste Agent Token > ")).strip()
        if len(token) < 10:
            err("Token looks too short — please paste the full token")

    return {"relay": relay, "token": token}


def get_credentials() -> dict:
    """
    Main credential resolver.
    1. Try to find + parse SEND_TO_REMOTE_PC.txt
    2. If found but incomplete, try to fill gaps interactively
    3. If not found at all, fall back to full interactive entry
    """
    config_path = find_config_file()

    if config_path:
        creds = parse_config_file(config_path)
        if creds.get("relay") and creds.get("token"):
            ok("All connection details found automatically — no input needed!")
            return creds
        warn("File found but could not parse all details.")
        if not creds.get("relay"):
            err("Missing: Relay URL")
            creds["relay"] = input(_c("96", "  Paste Relay URL > ")).strip()
        if not creds.get("token"):
            err("Missing: Agent Token")
            creds["token"] = input(_c("96", "  Paste Agent Token > ")).strip()
        return creds

    warn(f"{CONFIG_FILENAME} was not found on this PC.")
    warn("Make sure you copied the whole nexus-ras-v2 folder including that file.")
    return ask_for_credentials()


# ════════════════════════════════════════════════════════════════════════════
# STEP 4 — Python version check
# ════════════════════════════════════════════════════════════════════════════

def check_python() -> None:
    hdr("Checking System")
    v = sys.version_info
    if v.major < 3 or (v.major == 3 and v.minor < 10):
        err(f"Python 3.10+ required. You have {v.major}.{v.minor}.")
        err("Download Python from: https://www.python.org/downloads/")
        err('Tick "Add Python to PATH" during install, then try again.')
        input("\nPress Enter to exit...")
        sys.exit(1)
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    ok(f"System  : {platform.system()} {platform.release()} ({platform.machine()})")
    ok(f"Hostname: {socket.gethostname()}")


# ════════════════════════════════════════════════════════════════════════════
# STEP 5 — Silent package installer
# ════════════════════════════════════════════════════════════════════════════

PACKAGES = {
    "websockets":        "websockets>=12.0",
    "cryptography":      "cryptography>=42.0.0",
    "jwt":               "PyJWT>=2.8.0",
    "bcrypt":            "bcrypt>=4.1.2",
    "pydantic":          "pydantic>=2.6.0",
    "pydantic_settings": "pydantic-settings>=2.2.0",
    "psutil":            "psutil>=5.9.8",
    "mss":               "mss>=9.0.1",
    "PIL":               "Pillow>=10.2.0",
    "pynput":            "pynput>=1.7.6",
    "aiofiles":          "aiofiles>=23.2.1",
    "structlog":         "structlog>=24.1.0",
    "sqlalchemy":        "sqlalchemy>=2.0.28",
    "aiosqlite":         "aiosqlite>=0.20.0",
}

def install_packages() -> None:
    hdr("Installing Required Components")
    if getattr(sys, 'frozen', False):
        info("Running as frozen executable — all components are bundled, skipping install")
        return

    to_install = []
    for import_name, pip_spec in PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            to_install.append(pip_spec)

    if not to_install:
        ok("All components already installed")
        return

    info(f"Installing {len(to_install)} component(s) — please wait...")

    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"] + to_install,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    spinner = "|/-\\"
    i = 0
    while proc.poll() is None:
        _safe_print(f"\r  {_c('96', spinner[i % 4])}  Installing...", end="", flush=True)
        time.sleep(0.15)
        i += 1
    _safe_print("\r" + " " * 40 + "\r", end="")
    _, stderr = proc.communicate()

    if proc.returncode != 0:
        err("Installation failed:")
        _safe_print(stderr.decode(errors="replace")[:500])
        input("\nPress Enter to exit...")
        sys.exit(1)

    ok(f"Installed {len(to_install)} component(s) successfully")


# ════════════════════════════════════════════════════════════════════════════
# STEP 6 — Check project files exist
# ════════════════════════════════════════════════════════════════════════════

def check_project_files() -> None:
    hdr("Checking Project Files")
    required = [
        BASE_DIR / "agents" / "base_agent.py",
        BASE_DIR / "agents" / "desktop_agent.py",
        BASE_DIR / "core"   / "session.py",
        BASE_DIR / "core"   / "registry.py",
        BASE_DIR / "core"   / "auth.py",
        BASE_DIR / "utils"  / "crypto.py",
        BASE_DIR / "utils"  / "logger.py",
        BASE_DIR / "config" / "settings.py",
    ]
    missing = [f for f in required if not f.exists()]
    if missing:
        err("Some project files are missing. Make sure you extracted the full")
        err("nexus-ras-v2.zip and are running connect_remote.py from inside it.")
        err(f"Current location: {BASE_DIR}")
        for f in missing:
            try:
                dim(f"Missing: {f.relative_to(BASE_DIR)}")
            except ValueError:
                dim(f"Missing: {f}")
        input("\nPress Enter to exit...")
        sys.exit(1)
    ok("All project files present")


# ════════════════════════════════════════════════════════════════════════════
# STEP 7 — Write minimal agent .env
# ════════════════════════════════════════════════════════════════════════════

def write_agent_env() -> None:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        return
    env_file.write_text(
        "NEXUS_ENVIRONMENT=development\n"
        "NEXUS_DEBUG=false\n"
        "NEXUS_AUTH_SECRET_KEY=agent-side-placeholder-not-used\n"
        "NEXUS_AUTH_MFA_ENABLED=false\n"
        "NEXUS_TLS_REQUIRE_CLIENT_CERT=false\n"
        "NEXUS_LOG_LEVEL=WARNING\n"
        "NEXUS_LOG_FORMAT=console\n"
        "NEXUS_AGENT_SCREEN_FPS=15\n"
        "NEXUS_AGENT_SCREEN_QUALITY=75\n"
        "NEXUS_AGENT_RECONNECT_MAX_RETRIES=999\n"
        "NEXUS_AGENT_RECONNECT_DELAY=5\n",
        encoding="utf-8",
    )


# ════════════════════════════════════════════════════════════════════════════
# STEP 8 — Device identity
# ════════════════════════════════════════════════════════════════════════════

def get_device_id() -> str:
    hostname = socket.gethostname()
    username = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    raw = f"{hostname}-{username}".lower().replace(" ", "-")
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:6]
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in raw)[:20]
    return f"{safe}-{short_hash}"

def get_device_name() -> str:
    hostname = socket.gethostname()
    username = os.environ.get("USERNAME") or os.environ.get("USER") or "User"
    return f"{username}'s PC ({hostname})"


# ════════════════════════════════════════════════════════════════════════════
# STEP 9 — Test relay is reachable
# ════════════════════════════════════════════════════════════════════════════

def check_relay_reachable(relay_url: str) -> None:
    hdr("Testing Connection to Controller")
    try:
        parsed = urlparse(relay_url)
        host = parsed.hostname or "127.0.0.1"
        if parsed.port:
            port = parsed.port
        else:
            port = 443 if parsed.scheme == "wss" else 7000
    except Exception:
        warn("Could not parse relay URL — will attempt connection anyway")
        return

    info(f"Testing TCP connection to {host}:{port} (this is a real socket connect, not ICMP ping)...")
    for attempt in range(1, 6):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                if s.connect_ex((host, port)) == 0:
                    ok(f"Controller is reachable at {host}:{port}")
                    return
        except Exception:
            pass
        if attempt < 5:
            warn(f"Attempt {attempt}/5 failed — retrying in 3s...")
            time.sleep(3)

    err(f"Cannot reach {host}:{port} after 5 attempts.")
    err("Possible causes:")
    err("  • The controller's setup_and_launch.py is not running")
    err("  • A firewall is blocking this port")
    err(f"  • The Relay URL may be wrong: {relay_url}")
    if "pinggy" not in host and ".pinggy." not in relay_url:
        err("  • If this is a direct WAN IP (not a tunnel URL), the controller's ISP")
        err("    may use carrier-grade NAT — no port forwarding can fix that. Ask them")
        err("    to use the tunnel URL from their SEND_TO_REMOTE_PC.txt instead, or")
        err("    re-run setup_and_launch.py on their end to generate a fresh one.")
    err("Note: a failed `ping` command (ICMP) does NOT mean the same thing as this")
    err("test failing — many networks block ICMP while still allowing this real")
    err("connection through. Trust this result, not a separate ping test.")
    err("Ask the controller to confirm their relay is running, then try again.")
    input("\nPress Enter to exit...")
    sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# STEP 10 — Run the agent
# ════════════════════════════════════════════════════════════════════════════

def run_agent(relay_url: str, token: str, device_id: str, device_name: str) -> None:
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    # Load .env before importing settings
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    os.environ["NEXUS_RELAY_URL"] = relay_url
    os.environ["NEXUS_AGENT_TOKEN"] = token

    _safe_print()
    _safe_print(_c("1;92", "  \u2713  All set! The helper can now access this PC."))
    _safe_print()
    _safe_print(_c("2",   f"     Device    : {device_name}"))
    _safe_print(_c("2",   f"     Device ID : {device_id}"))
    _safe_print(_c("2",   f"     Relay     : {relay_url}"))
    _safe_print()
    _safe_print(_c("93",   "  Keep this window open while the helper is working."))
    _safe_print(_c("93",   "  Close this window at any time to disconnect them.\n"))

    try:
        from agents.desktop_agent import DesktopAgent
    except ImportError as e:
        err(f"Failed to load agent: {e}")
        err("Make sure you're running this from inside the nexus-ras-v2 folder.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    # NOTE: DesktopAgent (agents/desktop_agent.py) subclasses BaseAgent
    # (agents/base_agent.py), whose __init__ signature is:
    #     (relay_url, device_id, device_name, agent_token,
    #      jpeg_quality=60, frame_scale=1.0)
    # There is no `ghost_mode` parameter — passing one raises a TypeError
    # immediately after a successful relay connection. Removed below.
    agent = DesktopAgent(
        relay_url=relay_url,
        device_id=device_id,
        device_name=device_name,
        agent_token=token,
    )

    async def _run():
        attempt = 0
        while True:
            try:
                await agent.run()
                break  # agent.run() returned normally (stopped) — don't loop forever
            except asyncio.CancelledError:
                break
            except KeyboardInterrupt:
                raise
            except Exception:
                attempt += 1
                wait = min(5 * attempt, 60)
                _safe_print(_c("93", f"\r  \u26a0  Lost connection. Reconnecting in {wait}s..."), end="")
                await asyncio.sleep(wait)
                _safe_print(f"\r  \u2192  Reconnecting...{' ' * 30}")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        _safe_print(_c("93", "\n\n  Disconnected. Window can be closed."))
        sys.exit(0)


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--relay",     default=None)
    parser.add_argument("--token",     default=None)
    parser.add_argument("--name",      default=None)
    parser.add_argument("--device-id", default=None)
    args, _ = parser.parse_known_args()

    _safe_print(_c("1;96", """
  \u2588\u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2557  \u2588\u2588\u2557\u2588\u2588\u2557   \u2588\u2588\u2557\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557
  \u2588\u2588\u2588\u2588\u2557  \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d\u255a\u2588\u2588\u2557\u2588\u2588\u2554\u255d\u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u2550\u2550\u255d
  \u2588\u2588\u2554\u2588\u2588\u2557 \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2557   \u255a\u2588\u2588\u2588\u2554\u255d \u2588\u2588\u2551   \u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557
  \u2588\u2588\u2551\u255a\u2588\u2588\u2557\u2588\u2588\u2551\u2588\u2588\u2554\u2550\u2550\u255d   \u2588\u2588\u2554\u2588\u2588\u2557 \u2588\u2588\u2551   \u2588\u2588\u2551\u255a\u2550\u2550\u2550\u2550\u2588\u2588\u2551
  \u2588\u2588\u2551 \u255a\u2588\u2588\u2588\u2588\u2551\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2557\u2588\u2588\u2554\u255d \u2588\u2588\u2557\u255a\u2588\u2588\u2588\u2588\u2588\u2588\u2554\u255d\u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2551
  \u255a\u2550\u255d  \u255a\u2550\u2550\u2550\u255d\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u255d\u255a\u2550\u255d  \u255a\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u255d \u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d
  Remote Support \u2014 Setting everything up automatically...
"""))

    # ── Run checks ────────────────────────────────────────────────────────────
    check_python()
    install_packages()
    check_project_files()
    write_agent_env()

    # ── Get credentials ───────────────────────────────────────────────────────
    if args.relay and args.token:
        hdr("Using Provided Connection Details")
        ok(f"Relay URL   : {args.relay}")
        ok(f"Agent Token : {args.token[:8]}...")
        creds = {"relay": args.relay, "token": args.token}
    else:
        creds = get_credentials()
        if args.relay:
            creds["relay"] = args.relay
        if args.token:
            creds["token"] = args.token

    relay_url = creds["relay"]
    token     = creds["token"]

    device_id   = args.device_id or get_device_id()
    device_name = args.name      or get_device_name()

    # ── Verify connection & launch ───────────────────────────────────────────
    check_relay_reachable(relay_url)
    hdr("Starting Remote Support Agent")
    run_agent(relay_url, token, device_id, device_name)


if __name__ == "__main__":
    # Defense in depth for direct `python connect_remote.py` usage. Note this
    # guard does NOT run when nexus_agent.exe calls agent_main() via import
    # (see nexus_agent_entry.py) — that file has its own top-level guard,
    # which is what actually protects the packaged exe.
    try:
        main()
    except KeyboardInterrupt:
        _safe_print("\n\nInterrupted by user.")
    except SystemExit:
        raise
    except Exception:
        import traceback
        _safe_print("\n" + "=" * 60)
        _safe_print("  FATAL ERROR \u2014 NEXUS Agent crashed")
        _safe_print("=" * 60)
        try:
            traceback.print_exc()
        except Exception:
            pass
        _safe_print("=" * 60)
        try:
            input("\nPress Enter to exit...")
        except Exception:
            pass
        sys.exit(1)