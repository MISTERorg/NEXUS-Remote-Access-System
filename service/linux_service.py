"""
service/linux_service.py
------------------------
Linux systemd Ghost-Mode Service Installer for NEXUS Agent.

Generates and installs a systemd unit file that runs the NEXUS agent:
  - As a system service (starts at boot, before any user login)
  - With StandardOutput=null and StandardError=null (no console output)
  - With automatic restart on failure (Restart=always)
  - Optionally as a specific user (recommended over root)

Usage (run as root or with sudo):
    sudo python service/linux_service.py install \
        --relay wss://your-relay:7000 \
        --device-id my-linux-server \
        --token YOUR_AGENT_TOKEN \
        --user nexus                    # optional: run as 'nexus' user

    sudo python service/linux_service.py uninstall
    sudo python service/linux_service.py start
    sudo python service/linux_service.py stop
    sudo python service/linux_service.py status
    sudo python service/linux_service.py logs        # tail service journal
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

SERVICE_NAME = "nexus-ghost-agent"
BASE_DIR = Path(__file__).resolve().parent.parent
UNIT_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
LOG_DIR = BASE_DIR / "logs"


def _run(cmd: list, check: bool = True) -> None:
    print(f"  > {' '.join(cmd)}")
    subprocess.run(cmd, check=check)


def _python() -> str:
    return sys.executable


def generate_unit(
    relay: str,
    device_id: str,
    token: str,
    name: str = "",
    user: str = "root",
) -> str:
    """Generate the systemd unit file content."""
    agent_script = str(BASE_DIR / "agents" / "desktop_agent.py")
    exec_args = (
        f"{_python()} {agent_script} "
        f"--relay {relay} "
        f"--device-id {device_id} "
        f"--token {token} "
        f"--ghost"
    )
    if name:
        exec_args += f" --name {name}"

    return dedent(f"""\
        [Unit]
        Description=NEXUS Ghost Remote Access Agent
        Documentation=https://github.com/your-org/nexus-ras
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        User={user}
        WorkingDirectory={BASE_DIR}
        ExecStart={exec_args}
        Restart=always
        RestartSec=5s
        StartLimitIntervalSec=60s
        StartLimitBurst=5

        # Ghost Mode: suppress ALL output (no console window equivalent on Linux)
        StandardOutput=null
        StandardError=null

        # Resource limits (tune for your hardware)
        MemoryMax=256M
        CPUQuota=25%

        # Security hardening
        NoNewPrivileges=true
        PrivateTmp=true
        ProtectSystem=strict
        ReadWritePaths={BASE_DIR}/logs {BASE_DIR}/data {Path.home() / ".nexus"}

        # Environment
        Environment=NEXUS_GHOST=1
        Environment=PYTHONUNBUFFERED=1

        [Install]
        WantedBy=multi-user.target
    """)


def install(
    relay: str,
    device_id: str,
    token: str,
    name: str = "",
    user: str = "root",
    enable: bool = True,
) -> None:
    """Install and optionally enable + start the service."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    unit_content = generate_unit(relay, device_id, token, name, user)

    print(f"[+] Writing unit file: {UNIT_FILE}")
    UNIT_FILE.write_text(unit_content)
    print("[✓] Unit file written.")

    _run(["systemctl", "daemon-reload"])

    if enable:
        _run(["systemctl", "enable", SERVICE_NAME])
        _run(["systemctl", "start", SERVICE_NAME])
        print(f"[✓] Service enabled and started.")
    else:
        print(f"[i] Service installed but not started.")
        print(f"    Start manually: sudo systemctl start {SERVICE_NAME}")

    print(f"\nUseful commands:")
    print(f"  Status : sudo systemctl status {SERVICE_NAME}")
    print(f"  Logs   : sudo journalctl -u {SERVICE_NAME} -f")
    print(f"  Stop   : sudo systemctl stop {SERVICE_NAME}")


def uninstall() -> None:
    print(f"[+] Removing service: {SERVICE_NAME}")
    _run(["systemctl", "stop", SERVICE_NAME], check=False)
    _run(["systemctl", "disable", SERVICE_NAME], check=False)
    if UNIT_FILE.exists():
        UNIT_FILE.unlink()
        print(f"[✓] Removed {UNIT_FILE}")
    _run(["systemctl", "daemon-reload"])
    print("[✓] Service removed.")


def start() -> None:
    _run(["systemctl", "start", SERVICE_NAME])


def stop() -> None:
    _run(["systemctl", "stop", SERVICE_NAME])


def status() -> None:
    _run(["systemctl", "status", SERVICE_NAME], check=False)


def logs(lines: int = 50) -> None:
    _run(["journalctl", "-u", SERVICE_NAME, f"-n{lines}", "--no-pager"], check=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        print("ERROR: This script is for Linux only. Use service/windows_service.py on Windows.")
        sys.exit(1)

    if os.geteuid() != 0:
        print("WARNING: This script typically requires root (sudo) to install systemd services.")

    parser = argparse.ArgumentParser(description="NEXUS Linux Service Manager")
    subparsers = parser.add_subparsers(dest="command")

    install_p = subparsers.add_parser("install")
    install_p.add_argument("--relay", required=True)
    install_p.add_argument("--device-id", required=True)
    install_p.add_argument("--token", required=True)
    install_p.add_argument("--name", default="")
    install_p.add_argument("--user", default="root", help="System user to run the agent as")
    install_p.add_argument("--no-enable", action="store_true", help="Install but don't start")

    subparsers.add_parser("uninstall")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    logs_p = subparsers.add_parser("logs")
    logs_p.add_argument("-n", type=int, default=50, help="Number of log lines")

    args = parser.parse_args()

    if args.command == "install":
        install(
            relay=args.relay,
            device_id=args.device_id,
            token=args.token,
            name=args.name,
            user=args.user,
            enable=not args.no_enable,
        )
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "status":
        status()
    elif args.command == "logs":
        logs(args.n)
    else:
        parser.print_help()
