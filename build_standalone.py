"""
build_standalone.py
--------------------
Builds two standalone .exe files using PyInstaller:

  dist/
    nexus_controller.exe   — YOUR PC: setup + relay + dashboard launcher
    nexus_agent.exe        — THEIR PC: zero-touch auto-connector

Run this ONCE on a Windows machine to produce the .exe files:
    python build_standalone.py

Requirements:
    pip install pyinstaller

The output exes have ZERO external dependencies.
The person on the other end just double-clicks nexus_agent.exe.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ── colours ──────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\033[{code}m{t}\033[0m"
def ok(m):   print(_c("92",   f"  ✓  {m}"))
def info(m): print(_c("96",   f"  →  {m}"))
def warn(m): print(_c("93",   f"  ⚠  {m}"))
def err(m):  print(_c("91",   f"  ✗  {m}"))
def hdr(m):  print(_c("1;96", f"\n{'─'*54}\n  {m}\n{'─'*54}"))


# ════════════════════════════════════════════════════════════════════════════
# 1. Ensure PyInstaller is installed
# ════════════════════════════════════════════════════════════════════════════

def ensure_pyinstaller() -> None:
    hdr("Checking PyInstaller")
    try:
        import PyInstaller
        ok(f"PyInstaller {PyInstaller.__version__} found")
    except ImportError:
        info("Installing PyInstaller...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller", "--quiet"],
            check=True
        )
        ok("PyInstaller installed")


# ════════════════════════════════════════════════════════════════════════════
# 2. Ensure all runtime packages are installed (PyInstaller needs to find them)
# ════════════════════════════════════════════════════════════════════════════

RUNTIME_PACKAGES = [
    "fastapi", "uvicorn[standard]", "websockets", "cryptography",
    "PyJWT", "bcrypt", "pydantic", "pydantic-settings", "httpx",
    "psutil", "mss", "Pillow", "pynput", "click", "rich",
    "aiofiles", "aiosqlite", "sqlalchemy", "structlog", "pyotp",
]

def ensure_runtime_packages() -> None:
    hdr("Installing Runtime Packages")
    info("This ensures PyInstaller can bundle everything...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + RUNTIME_PACKAGES,
        check=True
    )
    ok("All runtime packages ready")


# ════════════════════════════════════════════════════════════════════════════
# 3. Write PyInstaller .spec files
# ════════════════════════════════════════════════════════════════════════════

def get_hidden_imports() -> list[str]:
    """
    Packages that PyInstaller misses because they're loaded dynamically.
    Add anything that causes ImportError at runtime here.
    """
    return [
        # FastAPI / Starlette internals
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi.routing",
        "fastapi.security",
        "starlette.routing",
        "starlette.middleware",
        "starlette.middleware.cors",
        # Pydantic
        "pydantic.deprecated.class_validators",
        "pydantic_settings",
        # Cryptography
        "cryptography.hazmat.primitives.ciphers.aead",
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.backends.openssl",
        "cryptography.x509",
        # WebSockets
        "websockets.asyncio.client",
        "websockets.asyncio.server",
        "websockets.legacy.client",
        "websockets.legacy.server",
        # DB
        "sqlalchemy.dialects.sqlite",
        "aiosqlite",
        # Other
        "bcrypt",
        "jwt",
        "pyotp",
        "mss",
        "pynput.keyboard",
        "pynput.mouse",
        "psutil",
        "structlog",
        "click",
        "rich",
        "rich.console",
        "rich.table",
        "rich.panel",
        "rich.progress",
        "httpx",
        "aiofiles",
        "PIL",
        "PIL.Image",
        # Our own modules
        "core.auth",
        "core.registry",
        "core.relay",
        "core.session",
        "agents.base_agent",
        "agents.desktop_agent",
        "agents.server_agent",
        "config.settings",
        "utils.crypto",
        "utils.logger",
        "utils.heartbeat",
        "transport.websocket_transport",
        "transport.tls_context",
        "transport.tcp_transport",
        "transport.tunnel",
        "ui.dashboard",
        "ui.cli",
    ]


def get_collect_all() -> list[str]:
    """Packages that need their entire directory collected (data files included)."""
    return [
        "uvicorn",
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic_settings",
        "cryptography",
        "websockets",
        "rich",
        "click",
    ]


def write_controller_spec() -> Path:
    """Spec for nexus_controller.exe — YOUR PC."""
    hidden = get_hidden_imports()
    collect = get_collect_all()

    hidden_str  = ",\n        ".join(f'"{h}"' for h in hidden)
    collect_str = "\n".join(
        f"coll += collect_all('{p}')" for p in collect
    )

    spec_content = f"""# nexus_controller.spec — auto-generated by build_standalone.py
