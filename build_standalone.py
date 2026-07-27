# build_standalone.py (final – recursive environment DLL collection)
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\033[{code}m{t}\033[0m"
def ok(m):   print(_c("92", f"  ✓  {m}"))
def info(m): print(_c("96", f"  →  {m}"))
def warn(m): print(_c("93", f"  ⚠  {m}"))
def err(m):  print(_c("91", f"  ✗  {m}"))
def hdr(m):  print(_c("1;96", f"\n{'─' * 54}\n  {m}\n{'─' * 54}"))


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
# 2. Ensure all runtime packages are installed
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
# 3. Recursive environment‑wide DLL collector
# ════════════════════════════════════════════════════════════════════════════

def get_all_env_binaries() -> list[tuple[str, str]]:
    """
    Collect ALL .dll and .pyd files from the entire Python environment.
    This ensures that even deeply nested DLLs (like ffi.dll) are bundled.
    """
    binaries = []
    seen = set()
    env_root = Path(sys.prefix)

    info(f"Scanning {env_root} for native libraries (this may take a moment)...")

    # Recursively find all .dll and .pyd files
    for pattern in ["*.dll", "*.pyd"]:
        for file in env_root.rglob(pattern):
            if file.name.lower() not in seen:
                seen.add(file.name.lower())
                binaries.append((file.as_posix(), "."))

    if not binaries:
        err("No native libraries found! The build will fail.")
    else:
        ok(f"Found {len(binaries)} native library files")
        # Print a sample to confirm key files
        dll_count = sum(1 for p, _ in binaries if p.lower().endswith('.dll'))
        pyd_count = sum(1 for p, _ in binaries if p.lower().endswith('.pyd'))
        dir(f"  DLLs: {dll_count}, PYD files: {pyd_count}")

    return binaries


# ════════════════════════════════════════════════════════════════════════════
# 4. Runtime hook for DLL search path (still useful)
# ════════════════════════════════════════════════════════════════════════════

def write_runtime_hook() -> Path:
    hook_path = BASE_DIR / "rth_openssl_fix.py"
    hook_content = '''import os
import sys

if sys.platform == "win32":
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and os.path.exists(meipass):
        try:
            os.add_dll_directory(meipass)
        except Exception:
            pass
        os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")
'''
    hook_path.write_text(hook_content, encoding="utf-8")
    return hook_path


# ════════════════════════════════════════════════════════════════════════════
# 5. Write PyInstaller .spec files
# ════════════════════════════════════════════════════════════════════════════

def get_hidden_imports() -> list[str]:
    return [
        "_ssl", "ssl", "_hashlib", "hashlib", "_asyncio", "asyncio",
        "_ctypes", "ctypes",
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.loops.asyncio", "uvicorn.protocols",
        "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
        "fastapi.routing", "fastapi.security",
        "starlette.routing", "starlette.middleware", "starlette.middleware.cors",
        "pydantic.deprecated.class_validators", "pydantic_settings",
        "cryptography", "cryptography.hazmat.primitives.ciphers.aead",
        "cryptography.hazmat.primitives.asymmetric.ec",
        "cryptography.hazmat.primitives.asymmetric.rsa",
        "cryptography.hazmat.backends.openssl", "cryptography.x509",
        "websockets.asyncio.client", "websockets.asyncio.server",
        "websockets.legacy.client", "websockets.legacy.server",
        "sqlalchemy.dialects.sqlite", "aiosqlite", "bcrypt", "jwt", "pyotp",
        "mss", "mss.windows", "pynput", "pynput.keyboard",
        "pynput.keyboard._win32", "pynput.mouse", "pynput.mouse._win32",
        "psutil", "structlog", "click", "rich", "rich.console",
        "rich.table", "rich.panel", "rich.progress", "httpx", "aiofiles",
        "PIL", "PIL.Image", "PIL.JpegImagePlugin", "PIL.PngImagePlugin",
        "core.auth", "core.registry", "core.relay", "core.session",
        "agents.base_agent", "agents.desktop_agent", "agents.server_agent",
        "config.settings", "utils.crypto", "utils.logger", "utils.heartbeat",
        "transport.websocket_transport", "transport.tls_context",
        "transport.tcp_transport", "transport.tunnel", "ui.dashboard", "ui.cli",
    ]

def get_controller_collect_pkgs() -> list[str]:
    return [
        "uvicorn", "fastapi", "starlette", "pydantic", "pydantic_settings",
        "cryptography", "websockets", "rich", "click", "pynput", "mss",
        "PIL", "psutil", "structlog", "aiosqlite", "sqlalchemy", "bcrypt", "pyotp",
    ]

def get_agent_collect_pkgs() -> list[str]:
    return [
        "pydantic", "pydantic_settings", "cryptography", "websockets", "rich",
        "click", "pynput", "mss", "PIL", "psutil", "structlog",
    ]

def write_controller_spec() -> Path:
    hidden = get_hidden_imports()
    collect_pkgs = get_controller_collect_pkgs()
    all_binaries = get_all_env_binaries()  # use the recursive collector

    # Deduplicate
    uniq = {}
    for p, d in all_binaries:
        uniq[p] = (p, d)
    all_binaries = list(uniq.values())

    B = BASE_DIR.as_posix()
    hidden_str = ",\n        ".join(f'"{h}"' for h in hidden)
    collect_pkgs_repr = repr(collect_pkgs)
    binaries_repr = repr(all_binaries)

    spec_content = f"""# nexus_controller.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
block_cipher = None
datas = [
    ('{B}/config',    'config'),
    ('{B}/core',      'core'),
    ('{B}/agents',    'agents'),
    ('{B}/transport', 'transport'),
    ('{B}/utils',     'utils'),
    ('{B}/ui',        'ui'),
    ('{B}/service',   'service'),
]
binaries = {binaries_repr}
hiddenimports = [
    {hidden_str}
]
for pkg in {collect_pkgs_repr}:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
a = Analysis(
    ['{B}/setup_and_launch.py'],
    pathex=['{B}'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=['{B}/rth_openssl_fix.py'],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='nexus_controller', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None, console=True,
    disable_windowed_traceback=False, target_arch=None, codesign_identity=None,
    entitlements_file=None, icon=None,
)
"""
    spec_path = BASE_DIR / "nexus_controller.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    ok("nexus_controller.spec written")
    return spec_path


def write_agent_spec() -> Path:
    hidden = get_hidden_imports()
    agent_hidden = [h for h in hidden if not any(
        x in h for x in ["uvicorn", "fastapi", "starlette", "cli"]
    )]
    agent_hidden_str = ",\n        ".join(f'"{h}"' for h in agent_hidden)
    collect_pkgs = get_agent_collect_pkgs()
    collect_pkgs_repr = repr(collect_pkgs)
    all_binaries = get_all_env_binaries()

    uniq = {}
    for p, d in all_binaries:
        uniq[p] = (p, d)
    all_binaries = list(uniq.values())

    B = BASE_DIR.as_posix()
    binaries_repr = repr(all_binaries)

    spec_content = f"""# nexus_agent.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
block_cipher = None
datas = [
    ('{B}/config',    'config'),
    ('{B}/core',      'core'),
    ('{B}/agents',    'agents'),
    ('{B}/transport', 'transport'),
    ('{B}/utils',     'utils'),
    ('{B}/ui',        'ui'),
]
binaries = {binaries_repr}
hiddenimports = [
    {agent_hidden_str}
]
for pkg in {collect_pkgs_repr}:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
a = Analysis(
    ['{B}/nexus_agent_entry.py'],
    pathex=['{B}'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=['{B}/rth_openssl_fix.py'],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy',
              'uvicorn', 'fastapi', 'starlette'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='nexus_agent', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, upx_exclude=[], runtime_tmpdir=None, console=True,
    disable_windowed_traceback=False, target_arch=None, codesign_identity=None,
    entitlements_file=None, icon=None,
)
"""
    spec_path = BASE_DIR / "nexus_agent.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    ok("nexus_agent.spec written")
    return spec_path


# ════════════════════════════════════════════════════════════════════════════
# 6. Run PyInstaller builds
# ════════════════════════════════════════════════════════════════════════════

def build(spec_path: Path, label: str) -> bool:
    info(f"Building {label} — this takes 1–3 minutes...")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_path)],
        cwd=str(BASE_DIR), capture_output=False
    )
    if result.returncode == 0:
        ok(f"{label} built successfully")
        return True
    err(f"{label} build failed — check output above")
    return False


