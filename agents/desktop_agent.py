"""
agents/desktop_agent.py
------------------------
Desktop agent — subclasses BaseAgent (agents/base_agent.py).
Ensures screen streaming starts immediately upon ECDH session activation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Optional capability imports (graceful degradation) ────────────────────────
try:
    import mss
    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Key, Controller as KeyboardController
    _HAS_PYNPUT = True
except ImportError:
    _HAS_PYNPUT = False

# ── Internal imports ──────────────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from agents.base_agent import BaseAgent
from core.registry import DeviceCapabilities, DeviceType
from core.session import MessageType
from config.settings import settings
from utils.logger import AuditLogger, get_logger

log = get_logger("nexus.agent.desktop")
audit = AuditLogger(settings.log.audit_file)


# ── pynput special-key mapping ────────────────────────────────────────────────
_SPECIAL_KEYS: Dict[str, object] = {}
if _HAS_PYNPUT:
    _SPECIAL_KEYS = {
        "enter": Key.enter,     "return": Key.enter,
        "backspace": Key.backspace, "delete": Key.delete,
        "tab": Key.tab,         "escape": Key.esc, "esc": Key.esc,
        "space": Key.space,
        "up": Key.up,           "down": Key.down,
        "left": Key.left,       "right": Key.right,
        "home": Key.home,       "end": Key.end,
        "page_up": Key.page_up, "page_down": Key.page_down,
        "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
        "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
        "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
        "ctrl": Key.ctrl,       "alt": Key.alt,     "shift": Key.shift,
        "ctrl_l": Key.ctrl_l,   "ctrl_r": Key.ctrl_r,
        "alt_l": Key.alt_l,     "alt_r": Key.alt_r,
        "shift_l": Key.shift_l, "shift_r": Key.shift_r,
        "cmd": Key.cmd,         "win": Key.cmd,
        "caps_lock": Key.caps_lock, "insert": Key.insert,
    }


class DesktopAgent(BaseAgent):
    """
    Full-capability desktop agent: screen share, remote input, terminal,
    file transfer, clipboard. All transport/auth/dispatch is inherited
    from BaseAgent.
    """

    SCREEN_PUSH_FPS = 15

    def __init__(
        self,
        relay_url: str,
        device_id: str,
        device_name: str,
        agent_token: str,
        jpeg_quality: int = 60,
        frame_scale: float = 1.0,
    ) -> None:
        super().__init__(relay_url, device_id, device_name, agent_token)
        self.jpeg_quality = jpeg_quality
        self.frame_scale = frame_scale

        self._sct: Optional[object] = None
        self._screen_task: Optional[asyncio.Task] = None
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}

        self._mouse: Optional[object] = None
        self._keyboard: Optional[object] = None
        if _HAS_PYNPUT:
            try:
                self._mouse = MouseController()
                self._keyboard = KeyboardController()
            except Exception:
                pass

    @property
    def device_type(self) -> DeviceType:
        return DeviceType.DESKTOP

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            screen_share=_HAS_MSS and _HAS_PIL,
            remote_input=_HAS_PYNPUT,
            file_transfer=True,
            terminal=True,
            clipboard=self._has_display(),
            metrics=True,
        )

    @staticmethod
    def _has_display() -> bool:
        return (
            sys.platform in ("win32", "darwin")
            or bool(os.environ.get("DISPLAY"))
            or bool(os.environ.get("WAYLAND_DISPLAY"))
        )

    # ------------------------------------------------------------------
    # Lifecycle Hooks
    # ------------------------------------------------------------------

    async def on_session_start(self) -> None:
        """Starts active frame pushing immediately when ECDH completes."""
        log.info("desktop_agent.session_started", session_id=self._current_session_id)
        if _HAS_MSS and _HAS_PIL and self._screen_task is None:
            self._screen_task = asyncio.create_task(self._screen_push_loop())

    async def on_session_end(self) -> None:
        log.info("desktop_agent.session_ended", session_id=self._current_session_id)
        if self._screen_task:
            self._screen_task.cancel()
            try:
                await self._screen_task
            except (asyncio.CancelledError, Exception):
                pass
            self._screen_task = None
        if self._sct:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None
        for tid in list(self._terminals.keys()):
            await self.on_terminal_close(tid)

    # ------------------------------------------------------------------
    # Screen Streaming
    # ------------------------------------------------------------------

    def _capture_jpeg_using_sct(self, quality: int, scale: float) -> Optional[bytes]:
        if not (_HAS_MSS and _HAS_PIL and self._sct):
            return None
        try:
            monitor = self._sct.monitors[1]
            shot = self._sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if scale != 1.0:
                w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
                img = img.resize((w, h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
        except Exception as e:
            log.warning("desktop_agent.capture_failed", error=str(e))
            return None

    async def _screen_push_loop(self) -> None:
        self._sct = mss.mss()
        interval = 1.0 / self.SCREEN_PUSH_FPS
        loop = asyncio.get_running_loop()
        try:
            while True:
                t0 = loop.time()
                jpeg = await loop.run_in_executor(
                    None, self._capture_jpeg_using_sct, self.jpeg_quality, self.frame_scale
                )
                if jpeg:
                    await self._send_session(
                        MessageType.SCREEN_FRAME,
                        {"frame": jpeg.hex(), "timestamp": time.time()},
                    )
                await asyncio.sleep(max(0.0, interval - (loop.time() - t0)))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("desktop_agent.screen_push_error", error=str(e))

    async def on_screen_request(self) -> Optional[bytes]:
        if not (_HAS_MSS and _HAS_PIL):
            return None
        loop = asyncio.get_running_loop()
        owned = False
        sct = self._sct
        if sct is None:
            sct = mss.mss()
            owned = True
        try:
            return await loop.run_in_executor(
                None, self._capture_with_context, sct, self.jpeg_quality, self.frame_scale
            )
        finally:
            if owned:
                try:
                    sct.close()
                except Exception:
                    pass

    @staticmethod
    def _capture_with_context(sct, quality: int, scale: float) -> Optional[bytes]:
        try:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if scale != 1.0:
                w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
                img = img.resize((w, h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Input, Terminal, Files, Clipboard Handlers
    # ------------------------------------------------------------------

    async def on_mouse_event(self, payload: Dict[str, Any]) -> None:
        if not (self._mouse and _HAS_PYNPUT):
            return
        try:
            action = payload.get("action", "")
            x, y = payload.get("x"), payload.get("y")
            if x is not None and y is not None:
                self._mouse.position = (int(x), int(y))

            btn_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
            btn = btn_map.get(str(payload.get("button", "left")), Button.left)

            if action == "click":
                self._mouse.click(btn, int(payload.get("clicks", 1)))
            elif action == "press":
                self._mouse.press(btn)
            elif action == "release":
                self._mouse.release(btn)
            elif action == "scroll":
                self._mouse.scroll(int(payload.get("dx", 0)), int(payload.get("dy", 0)))
        except Exception as e:
            log.warning("desktop_agent.mouse_error", error=str(e))

    async def on_key_event(self, payload: Dict[str, Any]) -> None:
        if not (self._keyboard and _HAS_PYNPUT):
            return
        try:
            action = payload.get("action", "press")
            key_str = str(payload.get("key", ""))
            key = _SPECIAL_KEYS.get(key_str.lower())
            if key is None:
                key = key_str if len(key_str) == 1 else None
            if key is None:
                return
            if action == "press":
                self._keyboard.press(key)
            elif action == "release":
                self._keyboard.release(key)
            elif action in ("tap", "type"):
                self._keyboard.press(key)
                self._keyboard.release(key)
        except Exception as e:
            log.warning("desktop_agent.key_error", error=str(e))

    async def on_terminal_open(self, terminal_id: str) -> bool:
        shell = (
            ["powershell.exe", "-NoLogo", "-NoProfile"]
            if sys.platform == "win32"
            else [os.environ.get("SHELL", "/bin/bash")]
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *shell,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            log.error("desktop_agent.terminal_open_failed", error=str(e))
            return False

        self._terminals[terminal_id] = proc
        asyncio.create_task(self._drain_terminal(terminal_id, proc))
        log.info("desktop_agent.terminal_opened", terminal_id=terminal_id, pid=proc.pid)
        return True

    async def _drain_terminal(self, terminal_id: str, proc: asyncio.subprocess.Process) -> None:
        try:
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                await self._send_session(
                    MessageType.TERMINAL_DATA,
                    {"terminal_id": terminal_id, "data": chunk.decode("utf-8", errors="replace")},
                )
        except Exception:
            pass
        finally:
            self._terminals.pop(terminal_id, None)
            await self._send_session(MessageType.TERMINAL_CLOSE, {"terminal_id": terminal_id})
            log.info("desktop_agent.terminal_exited", terminal_id=terminal_id, rc=proc.returncode)

    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]:
        proc = self._terminals.get(terminal_id)
        if proc and proc.stdin and not proc.stdin.is_closing():
            try:
                proc.stdin.write(data.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as e:
                log.warning("desktop_agent.terminal_write_error", error=str(e))
        return None

    async def on_terminal_close(self, terminal_id: str) -> None:
        proc = self._terminals.pop(terminal_id, None)
        if not proc:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
        log.info("desktop_agent.terminal_closed", terminal_id=terminal_id)

    async def on_file_list(self, path: str) -> List[dict]:
        p = Path(path).expanduser() if path else Path.home()
        entries: List[dict] = []
        try:
            for item in sorted(p.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower())):
                try:
                    st = item.stat()
                    entries.append({
                        "name": item.name,
                        "is_dir": item.is_dir(),
                        "size": st.st_size,
                        "modified": st.st_mtime,
                    })
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass
        return entries

    async def on_file_download(self, path: str) -> Optional[bytes]:
        p = Path(path).expanduser()
        if not p.is_file():
            return None
        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > settings.agent.max_transfer_mb:
            log.warning("desktop_agent.file_too_large", path=path, size_mb=round(size_mb, 1))
            return None
        return p.read_bytes()

    async def on_file_upload(self, path: str, data: bytes) -> bool:
        try:
            p = Path(path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            audit.file_transfer(
                session_id=self._current_session_id or "unknown",
                direction="upload", filename=p.name, size_bytes=len(data),
            )
            return True
        except Exception as e:
            log.error("desktop_agent.file_upload_error", error=str(e))
            return False

    async def on_clipboard_get(self) -> Optional[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._read_clipboard)

    async def on_clipboard_set(self, text: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_clipboard, text)

    @staticmethod
    def _read_clipboard() -> str:
        try:
            if sys.platform == "win32":
                import subprocess
                r = subprocess.run(
                    ["powershell", "-NonInteractive", "-Command",
                     "[System.Windows.Forms.Clipboard]::GetText()"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.stdout.rstrip("\r\n")
            if sys.platform == "darwin":
                import subprocess
                r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
                return r.stdout
            import subprocess
            for cmd in (["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                    if r.returncode == 0:
                        return r.stdout
                except FileNotFoundError:
                    continue
        except Exception:
            pass
        return ""

    @staticmethod
    def _write_clipboard(text: str) -> None:
        try:
            if sys.platform == "win32":
                import subprocess
                proc = subprocess.Popen(
                    ["powershell", "-NonInteractive", "-Command", "$input | Set-Clipboard"],
                    stdin=subprocess.PIPE,
                )
                proc.communicate(input=text.encode("utf-8"))
                return
            if sys.platform == "darwin":
                import subprocess
                proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"))
                return
            import subprocess
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
                try:
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    proc.communicate(text.encode("utf-8"))
                    return
                except FileNotFoundError:
                    continue
        except Exception:
            pass


def main() -> None:
    import argparse
    import hashlib
    import socket

    parser = argparse.ArgumentParser(description="NEXUS Desktop Agent")
    parser.add_argument("--relay", required=True, help="ws[s]://host:port")
    parser.add_argument("--token", required=True, help="Agent auth token")
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--ghost", action="store_true", help="Suppress console output")
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()

    if args.ghost:
        _null = open(os.devnull, "w")
        sys.stdout = _null
        sys.stderr = _null
    else:
        logging.basicConfig(level=logging.INFO)

    hostname = socket.gethostname()
    username = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    raw = f"{hostname}-{username}".lower().replace(" ", "-")
    short = hashlib.md5(raw.encode()).hexdigest()[:6]

    agent = DesktopAgent(
        relay_url=args.relay,
        device_id=args.device_id or f"{raw[:15]}-{short}",
        device_name=args.name or f"{hostname} (Agent)",
        agent_token=args.token,
        jpeg_quality=args.quality,
        frame_scale=args.scale,
    )

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()