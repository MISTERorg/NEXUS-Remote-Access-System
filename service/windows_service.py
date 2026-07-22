"""
service/windows_service.py
--------------------------
Windows Ghost-Mode Service Installer for NEXUS Agent.

Uses NSSM (Non-Sucking Service Manager) to wrap the Python agent as a
proper Windows Service that:
  - Starts automatically at boot (before user login)
  - Runs in SESSION 0 (System context) — invisible to the logged-in user
  - Restarts automatically on crash
  - Redirects all stdout/stderr to a log file (no console window ever)

Requirements:
  - NSSM downloaded and placed in PATH, or set NSSM_PATH below.
    Download: https://nssm.cc/download
  - Python installed system-wide (not just for current user)
  - Run this installer as Administrator

Usage:
    python service/windows_service.py install \
        --relay wss://your-relay:7000 \
        --device-id my-desktop \
        --token YOUR_AGENT_TOKEN

    python service/windows_service.py uninstall
    python service/windows_service.py start
    python service/windows_service.py stop
    python service/windows_service.py status
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "NexusGhostAgent"
SERVICE_DISPLAY = "NEXUS Remote Access Agent"
SERVICE_DESCRIPTION = "NEXUS Ghost-Mode Remote Access Agent - secure background agent"

# Adjust if nssm.exe is not in PATH
NSSM_PATH = os.environ.get("NSSM_PATH", "nssm")

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    print(f"  > {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=False, check=check)


def _python() -> str:
    """Return the path to the Python executable."""
    return sys.executable


def install(relay: str, device_id: str, token: str, name: str = "") -> None:
    """Install the NEXUS agent as a Windows service via NSSM."""
    agent_script = str(BASE_DIR / "agents" / "desktop_agent.py")
    agent_args = (
        f'"{agent_script}" '
        f"--relay {relay} "
        f"--device-id {device_id} "
        f"--token {token} "
        f"--ghost"
    )
    if name:
        agent_args += f" --name {name}"

    stdout_log = str(LOG_DIR / "agent_stdout.log")
    stderr_log = str(LOG_DIR / "agent_stderr.log")

    print(f"[+] Installing service: {SERVICE_NAME}")

    # Install
    _run([NSSM_PATH, "install", SERVICE_NAME, _python()])
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppParameters", agent_args])
    _run([NSSM_PATH, "set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY])
    _run([NSSM_PATH, "set", SERVICE_NAME, "Description", SERVICE_DESCRIPTION])
    _run([NSSM_PATH, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"])
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppStdout", stdout_log])
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppStderr", stderr_log])
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppRotateFiles", "1"])
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppRotateBytes", "10485760"])  # 10 MB
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppRestartDelay", "5000"])      # 5s restart delay
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppNoConsole", "1"])            # NO console window

    # Set working directory
    _run([NSSM_PATH, "set", SERVICE_NAME, "AppDirectory", str(BASE_DIR)])

    print(f"[✓] Service installed. Start with: python service/windows_service.py start")
    print(f"    Logs → {stdout_log}")


def uninstall() -> None:
    print(f"[+] Uninstalling service: {SERVICE_NAME}")
    _run([NSSM_PATH, "stop", SERVICE_NAME], check=False)
    _run([NSSM_PATH, "remove", SERVICE_NAME, "confirm"])
    print("[✓] Service removed.")


def start() -> None:
    _run([NSSM_PATH, "start", SERVICE_NAME])
    print(f"[✓] {SERVICE_NAME} started.")


def stop() -> None:
    _run([NSSM_PATH, "stop", SERVICE_NAME])
    print(f"[✓] {SERVICE_NAME} stopped.")


def status() -> None:
    _run([NSSM_PATH, "status", SERVICE_NAME], check=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    if sys.platform != "win32":
        print("ERROR: This script is for Windows only. Use service/linux_service.py on Linux.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="NEXUS Windows Service Manager")
    subparsers = parser.add_subparsers(dest="command")

    # install
    install_p = subparsers.add_parser("install")
    install_p.add_argument("--relay", required=True)
    install_p.add_argument("--device-id", required=True)
    install_p.add_argument("--token", required=True)
    install_p.add_argument("--name", default="")

    subparsers.add_parser("uninstall")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")

    args = parser.parse_args()

    if args.command == "install":
        install(args.relay, args.device_id, args.token, args.name)
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    else:
        parser.print_help()