# ════════════════════════════════════════════════════════════════════════════
# 7. Post-build: show results + instructions
# ════════════════════════════════════════════════════════════════════════════

def show_results() -> None:
    dist_dir = BASE_DIR / "dist"
    hdr("Build Results")
    exes = {
        "nexus_controller.exe": "YOUR PC  — double-click to start",
        "nexus_agent.exe": "THEIR PC — send this + token",
    }
    for fname, desc in exes.items():
        exe_path = dist_dir / fname
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            ok(f"{fname}  ({size_mb:.0f} MB)  →  {desc}")
        else:
            err(f"{fname}  NOT FOUND — build may have failed")
            return
    print(f"""
{_c('1', '  HOW TO USE THE .EXE FILES')}
  YOUR PC: double-click nexus_controller.exe
  THEIR PC: double-click nexus_agent.exe, paste relay URL and token
  Files are in: {dist_dir}
""")


# ════════════════════════════════════════════════════════════════════════════
# 8. Write agent wrapper entry point
# ════════════════════════════════════════════════════════════════════════════

AGENT_WRAPPER = '''"""
nexus_agent_entry.py
---------------------
Entry point for nexus_agent.exe.
"""
from __future__ import annotations
import os, sys, argparse
from pathlib import Path

if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\\033[{code}m{t}\\033[0m"

def main():
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

    sys.argv = ["connect_remote.py", "--relay", relay, "--token", token]
    if name:
        sys.argv += ["--name", name]

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

    ensure_pyinstaller()
    ensure_runtime_packages()

    hdr("Writing Build Specs & Runtime Hooks")
    write_runtime_hook()
    ok("rth_openssl_fix.py written")

    write_agent_wrapper()
    ok("nexus_agent_entry.py written")

    controller_spec = write_controller_spec()
    agent_spec = write_agent_spec()

    hdr("Building nexus_controller.exe (YOUR PC)")
    ctrl_ok = build(controller_spec, "nexus_controller.exe")

    hdr("Building nexus_agent.exe (THEIR PC)")
    agent_ok = build(agent_spec, "nexus_agent.exe")

    if ctrl_ok and agent_ok:
        show_results()
    else:
        err("One or more builds failed.")

    input("\nPress Enter to exit...")


if __name__ == "__main__":
    main()