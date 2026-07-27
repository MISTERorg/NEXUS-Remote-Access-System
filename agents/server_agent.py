"""
agents/server_agent.py
----------------------
Headless server agent — no screen capture or GUI input.
Focused on: terminal access, file management, metrics monitoring.

Also contains:
  - IoTAgent  : lightweight agent for Raspberry Pi / constrained hardware
  - MobileAgent: Termux / Python-based mobile stub

Improvements over v1:
  - Lifecycle hooks (on_session_start / on_session_end) properly wired
  - Terminal drain uses asyncio.wait_for with tighter timeout
  - Graceful terminal teardown with SIGTERM → SIGKILL fallback
  - IoTAgent supports optional GPIO metrics hook
  - MobileAgent no longer calls ServerAgent methods via type: ignore hacks
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.base_agent import BaseAgent
from core.registry import DeviceCapabilities, DeviceType
from core.session import MessageType
from config.settings import settings
from utils.logger import AuditLogger, get_logger

log = get_logger("nexus.agent.server")
audit = AuditLogger(settings.log.audit_file)


# ===========================================================================
# ServerAgent
# ===========================================================================

class ServerAgent(BaseAgent):
    """
    Headless server agent for Linux/Windows servers, Docker containers,
    cloud VMs, and any host without a display.
    """

    def __init__(self, relay_url: str, device_id: str, device_name: str, agent_token: str):
        super().__init__(relay_url, device_id, device_name, agent_token)
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.SERVER

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            screen_share=False,
            remote_input=False,
            file_transfer=True,
            terminal=True,
            clipboard=False,
            metrics=True,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_session_start(self) -> None:
        logging.info(f"[server_agent] Session started: {self._current_session_id}")

    async def on_session_end(self) -> None:
        # Close all open terminals when session ends
        tids = list(self._terminals.keys())
        for tid in tids:
            await self.on_terminal_close(tid)
        logging.info("[server_agent] Session ended, all terminals closed")

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    async def on_terminal_open(self, terminal_id: str) -> bool:
        try:
            shell = os.environ.get("SHELL", "/bin/bash")
            proc = await asyncio.create_subprocess_shell(
                shell,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "TERM": "xterm-256color"},
            )
            self._terminals[terminal_id] = proc
            asyncio.create_task(self._drain_terminal(terminal_id, proc))
            logging.info(f"[server_agent] Terminal opened: {terminal_id} PID={proc.pid}")
            return True
        except Exception as e:
            logging.error(f"[server_agent] Terminal open failed: {e}")
            return False

    async def _drain_terminal(
        self, terminal_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        """Background reader: push terminal output to the session stream."""
        while proc.returncode is None:
            try:
                data = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.05)
                if data:
                    await self._send_session(
                        MessageType.TERMINAL_DATA,
                        {
                            "terminal_id": terminal_id,
                            "data": data.decode("utf-8", errors="replace"),
                        },
                    )
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
        logging.info(
            f"[server_agent] Terminal exited: {terminal_id} rc={proc.returncode}"
        )

    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]:
        proc = self._terminals.get(terminal_id)
        if proc and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write(data.encode())
                await proc.stdin.drain()
            except Exception as e:
                logging.warning(f"[server_agent] stdin write error: {e}")
        return None  # output is pushed asynchronously by _drain_terminal

    async def on_terminal_close(self, terminal_id: str) -> None:
        proc = self._terminals.pop(terminal_id, None)
        if not proc:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logging.warning(f"[server_agent] Terminal {terminal_id} didn't exit, killing")
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        logging.info(f"[server_agent] Terminal closed: {terminal_id}")

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def on_file_list(self, path: str) -> List[dict]:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return []
        entries = []
        try:
            for item in sorted(p.iterdir(), key=lambda i: (not i.is_dir(), i.name)):
                try:
                    stat = item.stat()
                    entries.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "permissions": oct(stat.st_mode)[-3:],
                    })
                except PermissionError:
                    pass
        except PermissionError:
            pass
        return entries

    async def on_file_download(self, path: str) -> Optional[bytes]:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            return None
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > settings.agent.max_transfer_mb:
            logging.warning(
                f"[server_agent] File too large: {path} ({size_mb:.1f} MB)"
            )
            return None
        return p.read_bytes()

    async def on_file_upload(self, path: str, data: bytes) -> bool:
        try:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            audit.file_transfer(
                session_id=self._current_session_id or "unknown",
                direction="upload",
                filename=p.name,
                size_bytes=len(data),
            )
            return True
        except Exception as e:
            logging.error(f"[server_agent] File upload error: {e}")
            return False


# ===========================================================================
# IoTAgent
# ===========================================================================

class IoTAgent(BaseAgent):
    """
    Minimal IoT agent for constrained hardware:
    Raspberry Pi, ESP32-with-Linux, industrial sensors, etc.

    Strips everything down to: terminal access + metrics.
    Screen share and file transfer are disabled by default.

    GPIO metrics:
        Subclass IoTAgent and override _collect_metrics() to append
        RPi.GPIO or gpiozero readings to the base dict.

    Example:
        class MyPiAgent(IoTAgent):
            def _collect_metrics(self):
                base = super()._collect_metrics()
                import RPi.GPIO as GPIO
                base["gpio_pin_17"] = float(GPIO.input(17))
                return base
    """

    def __init__(
        self,
        relay_url: str,
        device_id: str,
        device_name: str,
        agent_token: str,
        gpio_available: bool = False,
    ):
        super().__init__(relay_url, device_id, device_name, agent_token)
        self._gpio_available = gpio_available
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.IOT

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            screen_share=False,
            remote_input=False,
            file_transfer=False,
            terminal=True,
            clipboard=False,
            metrics=True,
        )

    async def on_session_start(self) -> None:
        logging.info(f"[iot_agent] Session started: {self._current_session_id}")

    async def on_session_end(self) -> None:
        for tid in list(self._terminals.keys()):
            await self.on_terminal_close(tid)
        logging.info("[iot_agent] Session ended")

    async def on_terminal_open(self, terminal_id: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_shell(
                "/bin/sh",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._terminals[terminal_id] = proc
            asyncio.create_task(self._drain(terminal_id, proc))
            return True
        except Exception as e:
            logging.error(f"[iot_agent] Terminal open failed: {e}")
            return False

    async def _drain(
        self, terminal_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        while proc.returncode is None:
            try:
                data = await asyncio.wait_for(proc.stdout.read(1024), timeout=0.1)
                if data:
                    await self._send_session(
                        MessageType.TERMINAL_DATA,
                        {
                            "terminal_id": terminal_id,
                            "data": data.decode(errors="replace"),
                        },
                    )
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]:
        proc = self._terminals.get(terminal_id)
        if proc and proc.stdin and not proc.stdin.is_closing():
            proc.stdin.write(data.encode())
            await proc.stdin.drain()
        return None

    async def on_terminal_close(self, terminal_id: str) -> None:
        proc = self._terminals.pop(terminal_id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _collect_metrics(self) -> Dict[str, Any]:
        """Override in subclass to add GPIO / sensor readings."""
        base = super()._collect_metrics()
        if self._gpio_available:
            # Hook: subclass adds GPIO values here
            base["gpio_hook"] = "override _collect_metrics() in your IoTAgent subclass"
        return base


# ===========================================================================
# MobileAgent
# ===========================================================================

class MobileAgent(BaseAgent):
    """
    Mobile device agent.

    Python stub — production use:
      - Android: rewrite in Kotlin using the same WebSocket protocol
      - iOS: rewrite in Swift using the same WebSocket protocol
      - Termux on Android: this Python stub works as-is

    Delegates file and terminal ops to an embedded ServerAgent instance
    rather than using ServerAgent.method(self, ...) type-ignore hacks.
    """

    def __init__(self, relay_url: str, device_id: str, device_name: str, agent_token: str):
        super().__init__(relay_url, device_id, device_name, agent_token)
        # Delegate impl — we reuse ServerAgent's file/terminal logic
        # via composition, not inheritance
        self._delegate = _MobileDelegateAgent(relay_url, device_id, device_name, agent_token)
        # Share session state with delegate so it can call _send_session
        self._delegate._get_session_state = self._get_session_state

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.MOBILE

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            screen_share=False,
            remote_input=False,
            file_transfer=True,
            terminal=True,
            clipboard=False,
            metrics=True,
        )

    def _get_session_state(self):
        return self._current_session_id, self._cipher

    async def on_session_start(self) -> None:
        self._delegate._current_session_id = self._current_session_id
        self._delegate._cipher = self._cipher
        logging.info(f"[mobile_agent] Session started: {self._current_session_id}")

    async def on_session_end(self) -> None:
        for tid in list(self._delegate._terminals.keys()):
            await self._delegate.on_terminal_close(tid)
        logging.info("[mobile_agent] Session ended")

    async def on_terminal_open(self, terminal_id: str) -> bool:
        self._delegate._ws = self._ws
        self._delegate._current_session_id = self._current_session_id
        self._delegate._cipher = self._cipher
        return await self._delegate.on_terminal_open(terminal_id)

    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]:
        return await self._delegate.on_terminal_data(terminal_id, data)

    async def on_terminal_close(self, terminal_id: str) -> None:
        await self._delegate.on_terminal_close(terminal_id)

    async def on_file_list(self, path: str) -> List[dict]:
        return await self._delegate.on_file_list(path)

    async def on_file_download(self, path: str) -> Optional[bytes]:
        return await self._delegate.on_file_download(path)

    async def on_file_upload(self, path: str, data: bytes) -> bool:
        return await self._delegate.on_file_upload(path, data)


class _MobileDelegateAgent(ServerAgent):
    """Internal delegate: gives MobileAgent access to ServerAgent logic."""

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.MOBILE

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities()


# ===========================================================================
# CLI entrypoint
# ===========================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NEXUS Headless/IoT/Mobile Agent")
    parser.add_argument(
        "--type",
        choices=["server", "iot", "mobile"],
        default="server",
        help="Agent type",
    )
    parser.add_argument("--relay", required=True, help="Relay URL wss://host:port")
    parser.add_argument("--device-id", default=str(uuid.uuid4()))
    parser.add_argument("--name", default=platform.node())
    parser.add_argument("--token", required=True, help="Agent registration token")
    parser.add_argument(
        "--ghost",
        action="store_true",
        help="Suppress all console output (service mode)",
    )
    args = parser.parse_args()

    if args.ghost:
        import sys

        _null = open(os.devnull, "w")
        sys.stdout = _null
        sys.stderr = _null

    agent_cls = {"server": ServerAgent, "iot": IoTAgent, "mobile": MobileAgent}[args.type]
    agent = agent_cls(
        relay_url=args.relay,
        device_id=args.device_id,
        device_name=args.name,
        agent_token=args.token,
    )
    asyncio.run(agent.run())