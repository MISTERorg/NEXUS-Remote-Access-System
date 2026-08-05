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
        self._shared_key: Optional[bytes] = None
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
    # AV stubs
    async def on_camera_start(self, payload: Dict[str, Any]) -> None: pass
    async def on_camera_stop(self, payload: Dict[str, Any]) -> None: pass
    async def on_camera_list(self, payload: Dict[str, Any]) -> None: pass
    async def on_camera_snapshot(self, payload: Dict[str, Any]) -> None: pass
    async def on_net_probe_ack(self, payload: Dict[str, Any]) -> None: pass
    async def on_p2p_endpoint_offer(self, payload: Dict[str, Any]) -> None: pass
    async def on_p2p_status(self, payload: Dict[str, Any]) -> None: pass
    async def on_audio_start(self, payload: Dict[str, Any]) -> None: pass
    async def on_audio_stop(self, payload: Dict[str, Any]) -> None: pass
    async def on_audio_data(self, payload: Dict[str, Any]) -> None: pass

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
        elif mtype == "session.close":
            # Sent by the relay (core/relay.py _handle_session_close) when the
            # controller ends the session, or when it force-closes one for any
            # other reason. Without handling this, the agent never learns the
            # session ended: on_session_end() would not fire, so subclasses
            # like DesktopAgent never stop their screen-push loop or close
            # open terminal subprocesses — they'd keep running against a
            # session the relay already tore down.
            session_id = msg.get("session_id")
            if session_id and session_id != self._current_session_id:
                # Stale/mismatched close for a session we're not currently in
                # (e.g. a delayed close arriving after we've already moved on
                # to a new session) — ignore rather than tear down the wrong one.
                logging.debug(
                    f"[agent] Ignoring session.close for {session_id}, "
                    f"current session is {self._current_session_id}"
                )
                return
            logging.info(f"[agent] Session closed by relay: {session_id}")
            await self._close_session()
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
            self._shared_key = shared_key  # kept for the P2P punch token — see
                                            # _p2p_phase1_discover() below
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

        # P2P Phase 1 (best-effort, background, never blocking): discover
        # our own public UDP endpoint via the relay's rendezvous listener
        # and report it. Wrapped so ANY failure here — timeout, network
        # oddity, whatever — can never affect the session that's already
        # active and working over the relay. See
        # transport/hole_punch.py's module docstring for exactly what
        # this does and doesn't enable today (short version: real,
        # tested, but the browser controller has no peer address to
        # punch toward yet, so this is discovery+reporting only, not a
        # live punch attempt).
        asyncio.create_task(self._p2p_phase1_discover(session_id))

    async def _p2p_phase1_discover(self, session_id: str) -> None:
        if not self._shared_key or not self.relay_url:
            return
        try:
            from urllib.parse import urlparse
            from transport.hole_punch import (
                make_probe_token, open_punch_socket, discover_public_endpoint,
            )

            parsed = urlparse(self.relay_url)
            relay_host = parsed.hostname
            # Matches config/settings.py's punch_port default (0 = reuse
            # the main relay port for UDP too) — if the relay operator set
            # a non-default punch_port, this won't find it; that's a known
            # limitation of inferring it purely from relay_url rather than
            # the agent being told explicitly, not a bug in the discovery
            # logic itself.
            relay_port = parsed.port or 7000
            if not relay_host:
                return

            token = make_probe_token(self._shared_key, session_id)
            transport, protocol, local_port = await open_punch_socket()
            try:
                endpoint = await discover_public_endpoint(
                    transport, protocol, relay_host, relay_port, token, timeout=5.0
                )
            finally:
                transport.close()

            if endpoint:
                logging.info(f"[agent] P2P endpoint discovered: {endpoint}")
                await self._send_session(
                    MessageType.P2P_ENDPOINT_OFFER,
                    {"endpoint": list(endpoint)},
                )
            else:
                logging.debug("[agent] P2P endpoint discovery: no response (non-fatal)")
        except Exception as e:
            # Deliberately swallow everything here — this is a best-effort
            # side channel, never allowed to affect the real session.
            logging.debug(f"[agent] P2P phase 1 discovery skipped: {e}")

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

        elif t == MessageType.FILE_UPLOAD_START:
            # Initialise an in-memory buffer for this transfer.  The transfer_id
            # lets multiple uploads run in parallel without their chunks
            # interleaving (the frontend only starts one at a time today, but
            # we're protocol-correct about it regardless).
            p = msg.payload or {}
            tid  = p.get("transfer_id", "default")
            path = p.get("path")
            if not hasattr(self, "_upload_buffers"):
                self._upload_buffers: Dict[str, Dict] = {}
            self._upload_buffers[tid] = {"path": path, "chunks": {}}

        elif t == MessageType.FILE_UPLOAD_CHUNK:
            p = msg.payload or {}
            tid   = p.get("transfer_id", "default")
            index = p.get("index", 0)
            b64   = p.get("data", "")
            if not hasattr(self, "_upload_buffers"):
                self._upload_buffers = {}
            buf = self._upload_buffers.get(tid)
            if buf and b64:
                try:
                    import base64
                    buf["chunks"][index] = base64.b64decode(b64)
                except Exception as e:
                    log.error("file_upload_chunk.decode_error", error=str(e))

        elif t == MessageType.FILE_UPLOAD_END:
            p = msg.payload or {}
            tid = p.get("transfer_id", "default")
            if not hasattr(self, "_upload_buffers"):
                self._upload_buffers = {}
            buf = self._upload_buffers.pop(tid, None)
            success = False
            path = None
            if buf:
                path = buf.get("path")
                # Reassemble chunks in index order
                ordered = b"".join(
                    buf["chunks"][k] for k in sorted(buf["chunks"])
                )
                success = await self.on_file_upload(path, ordered)
            await self._send_session(
                MessageType.FILE_UPLOAD_END,
                {"ok": success, "path": path, "transfer_id": tid},
            )

        elif t == MessageType.FILE_DOWNLOAD_START:
            p = msg.payload or {}
            path = p.get("path")
            data = await self.on_file_download(path)
            if data is None:
                await self._send_session(
                    MessageType.FILE_DOWNLOAD_CHUNK,
                    {"path": path, "error": "File not found or access denied", "done": True},
                )
            else:
                chunk_size = 65536  # 64 KB per chunk
                total = len(data)
                chunks = range(0, total, chunk_size) if total else [0]
                for i, offset in enumerate(chunks):
                    chunk = data[offset: offset + chunk_size]
                    is_done = (offset + chunk_size) >= total
                    await self._send_session(
                        MessageType.FILE_DOWNLOAD_CHUNK,
                        {
                            "path": path,
                            "data": chunk.hex(),
                            "offset": offset,
                            "total": max(total, 1),
                            "done": is_done,
                        },
                    )

        elif t == MessageType.CLIPBOARD_GET:
            text = await self.on_clipboard_get()
            await self._send_session(MessageType.CLIPBOARD_GET, {"text": text or ""})

        elif t == MessageType.CLIPBOARD_SET:
            await self.on_clipboard_set((msg.payload or {}).get("text", ""))

        elif t == MessageType.CAMERA_START:
            await self.on_camera_start(msg.payload or {})

        elif t == MessageType.CAMERA_STOP:
            await self.on_camera_stop(msg.payload or {})

        elif t == MessageType.CAMERA_LIST:
            # NOTE: this case was missing entirely — on_camera_list() existed
            # as a hook and could build+send a response, but nothing ever
            # called it, so a controller's camera_list request was silently
            # dropped no matter what the agent implementation did.
            await self.on_camera_list(msg.payload or {})

        elif t == MessageType.CAMERA_SNAPSHOT:
            await self.on_camera_snapshot(msg.payload or {})

        elif t == MessageType.NET_PROBE_ACK:
            await self.on_net_probe_ack(msg.payload or {})

        elif t == MessageType.P2P_ENDPOINT_OFFER:
            await self.on_p2p_endpoint_offer(msg.payload or {})

        elif t == MessageType.P2P_STATUS:
            await self.on_p2p_status(msg.payload or {})

        elif t == MessageType.AUDIO_START:
            await self.on_audio_start(msg.payload or {})

        elif t == MessageType.AUDIO_STOP:
            await self.on_audio_stop(msg.payload or {})

        elif t == MessageType.AUDIO_DATA:
            await self.on_audio_data(msg.payload or {})

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