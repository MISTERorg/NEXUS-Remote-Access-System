"""
core/session.py
---------------
Remote session lifecycle management.

A Session represents an active connection between a controller (human operator)
and a target device (agent). It coordinates:
  - Encrypted channel setup (ECDH key exchange)
  - Permission enforcement
  - Frame/message routing between controller <-> agent
  - Session recording (optional)
  - Clean teardown and audit logging
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from pydantic import BaseModel, Field

from core.auth import Role, has_permission
from utils.crypto import AESGCMCipher, ECDHKeyExchange
from utils.logger import AuditLogger, get_logger
from config.settings import settings

log = get_logger("nexus.session")
audit = AuditLogger(settings.log.audit_file)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SessionState(str, Enum):
    PENDING = "pending"         # Created, awaiting agent acceptance
    HANDSHAKING = "handshaking" # ECDH in progress
    ACTIVE = "active"           # Fully established
    PAUSED = "paused"           # Temporarily suspended
    CLOSING = "closing"
    CLOSED = "closed"


class MessageType(str, Enum):
    # Handshake
    ECDH_OFFER = "ecdh_offer"
    ECDH_ANSWER = "ecdh_answer"
    # Control
    PING = "ping"
    PONG = "pong"
    CLOSE = "close"
    # Screen
    SCREEN_FRAME = "screen_frame"
    SCREEN_REQUEST = "screen_request"
    # Input
    MOUSE_EVENT = "mouse_event"
    KEY_EVENT = "key_event"
    # Terminal
    TERMINAL_OPEN = "terminal_open"
    TERMINAL_DATA = "terminal_data"
    TERMINAL_CLOSE = "terminal_close"
    # File
    FILE_LIST = "file_list"
    FILE_UPLOAD_START = "file_upload_start"
    FILE_UPLOAD_CHUNK = "file_upload_chunk"
    FILE_UPLOAD_END = "file_upload_end"
    FILE_DOWNLOAD_START = "file_download_start"
    FILE_DOWNLOAD_CHUNK = "file_download_chunk"
    # Clipboard
    CLIPBOARD_GET = "clipboard_get"
    CLIPBOARD_SET = "clipboard_set"
    # Metrics
    METRICS_UPDATE = "metrics_update"
    # Error
    ERROR = "error"


class SessionMessage(BaseModel):
    type: MessageType
    session_id: str
    payload: Any = None
    encrypted: bool = False
    timestamp: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class Session:
    """
    Represents one remote access session.

    Typical flow:
      1. Controller requests session → PENDING
      2. Agent accepts → HANDSHAKING
      3. ECDH key exchange completes → ACTIVE
      4. Data flows bidirectionally (encrypted)
      5. Either side closes → CLOSING → CLOSED
    """

    def __init__(
        self,
        session_id: str,
        controller_id: str,       # user_id of the human operator
        device_id: str,
        controller_role: Role,
    ):
        self.session_id = session_id
        self.controller_id = controller_id
        self.device_id = device_id
        self.controller_role = controller_role
        self.state = SessionState.PENDING
        self.created_at = time.time()
        self.activated_at: Optional[float] = None
        self.closed_at: Optional[float] = None

        # Crypto
        self._ecdh = ECDHKeyExchange()
        self._cipher: Optional[AESGCMCipher] = None

        # Message routing
        self._controller_send: Optional[Callable] = None
        self._agent_send: Optional[Callable] = None
        self._message_handlers: Dict[MessageType, Callable] = {}

        # Terminals
        self._terminals: Dict[str, asyncio.subprocess.Process] = {}

        # Stats
        self.bytes_sent = 0
        self.bytes_received = 0
        self.frames_sent = 0

    # ------------------------------------------------------------------
    # Connection wiring
    # ------------------------------------------------------------------

    def attach_controller(self, send_fn: Callable[[dict], Coroutine]) -> None:
        """
        send_fn delivers a PLAINTEXT dict (JSON) to the controller's WebSocket.

        This is intentionally NOT the same contract as attach_agent(). The
        controller (browser) is never a party to this session's ECDH
        handshake — Session.__init__ creates its own ephemeral keypair and
        completes the handshake directly against the AGENT (see
        complete_handshake() below and relay.py's _handle_session_accept).
        The browser has no way to derive that shared key, so sending it
        AES-GCM ciphertext is undecryptable by design, not by bug — it
        would just be silently dropped on arrival. The controller leg is
        instead protected by the outer wss:// TLS transport, matching the
        agent leg's own encryption for the relay<->agent hop.
        """
        self._controller_send = send_fn

    def attach_agent(self, send_fn: Callable[[bytes], Coroutine]) -> None:
        self._agent_send = send_fn

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def get_ecdh_offer(self) -> bytes:
        """Return our ECDH public key bytes for the handshake offer."""
        self.state = SessionState.HANDSHAKING
        return self._ecdh.public_key_bytes()

    def complete_handshake(self, peer_public_key_bytes: bytes) -> None:
        """Derive shared session key from peer's ECDH public key."""
        shared_key = self._ecdh.derive_shared_key(
            peer_public_key_bytes,
            info=f"nexus-session-{self.session_id}".encode(),
        )
        self._cipher = AESGCMCipher(shared_key)
        self.state = SessionState.ACTIVE
        self.activated_at = time.time()
        log.info("session.activated", session_id=self.session_id, device_id=self.device_id)
        audit.session_opened(self.session_id, self.controller_id, self.device_id)

    # ------------------------------------------------------------------
    # Message routing
    # ------------------------------------------------------------------

    async def handle_from_controller_plaintext(self, msg_dict: dict) -> None:
        """
        Controller → agent path.

        The browser sends plain JSON — {"type": ..., "session_id": ...,
        "payload": {...}} — matching MessageType values directly (see
        dashboard.html's sendSessionMsg()). This builds the SessionMessage
        from that dict and hands it to _route_to_agent(), which is the
        part that actually encrypts it before it goes anywhere near the
        agent's WebSocket. Nothing arrives here already encrypted, because
        nothing on the browser side is capable of producing that ciphertext
        (see attach_controller's docstring for why).
        """
        if self.state != SessionState.ACTIVE:
            return
        try:
            msg = SessionMessage(
                type=msg_dict.get("type"),
                session_id=self.session_id,
                payload=msg_dict.get("payload"),
            )
        except Exception as e:
            log.warning("session.bad_controller_message", session_id=self.session_id, error=str(e))
            return
        self.bytes_received += len(json.dumps(msg_dict))
        await self._route_to_agent(msg, b"")

    async def handle_from_agent(self, raw: bytes) -> None:
        """Agent → controller path."""
        if self.state not in (SessionState.ACTIVE, SessionState.HANDSHAKING):
            return
        msg = self._decrypt_message(raw)
        self.bytes_received += len(raw)
        await self._route_to_controller(msg, raw)

    async def _route_to_agent(self, msg: SessionMessage, raw: bytes) -> None:
        """Forward message to agent after permission check."""
        perm_map = {
            MessageType.SCREEN_REQUEST: "screen.view",
            MessageType.MOUSE_EVENT: "screen.control",
            MessageType.KEY_EVENT: "screen.control",
            MessageType.TERMINAL_OPEN: "terminal.open",
            MessageType.TERMINAL_DATA: "terminal.open",
            MessageType.FILE_LIST: "file.download",
            MessageType.FILE_UPLOAD_START: "file.upload",
            MessageType.FILE_DOWNLOAD_START: "file.download",
            MessageType.CLIPBOARD_GET: "clipboard",
            MessageType.CLIPBOARD_SET: "clipboard",
        }
        required = perm_map.get(msg.type)
        if required and not has_permission(self.controller_role, required):
            log.warning("session.permission_denied",
                        session_id=self.session_id,
                        msg_type=msg.type,
                        required=required)
            return

        if self._agent_send:
            encrypted = self._encrypt_message(msg)
            await self._agent_send(encrypted)
            self.bytes_sent += len(encrypted)

    async def _route_to_controller(self, msg: SessionMessage, raw: bytes) -> None:
        """
        Agent → controller path. `msg` has already been decrypted (by
        handle_from_agent, using the session's real agent-side cipher) by
        the time it reaches here. It is forwarded to the browser as plain
        JSON — see attach_controller()'s docstring for why re-encrypting
        it would just produce ciphertext the browser can never open.
        """
        if self._controller_send:
            data = msg.model_dump(mode="json")
            await self._controller_send(data)
            self.bytes_sent += len(json.dumps(data))
            if msg.type == MessageType.SCREEN_FRAME:
                self.frames_sent += 1

    # ------------------------------------------------------------------
    # Crypto helpers
    # ------------------------------------------------------------------

    def _encrypt_message(self, msg: SessionMessage) -> bytes:
        import json
        data = msg.model_dump_json().encode()
        if self._cipher:
            return self._cipher.encrypt(data, aad=self.session_id.encode())
        return data

    def _decrypt_message(self, raw: bytes) -> SessionMessage:
        import json
        data = raw
        if self._cipher:
            data = self._cipher.decrypt(raw, aad=self.session_id.encode())
        return SessionMessage.model_validate_json(data)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    async def close(self, reason: str = "normal") -> None:
        if self.state in (SessionState.CLOSING, SessionState.CLOSED):
            return
        self.state = SessionState.CLOSING

        # Terminate any open terminals
        for proc in self._terminals.values():
            try:
                proc.terminate()
            except Exception:
                pass

        self.state = SessionState.CLOSED
        self.closed_at = time.time()
        duration = (self.closed_at - (self.activated_at or self.created_at))
        audit.session_closed(self.session_id, duration_s=duration, reason=reason)
        log.info("session.closed",
                 session_id=self.session_id,
                 duration_s=round(duration, 2),
                 reason=reason)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def duration(self) -> float:
        end = self.closed_at or time.time()
        return end - self.created_at

    def summary(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "controller_id": self.controller_id,
            "device_id": self.device_id,
            "state": self.state,
            "duration_s": round(self.duration, 2),
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "frames_sent": self.frames_sent,
        }


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

class SessionManager:
    """
    Creates, tracks, and cleans up sessions.
    Thread-safe via asyncio lock.
    """

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._close_listeners: List[Callable[[Session, str], Coroutine]] = []

    def on_close(self, callback: Callable[[Session, str], Coroutine]) -> None:
        """
        Register a callback fired whenever a session closes, regardless of
        which code path triggered it — relay.py's own WS session.close
        handler, an idle-session sweep, an agent disconnecting (cascading
        via close_all_for_device), or an external caller such as the
        dashboard's DELETE /sessions/{id} REST endpoint.

        This is the single place responsible for notifying the controller
        and agent WebSocket connections that a session ended. Callers of
        close() don't need direct access to relay.py's connection registry
        to trigger that notification — relay.py registers itself as a
        listener here instead (see RelayServer.__init__).
        """
        self._close_listeners.append(callback)

    async def _notify_close(self, session: Session, reason: str) -> None:
        for cb in self._close_listeners:
            try:
                await cb(session, reason)
            except Exception as e:
                log.error("session_manager.close_listener_error", error=str(e))

    async def create(
        self,
        controller_id: str,
        device_id: str,
        controller_role: Role,
    ) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            controller_id=controller_id,
            device_id=device_id,
            controller_role=controller_role,
        )
        async with self._lock:
            self._sessions[session_id] = session
        log.info("session.created", session_id=session_id,
                 controller_id=controller_id, device_id=device_id)
        return session

    async def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    async def close(self, session_id: str, reason: str = "normal") -> None:
        session = self._sessions.get(session_id)
        if session:
            await session.close(reason=reason)
            await self._notify_close(session, reason)
            async with self._lock:
                self._sessions.pop(session_id, None)

    async def close_all_for_device(self, device_id: str) -> None:
        to_close = [s for s in self._sessions.values()
                    if s.device_id == device_id]
        for s in to_close:
            await self.close(s.session_id, reason="device_disconnected")

    async def list_active(self) -> List[Session]:
        return [s for s in self._sessions.values()
                if s.state == SessionState.ACTIVE]

    async def sweep_expired(self, timeout_s: int = 3600) -> int:
        expired = [s for s in self._sessions.values()
                   if s.duration > timeout_s]
        for s in expired:
            await self.close(s.session_id, reason="timeout")
        return len(expired)


# Module-level singleton
session_manager = SessionManager()