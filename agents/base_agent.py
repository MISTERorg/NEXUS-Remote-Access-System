"""
agents/base_agent.py
--------------------
Abstract base class for all NEXUS agents.

Improvements over v1:
  - on_session_start() / on_session_end() lifecycle hooks for subclasses
  - Native WebSocket ping frames (replaces manual "ping" text messages)
  - Cleaner reconnect loop with exponential back-off cap
  - _send_session() guards against closed socket mid-session
  - Ghost-mode aware: no print(), uses logging only
  - Modern websockets.asyncio.client import
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import ssl
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import psutil
import websockets
from websockets.asyncio.client import ClientConnection

from config.settings import settings
from core.registry import DeviceCapabilities, DeviceType
from core.session import MessageType, SessionMessage
from utils.crypto import AESGCMCipher, ECDHKeyExchange
from utils.logger import get_logger

log = get_logger("nexus.agent")


class BaseAgent(ABC):
    """
    Abstract base agent.

    Subclasses MUST implement:
        device_type         -> DeviceType  (property)
        get_capabilities()  -> DeviceCapabilities

    Subclasses SHOULD override (all are no-ops by default):
        on_session_start()          called once ECDH completes → session ACTIVE
        on_session_end()            called on any session close
        on_screen_request()         pull-mode single frame
        on_mouse_event(payload)
        on_key_event(payload)
        on_terminal_open(tid)       returns bool
        on_terminal_data(tid, data) returns Optional[bytes]
        on_terminal_close(tid)
        on_file_list(path)          returns list[dict]
        on_file_download(path)      returns Optional[bytes]
        on_file_upload(path, data)  returns bool
        on_clipboard_get()          returns Optional[str]
        on_clipboard_set(text)
    """

    def __init__(
        self,
        relay_url: str,
        device_id: str,
        device_name: str,
        agent_token: str,
    ):
        self.relay_url = relay_url
        self.device_id = device_id
        self.device_name = device_name
        self.agent_token = agent_token

        self._ws: Optional[ClientConnection] = None
        self._running = False
        self._current_session_id: Optional[str] = None
        self._cipher: Optional[AESGCMCipher] = None
        self._ecdh: Optional[ECDHKeyExchange] = None

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def device_type(self) -> DeviceType: ...

    @abstractmethod
    def get_capabilities(self) -> DeviceCapabilities: ...

    # Lifecycle hooks
    async def on_session_start(self) -> None: pass
    async def on_session_end(self) -> None: pass

    # Capability handlers (all optional)
    async def on_screen_request(self) -> Optional[bytes]: return None
    async def on_mouse_event(self, payload: Dict[str, Any]) -> None: pass
    async def on_key_event(self, payload: Dict[str, Any]) -> None: pass
    async def on_terminal_open(self, terminal_id: str) -> bool: return False
    async def on_terminal_data(self, terminal_id: str, data: str) -> Optional[bytes]: return None
    async def on_terminal_close(self, terminal_id: str) -> None: pass
    async def on_file_list(self, path: str) -> list: return []
    async def on_file_download(self, path: str) -> Optional[bytes]: return None
    async def on_file_upload(self, path: str, data: bytes) -> bool: return False
    async def on_clipboard_get(self) -> Optional[str]: return None
    async def on_clipboard_set(self, text: str) -> None: pass

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main entry point. Connects and reconnects with exponential back-off."""
        self._running = True
        retries = 0
        max_delay = 120  # cap back-off at 2 minutes

        while self._running and retries < settings.agent.reconnect_max_retries:
            try:
                ssl_ctx = self._build_ssl_context()
                logging.info(f"[agent] Connecting to {self.relay_url} (attempt {retries + 1})")

                async with websockets.connect(
                    self.relay_url,
                    ssl=ssl_ctx,
                    ping_interval=None,   # we manage pings ourselves
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    retries = 0           # reset on successful connect
                    await self._authenticate()
                    await asyncio.gather(
                        self._receive_loop(),
                        self._heartbeat_loop(),
                        self._metrics_loop(),
                    )

            except websockets.exceptions.ConnectionClosed as e:
                logging.warning(f"[agent] Disconnected: code={e.code} reason={e.reason}")
            except (OSError, websockets.exceptions.WebSocketException) as e:
                logging.error(f"[agent] Connection error: {e}")
            except Exception as e:
                logging.error(f"[agent] Unexpected error: {e}")
            finally:
                self._ws = None

            if self._running:
                retries += 1
                delay = min(settings.agent.reconnect_delay * (2 ** (retries - 1)), max_delay)
                logging.info(f"[agent] Reconnecting in {delay:.0f}s (attempt {retries})")
                await asyncio.sleep(delay)

        logging.info("[agent] Stopped.")

    def stop(self) -> None:
        self._running = False

    def _build_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ca = settings.tls.ca_file
        if ca and ca.exists():
            ctx.load_verify_locations(cafile=str(ca))
        else:
            # Dev fallback: skip verification
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            logging.warning("[agent] TLS: CA file not found — certificate verification disabled")

        if settings.tls.require_client_cert:
            cert, key = settings.tls.cert_file, settings.tls.key_file
            if cert.exists() and key.exists():
                ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        return ctx

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        caps = self.get_capabilities()
        auth_msg = {
            "type": "auth.agent",
            "device_id": self.device_id,
            "name": self.device_name,
            "device_type": self.device_type.value,
            "token": self.agent_token,
            "capabilities": caps.model_dump(),
            "metadata": self._collect_sys_info(),
        }
        await self._ws.send(json.dumps(auth_msg))
        resp = json.loads(await self._ws.recv())
        if resp.get("type") != "auth.ok":
            raise RuntimeError(f"Agent auth rejected: {resp}")
        logging.info(f"[agent] Authenticated as {self.device_id}")

    def _collect_sys_info(self) -> Dict[str, str]:
        return {
            "os": platform.system(),
            "os_version": platform.version(),
            "hostname": platform.node(),
            "python": platform.python_version(),
            "arch": platform.machine(),
        }

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        async for raw in self._ws:
            try:
                if isinstance(raw, bytes):
                    await self._handle_binary(raw)
                else:
                    await self._handle_text(json.loads(raw))
            except json.JSONDecodeError:
                logging.warning("[agent] Received non-JSON text frame")
            except Exception as e:
                logging.error(f"[agent] Dispatch error: {e}")

    async def _handle_text(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "session.request":
            await self._handle_session_request(msg)
        elif mtype == "error":
            logging.warning(f"[agent] Relay error: {msg.get('code')} — {msg.get('message')}")
        # "pong" text frames are handled implicitly by record_pong; ignore here

    async def _handle_binary(self, data: bytes) -> None:
        """Decrypt and dispatch an encrypted session frame."""
        if not self._cipher or not self._current_session_id:
            return
        try:
            plain = self._cipher.decrypt(data, aad=self._current_session_id.encode())
            msg = SessionMessage.model_validate_json(plain)
            await self._dispatch_session_msg(msg)
        except Exception as e:
            logging.error(f"[agent] Decrypt/dispatch error: {e}")

    # ------------------------------------------------------------------
    # Session handshake
    # ------------------------------------------------------------------

    async def _handle_session_request(self, msg: dict) -> None:
        session_id = msg.get("session_id")
        controller_ecdh_hex = msg.get("ecdh_key")
        logging.info(f"[agent] Session request: {session_id}")

        self._ecdh = ECDHKeyExchange()
        self._current_session_id = session_id

        try:
            controller_key = bytes.fromhex(controller_ecdh_hex)
            shared_key = self._ecdh.derive_shared_key(
                controller_key,
                info=f"nexus-session-{session_id}".encode(),
            )
            self._cipher = AESGCMCipher(shared_key)
        except Exception as e:
            logging.error(f"[agent] ECDH failed: {e}")
            await self._ws.send(json.dumps({
                "type": "session.reject",
                "session_id": session_id,
                "reason": "ecdh_failed",
            }))
            self._current_session_id = None
            return

        await self._ws.send(json.dumps({
            "type": "session.accept",
            "session_id": session_id,
            "ecdh_key": self._ecdh.public_key_bytes().hex(),
        }))
        logging.info(f"[agent] Session accepted: {session_id}")

        # Fire lifecycle hook
        await self.on_session_start()

    # ------------------------------------------------------------------
    # Session message dispatch
    # ------------------------------------------------------------------

    async def _dispatch_session_msg(self, msg: SessionMessage) -> None:
        t = msg.type

        if t == MessageType.PING:
            await self._send_session(MessageType.PONG, None)

        elif t == MessageType.SCREEN_REQUEST:
            frame = await self.on_screen_request()
            if frame:
                await self._send_session(MessageType.SCREEN_FRAME, {"frame": frame.hex()})

        elif t == MessageType.MOUSE_EVENT:
            await self.on_mouse_event(msg.payload or {})

        elif t == MessageType.KEY_EVENT:
            await self.on_key_event(msg.payload or {})

        elif t == MessageType.TERMINAL_OPEN:
            tid = (msg.payload or {}).get("terminal_id", str(uuid.uuid4()))
            ok = await self.on_terminal_open(tid)
            await self._send_session(MessageType.TERMINAL_DATA, {"terminal_id": tid, "ok": ok})

        elif t == MessageType.TERMINAL_DATA:
            p = msg.payload or {}
            tid = p.get("terminal_id")
            data = p.get("data", "")
            output = await self.on_terminal_data(tid, data)
            if output:
                await self._send_session(
                    MessageType.TERMINAL_DATA,
                    {"terminal_id": tid, "data": output.decode("utf-8", errors="replace")},
                )

        elif t == MessageType.TERMINAL_CLOSE:
            tid = (msg.payload or {}).get("terminal_id")
            await self.on_terminal_close(tid)

        elif t == MessageType.FILE_LIST:
            path = (msg.payload or {}).get("path", "~")
            entries = await self.on_file_list(path)
            await self._send_session(MessageType.FILE_LIST, {"path": path, "entries": entries})

        elif t == MessageType.FILE_DOWNLOAD_START:
            path = (msg.payload or {}).get("path")
            data = await self.on_file_download(path)
            if data:
                chunk_size = 65536
                total = len(data)
                for i in range(0, total, chunk_size):
                    chunk = data[i: i + chunk_size]
                    await self._send_session(
                        MessageType.FILE_DOWNLOAD_CHUNK,
                        {
                            "data": chunk.hex(),
                            "offset": i,
                            "total": total,
                            "done": (i + chunk_size) >= total,
                        },
                    )

        elif t == MessageType.FILE_UPLOAD_END:
            p = msg.payload or {}
            path = p.get("path")
            data_hex = p.get("data", "")
            ok = await self.on_file_upload(path, bytes.fromhex(data_hex))
            await self._send_session(MessageType.FILE_UPLOAD_END, {"ok": ok, "path": path})

        elif t == MessageType.CLIPBOARD_GET:
            text = await self.on_clipboard_get()
            await self._send_session(MessageType.CLIPBOARD_GET, {"text": text or ""})

        elif t == MessageType.CLIPBOARD_SET:
            await self.on_clipboard_set((msg.payload or {}).get("text", ""))

        elif t == MessageType.CLOSE:
            logging.info(f"[agent] Session closed by controller: {msg.session_id}")
            await self._close_session()

    async def _close_session(self) -> None:
        """Clean up session state and fire on_session_end()."""
        self._current_session_id = None
        self._cipher = None
        self._ecdh = None
        await self.on_session_end()

    # ------------------------------------------------------------------
    # Outbound helper
    # ------------------------------------------------------------------

    async def _send_session(self, msg_type: MessageType, payload: Any) -> None:
        """Encrypt and send a session frame. Safe to call even if ws is closing."""
        if not self._ws or not self._cipher or not self._current_session_id:
            return
        try:
            msg = SessionMessage(
                type=msg_type,
                session_id=self._current_session_id,
                payload=payload,
            )
            encrypted = self._cipher.encrypt(
                msg.model_dump_json().encode(),
                aad=self._current_session_id.encode(),
            )
            # Prepend session_id (36 bytes) for relay routing
            framed = self._current_session_id.encode() + encrypted
            await self._ws.send(framed)
        except websockets.exceptions.ConnectionClosed:
            logging.warning("[agent] Cannot send — connection closed")
        except Exception as e:
            logging.error(f"[agent] Send error: {e}")

    # ------------------------------------------------------------------
    # Heartbeat — uses native WebSocket ping frames
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = settings.relay.heartbeat_interval
        while self._running and self._ws:
            try:
                await self._ws.ping()
                await asyncio.sleep(interval)
            except Exception:
                break

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def _metrics_loop(self) -> None:
        while self._running and self._ws:
            try:
                metrics = self._collect_metrics()
                await self._ws.send(json.dumps({"type": "device.metrics", "metrics": metrics}))
                await asyncio.sleep(30)
            except Exception:
                break

    def _collect_metrics(self) -> Dict[str, float]:
        try:
            net = psutil.net_io_counters()
            return {
                "cpu_percent": psutil.cpu_percent(interval=0.5),
                "ram_percent": psutil.virtual_memory().percent,
                "disk_percent": psutil.disk_usage("/").percent,
                "uptime_seconds": time.time() - psutil.boot_time(),
                "network_rx_bps": float(net.bytes_recv),
                "network_tx_bps": float(net.bytes_sent),
            }
        except Exception:
            return {}
