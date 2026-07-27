"""
build_server.py
----------------
Robust standalone .EXE builder for server.py.
Collects all native C libraries (DLLs/PYDs) and injects runtime DLL directory hooks
to resolve '_ctypes' and C-runtime import failures.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
import PyInstaller.__main__

BASE_DIR = Path(__file__).resolve().parent

if sys.platform == "win32":
    os.system("color")

def _c(code, t): return f"\033[{code}m{t}\033[0m"
def ok(m):   print(_c("92", f"  ✓  {m}"))
def info(m): print(_c("96", f"  →  {m}"))
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
# 2. Ensure all required dependencies are installed
# ════════════════════════════════════════════════════════════════════════════

RUNTIME_PACKAGES = [
    "pyautogui", "Pillow", "pyscreeze", "pytweening", "mouse", "keyboard"
]

def ensure_runtime_packages() -> None:
    hdr("Installing Runtime Dependencies")
    info("Ensuring all required packages are present in environment...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + RUNTIME_PACKAGES,
        check=True
    )
    ok("All dependencies verified")


# ════════════════════════════════════════════════════════════════════════════
# 3. Recursive Environment DLL / PYD Collector
# ════════════════════════════════════════════════════════════════════════════

def get_all_env_binaries() -> list[tuple[str, str]]:
    """
    Recursively scans the Python environment for native C libraries (.dll, .pyd).
    Guarantees that libffi.dll, _ctypes.pyd, and runtime DLLs are included.
    """
    binaries = []
    seen = set()
    env_root = Path(sys.prefix)

    info(f"Scanning {env_root} for native libraries (.dll / .pyd)...")

    for pattern in ["*.dll", "*.pyd"]:
        for file in env_root.rglob(pattern):
            if file.name.lower() not in seen:
                seen.add(file.name.lower())
                binaries.append((file.as_posix(), "."))

    if not binaries:
        err("No native libraries found! Build may fail.")
    else:
        ok(f"Found {len(binaries)} native library files")

    return binaries


# ════════════════════════════════════════════════════════════════════════════
# 4. Runtime Hook for Windows DLL Search Directory
# ════════════════════════════════════════════════════════════════════════════

def write_runtime_hook() -> Path:
    """
    Writes a runtime hook that forces Windows to add sys._MEIPASS (the extracted
    temporary folder) to the OS DLL search directory before python module loads.
    """
    hook_path = BASE_DIR / "rth_dll_fix.py"
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
# 5. Write PyInstaller Spec File
# ════════════════════════════════════════════════════════════════════════════

def write_server_spec() -> Path:
    all_binaries = get_all_env_binaries()

    # Deduplicate binaries
    uniq = {}
    for p, d in all_binaries:
        uniq[p] = (p, d)
    all_binaries = list(uniq.values())

    B = BASE_DIR.as_posix()
    binaries_repr = repr(all_binaries)

    hidden_imports = [
        "_ctypes", "ctypes", "pyautogui", "pyscreeze", "pytweening",
        "PIL", "PIL.Image", "PIL.JpegImagePlugin", "PIL._tkinter_finder",
        "mouse", "keyboard", "socket", "struct", "threading", "io", "time"
    ]
    hidden_str = ",\n        ".join(f'"{h}"' for h in hidden_imports)

    spec_content = f"""# server.spec
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = []
binaries = {binaries_repr}
hiddenimports = [
    {hidden_str}
]

for pkg in ['pyautogui', 'PIL', 'pyscreeze']:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ['{B}/server.py'],
    pathex=['{B}'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=['{B}/rth_dll_fix.py'],
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
    name='RemoteDesktopServer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    uac_admin=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
"""
    spec_path = BASE_DIR / "server.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    ok("server.spec file created")
    return spec_path


# ════════════════════════════════════════════════════════════════════════════
# 6. Run Build
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(_c("1;96", """
  ==================================================
   Remote Desktop Server — Robust Standalone Builder
  ==================================================
"""))

    ensure_pyinstaller()
    ensure_runtime_packages()

    hdr("Preparing Build Fixes & Spec File")
    write_runtime_hook()
    ok("rth_dll_fix.py runtime hook generated")

    spec_path = write_server_spec()

    hdr("Building RemoteDesktopServer.exe")
    info("Compiling binary (this may take 1–2 minutes)...")

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_path)],
        cwd=str(BASE_DIR)
    )

    if result.returncode == 0:
        dist_exe = BASE_DIR / "dist" / "RemoteDesktopServer.exe"
        hdr("Build Successful!")
        if dist_exe.exists():
            size_mb = dist_exe.stat().st_size / (1024 * 1024)
            ok(f"Generated: {dist_exe} ({size_mb:.1f} MB)")
    else:
        err("Build failed. Check the error log above.")


if __name__ == "__main__":
    main()