# -*- mode: python ; coding: utf-8 -*-

import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Collect packages with data files
coll = []
{collect_str}

datas_extra  = [(src, dst) for src, dst, _ in [item for sublist in [c[0] for c in coll] for item in sublist]] if coll else []
binaries_extra = []
hiddenimports_extra = []

a = Analysis(
    ['{BASE_DIR / "setup_and_launch.py"}'],
    pathex=['{BASE_DIR}'],
    binaries=binaries_extra,
    datas=datas_extra + [
        ('{BASE_DIR / "config"}',    'config'),
        ('{BASE_DIR / "core"}',      'core'),
        ('{BASE_DIR / "agents"}',    'agents'),
        ('{BASE_DIR / "transport"}', 'transport'),
        ('{BASE_DIR / "utils"}',     'utils'),
        ('{BASE_DIR / "ui"}',        'ui'),
        ('{BASE_DIR / "service"}',   'service'),
    ],
    hiddenimports=[
        {hidden_str}
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='nexus_controller',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # Keep console open so you see the logs
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
"""
    spec_path = BASE_DIR / "nexus_controller.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    return spec_path


def write_agent_spec() -> Path:
    """Spec for nexus_agent.exe — THEIR PC (the remote)."""
    hidden = get_hidden_imports()

    # Agent only needs a subset — strip out server/dashboard imports
    agent_hidden = [h for h in hidden if not any(
        x in h for x in ["uvicorn", "fastapi", "starlette", "cli"]
    )]
    agent_hidden_str = ",\n        ".join(f'"{h}"' for h in agent_hidden)

    collect = ["pydantic", "pydantic_settings", "cryptography", "websockets", "rich", "click"]
    collect_str = "\n".join(f"coll += collect_all('{p}')" for p in collect)

    spec_content = f"""# nexus_agent.spec — auto-generated by build_standalone.py
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

block_cipher = None

coll = []
{collect_str}

datas_extra = [(src, dst) for src, dst, _ in [item for sublist in [c[0] for c in coll] for item in sublist]] if coll else []

a = Analysis(
    ['{BASE_DIR / "connect_remote.py"}'],
    pathex=['{BASE_DIR}'],
    binaries=[],
    datas=datas_extra + [
        ('{BASE_DIR / "config"}',    'config'),
        ('{BASE_DIR / "core"}',      'core'),
        ('{BASE_DIR / "agents"}',    'agents'),
        ('{BASE_DIR / "transport"}', 'transport'),
        ('{BASE_DIR / "utils"}',     'utils'),
        ('{BASE_DIR / "ui"}',        'ui'),
    ],
    hiddenimports=[
        {agent_hidden_str}
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
              'uvicorn', 'fastapi', 'starlette'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='nexus_agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # Show status window so user knows it's running
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
"""
    spec_path = BASE_DIR / "nexus_agent.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    return spec_path


# ════════════════════════════════════════════════════════════════════════════
# 4. Run PyInstaller builds
# ════════════════════════════════════════════════════════════════════════════

def build(spec_path: Path, label: str) -> bool:
    info(f"Building {label} — this takes 1–3 minutes...")
    result = subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--clean",
            "--noconfirm",
            str(spec_path),
        ],
        cwd=str(BASE_DIR),
        capture_output=False,
    )
    if result.returncode == 0:
        ok(f"{label} built successfully")
        return True
    else:
        err(f"{label} build failed — check output above")
        return False


# ════════════════════════════════════════════════════════════════════════════
# 5. Post-build: show results + instructions
# ════════════════════════════════════════════════════════════════════════════

def show_results() -> None:
    dist_dir = BASE_DIR / "dist"
    hdr("Build Results")

    exes = {
        "nexus_controller.exe": "YOUR PC  — double-click to start the relay + dashboard",
        "nexus_agent.exe":      "THEIR PC — send this + token to the remote person",
    }

    all_ok = True
    for fname, desc in exes.items():
        exe_path = dist_dir / fname
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            ok(f"{fname}  ({size_mb:.0f} MB)  →  {desc}")
        else:
            err(f"{fname}  NOT FOUND — build may have failed")
            all_ok = False

    if not all_ok:
        return

    print(f"""
{_c('1', '  HOW TO USE THE .EXE FILES')}

  {_c('1;93', 'YOUR PC (nexus_controller.exe):')}
    1. Double-click  nexus_controller.exe
    2. It auto-generates your IP + token and starts everything
    3. A file called SEND_TO_REMOTE_PC.txt is created — open it

  {_c('1;93', "THEIR PC (nexus_agent.exe):")}
    1. Send them  nexus_agent.exe  (email, USB, WeTransfer, etc.)
    2. They double-click it
    3. A small window opens asking for the Relay URL and Token
    4. They paste what's in your SEND_TO_REMOTE_PC.txt and press Enter
    5. Done — you have full control

  {_c('2', f'Files are in: {dist_dir}')}
  {_c('2', 'Antivirus note: some AV flags PyInstaller .exe files as suspicious.')}
  {_c('2', 'This is a false positive — add an exclusion in your AV if needed.')}
""")


# ════════════════════════════════════════════════════════════════════════════
# 6. Write a wrapper for nexus_agent.exe that prompts for relay+token
#    (so they don't have to pass CLI args)
# ════════════════════════════════════════════════════════════════════════════

AGENT_WRAPPER = '''"""
nexus_agent_entry.py
---------------------
Entry point for nexus_agent.exe.
Prompts the user for relay URL and token if not passed as arguments,
so they can just double-click the .exe and paste what they received.
"""
from __future__ import annotations
import os, sys, argparse
from pathlib import Path

if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\\033[{code}m{t}\\033[0m"

def main():
    # Check if args were passed (e.g. from command line)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--relay", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--name",  default=None)
    known, _ = parser.parse_known_args()

    relay = known.relay
    token = known.token
    name  = known.name

    print(_c("1;96", """
  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
  Remote Support Agent — Connecting to your helper
"""))

    if not relay:
        print(_c("93", "  Paste the Relay URL your helper sent you:"))
        print(_c("2",  "  (looks like: ws://102.45.67.89:7000)"))
        relay = input("  Relay URL > ").strip()

    if not token:
        print(_c("93", "\\n  Paste the Agent Token your helper sent you:"))
        token = input("  Token     > ").strip()

    if not relay or not token:
        print(_c("91", "\\n  ✗  Relay URL and Token are both required. Closing."))
        input("  Press Enter to exit...")
        sys.exit(1)

    # Hand off to connect_remote main logic
    sys.argv = ["connect_remote.py", "--relay", relay, "--token", token]
    if name:
        sys.argv += ["--name", name]

    # Add project root to path
    base = Path(__file__).resolve().parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    from connect_remote import main as agent_main
    agent_main()

if __name__ == "__main__":
    main()
'''

def write_agent_wrapper() -> Path:
    wrapper = BASE_DIR / "nexus_agent_entry.py"
    wrapper.write_text(AGENT_WRAPPER, encoding="utf-8")
    return wrapper


# ════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(_c("1;96", """
  ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
  NEXUS — Standalone .EXE Builder
  Packages Python + all dependencies into single files
"""))

    if sys.platform != "win32":
        warn("You're not on Windows.")
        warn("PyInstaller builds for the OS it runs on.")
        warn("To build Windows .exe files, run this script on a Windows machine.")
        warn("Continuing anyway — will produce Linux/macOS binaries instead.")
        input("\nPress Enter to continue or Ctrl+C to cancel...")

    ensure_pyinstaller()
    ensure_runtime_packages()

    hdr("Writing Build Specs")
    write_agent_wrapper()
    ok("nexus_agent_entry.py written (wraps connect_remote with a prompt UI)")

    controller_spec = write_controller_spec()
    ok(f"nexus_controller.spec written")

    # For agent, build from the wrapper so it prompts for relay+token on double-click
    agent_spec = write_agent_spec()
    ok(f"nexus_agent.spec written")

    hdr("Building nexus_controller.exe (YOUR PC)")
    ctrl_ok = build(controller_spec, "nexus_controller.exe")

    hdr("Building nexus_agent.exe (THEIR PC)")
    # Update spec to point at wrapper entry point
    agent_spec_text = agent_spec.read_text()
    agent_spec_text = agent_spec_text.replace(
        str(BASE_DIR / "connect_remote.py"),
        str(BASE_DIR / "nexus_agent_entry.py"),
    )
    agent_spec.write_text(agent_spec_text)
    agent_ok = build(agent_spec, "nexus_agent.exe")

    if ctrl_ok and agent_ok:
        show_results()
    else:
        err("One or more builds failed. Common fixes:")
        err("  • Run as Administrator")
        err("  • Temporarily disable antivirus during build")
        err("  • pip install pyinstaller --upgrade")

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()
