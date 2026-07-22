"""
core/relay.py
-------------
NEXUS Relay Server — the central WebSocket hub.

Improvements over v1:
  - Uses websockets.asyncio.server.ServerConnection (modern API)
  - WSServer from transport layer replaces raw websockets.serve()
  - Rate limiting on login and message dispatch (RateLimiter)
  - Stale-device sweep task runs on a background asyncio loop
  - Session expiry sweep runs in background
  - Structured logging throughout with conn_id context
  - Relay metrics endpoint: GET /relay/stats via shared state dict
  - Binary frame routing now validates session ownership before forwarding
  - _on_peer_dead properly awaits graceful WS close with timeout
  - RelayStats dataclass for observability

Start with:
    python -m core.relay --host 0.0.0.0 --port 7000
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import websockets
from websockets.asyncio.server import ServerConnection

from config.settings import settings
from core.auth import AuthError, Role, auth_service
from core.registry import (
    DeviceCapabilities,
    DeviceInfo,
    DeviceStatus,
    DeviceType,
    device_registry,
)
from core.session import SessionState, session_manager
from transport.websocket_transport import WSServer
from utils.heartbeat import HeartbeatManager, RateLimiter
from utils.logger import get_logger

log = get_logger("nexus.relay")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class RelayStats:
    started_at: float = field(default_factory=time.time)
    total_connections: int = 0
    total_sessions: int = 0
    total_bytes_relayed: int = 0
    active_connections: int = 0
    active_sessions: int = 0

    def to_dict(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "total_connections": self.total_connections,
            "total_sessions": self.total_sessions,
            "total_bytes_relayed": self.total_bytes_relayed,
            "active_connections": self.active_connections,
            "active_sessions": self.active_sessions,
        }


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class Connection:
    """Wraps a ServerConnection with identity and state."""

    def __init__(self, ws: ServerConnection):
        self.ws = ws
        self.conn_id = str(uuid.uuid4())
        self.identity_type: Optional[str] = None   # "controller" | "agent"
        self.user_id: Optional[str] = None
        self.device_id: Optional[str] = None
        self.role: Optional[Role] = None
        self.authenticated = False
        self.connected_at = time.time()

    async def send_json(self, data: dict) -> None:
        try:
            await self.ws.send(json.dumps(data))
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self.ws.send(data)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_error(self, code: str, message: str) -> None:
        await self.send_json({"type": "error", "code": code, "message": message})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await asyncio.wait_for(self.ws.close(code, reason), timeout=3.0)
        except Exception:
            pass

    @property
    def remote_ip(self) -> str:
        addr = self.ws.remote_address
        return addr[0] if addr else "unknown"


# ---------------------------------------------------------------------------
# Relay Server
# ---------------------------------------------------------------------------

class RelayServer:
    """
    Central relay — brokers sessions between controllers and agents.
    """

    def __init__(self):
        self._connections: Dict[str, Connection] = {}
        self._agent_conns: Dict[str, str] = {}       # device_id → conn_id
        self._controller_conns: Dict[str, str] = {}  # user_id → conn_id (latest)
        self._stats = RelayStats()

        # Rate limiters
        self._auth_limiter = RateLimiter(max_calls=10, window_seconds=60)
        self._msg_limiter = RateLimiter(max_calls=500, window_seconds=10)

        self._heartbeat = HeartbeatManager(
            interval=settings.relay.heartbeat_interval,
            max_misses=3,
            on_dead=self._on_peer_dead,
        )

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self) -> None:
        from transport.tls_context import TLSContextFactory
        ssl_ctx = TLSContextFactory.server_context()

        self._heartbeat.start()
        asyncio.create_task(self._sweep_loop())

        log.info(
            "relay.starting",
            host=settings.relay.host,
            port=settings.relay.port,
        )
        server = WSServer(
            host=settings.relay.host,
            port=settings.relay.port,
            handler=self._handle_connection,
            ssl_context=ssl_ctx,
            max_size=settings.relay.max_payload_bytes,
            ping_interval=None,
        )
        await server.start()
        log.info("relay.started")

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(self, ws: ServerConnection) -> None:
        conn = Connection(ws)
        self._connections[conn.conn_id] = conn
        self._heartbeat.register(conn.conn_id)
        self._stats.total_connections += 1
        self._stats.active_connections += 1
        log.info("relay.connection_opened", conn_id=conn.conn_id, ip=conn.remote_ip)

        try:
            # Rate-limit by IP before accepting auth
            if not self._auth_limiter.is_allowed(conn.remote_ip):
                await conn.send_error("rate_limited", "Too many connections from this IP")
                return

            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            msg = json.loads(raw)

            if not await self._authenticate(conn, msg):
                return

            async for raw_msg in ws:
                self._heartbeat.record_pong(conn.conn_id)
                if not self._msg_limiter.is_allowed(conn.conn_id):
                    await conn.send_error("rate_limited", "Message rate exceeded")
                    continue
                await self._dispatch(conn, raw_msg)

        except asyncio.TimeoutError:
            await conn.send_error("timeout", "Authentication timed out")
        except websockets.exceptions.ConnectionClosed as e:
            log.info("relay.connection_closed", conn_id=conn.conn_id, code=e.code)
        except Exception as e:
            log.error("relay.connection_error", conn_id=conn.conn_id, error=str(e))
        finally:
            self._stats.active_connections -= 1
            await self._cleanup(conn)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self, conn: Connection, msg: dict) -> bool:
        mtype = msg.get("type")

        if mtype == "auth.controller":
            token = msg.get("token", "")
            if not token:
                await conn.send_error("missing_token", "Token required")
                return False
            try:
                payload = auth_service.verify_access_token(token)
            except AuthError as e:
                await conn.send_error("auth_failed", str(e))
                return False

            conn.user_id = payload.sub
            conn.role = Role(payload.role)
            conn.identity_type = "controller"
            conn.authenticated = True
            self._controller_conns[conn.user_id] = conn.conn_id
            await conn.send_json({"type": "auth.ok", "user_id": conn.user_id})
            log.info("relay.controller_authenticated",
                     user_id=conn.user_id, ip=conn.remote_ip)
            return True

        elif mtype == "auth.agent":
            device_id = msg.get("device_id", "")
            token = msg.get("token", "")
            if not device_id or not token:
                await conn.send_error("missing_fields", "device_id and token required")
                return False

            # In production: verify token against DB / signed JWT
            conn.device_id = device_id
            conn.role = Role.AGENT
            conn.identity_type = "agent"
            conn.authenticated = True
            self._agent_conns[device_id] = conn.conn_id

            device_info = DeviceInfo(
                device_id=device_id,
                name=msg.get("name", device_id),
                device_type=DeviceType(msg.get("device_type", "unknown")),
                ip_address=conn.remote_ip,
                capabilities=DeviceCapabilities(**msg.get("capabilities", {})),
                status=DeviceStatus.ONLINE,
                metadata=msg.get("metadata", {}),
            )
            await device_registry.register(device_info)
            await conn.send_json({"type": "auth.ok", "device_id": device_id})
            log.info("relay.agent_authenticated",
                     device_id=device_id, ip=conn.remote_ip)
            return True

        else:
            await conn.send_error(
                "bad_auth_type",
                f"Expected auth.controller or auth.agent, got: {mtype}",
            )
            return False

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, conn: Connection, raw: str | bytes) -> None:
        # Binary = encrypted session frame
        if isinstance(raw, bytes):
            await self._route_binary(conn, raw)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        mtype = msg.get("type", "")

        if mtype == "ping":
            await conn.send_json({"type": "pong"})

        elif mtype == "session.request" and conn.identity_type == "controller":
            await self._handle_session_request(conn, msg)

        elif mtype == "session.accept" and conn.identity_type == "agent":
            await self._handle_session_accept(conn, msg)

        elif mtype == "session.reject" and conn.identity_type == "agent":
            await self._handle_session_reject(conn, msg)

        elif mtype == "session.close":
            await self._handle_session_close(conn, msg)

        elif mtype == "device.list" and conn.identity_type == "controller":
            devices = await device_registry.list_online()
            await conn.send_json({
                "type": "device.list",
                "devices": [d.model_dump() for d in devices],
            })

        elif mtype == "device.metrics" and conn.identity_type == "agent":
            await self._handle_metrics(conn, msg)

        elif mtype == "relay.stats" and conn.identity_type == "controller":
            self._stats.active_sessions = len(await session_manager.list_active())
            await conn.send_json({"type": "relay.stats", **self._stats.to_dict()})

        else:
            log.debug("relay.unhandled", mtype=mtype, conn_id=conn.conn_id)

    # ------------------------------------------------------------------
    # Session brokering
    # ------------------------------------------------------------------

    async def _handle_session_request(self, conn: Connection, msg: dict) -> None:
        device_id = msg.get("device_id")
        if not device_id:
            await conn.send_error("missing_field", "device_id required")
            return

        device = await device_registry.get(device_id)
        if not device:
            await conn.send_error("not_found", f"Device '{device_id}' not found")
            return
        if device.status == DeviceStatus.OFFLINE:
            await conn.send_error("device_offline", "Device is offline")
            return
        if device.status == DeviceStatus.BUSY:
            await conn.send_error("device_busy", "Device is in an active session")
            return

        session = await session_manager.create(
            controller_id=conn.user_id,
            device_id=device_id,
            controller_role=conn.role,
        )
        self._stats.total_sessions += 1

        # Wire send functions
        ctrl_conn_id = conn.conn_id

        async def send_to_controller(data: bytes) -> None:
            c = self._connections.get(ctrl_conn_id)
            if c:
                await c.send_bytes(data)
                self._stats.total_bytes_relayed += len(data)

        session.attach_controller(send_to_controller)

        agent_conn_id = self._agent_conns.get(device_id)
        if agent_conn_id and agent_conn_id in self._connections:
            agent_conn = self._connections[agent_conn_id]

            async def send_to_agent(data: bytes) -> None:
                self._stats.total_bytes_relayed += len(data)
                await agent_conn.send_bytes(data)

            session.attach_agent(send_to_agent)

            await agent_conn.send_json({
                "type": "session.request",
                "session_id": session.session_id,
                "controller_id": conn.user_id,
                "ecdh_key": session.get_ecdh_offer().hex(),
            })
            await conn.send_json({
                "type": "session.pending",
                "session_id": session.session_id,
            })
            log.info("relay.session_pending",
                     session_id=session.session_id,
                     device_id=device_id,
                     controller_id=conn.user_id)
        else:
            await session_manager.close(session.session_id, reason="agent_unavailable")
            await conn.send_error("agent_unavailable", "Agent is not connected")

    async def _handle_session_accept(self, conn: Connection, msg: dict) -> None:
        session_id = msg.get("session_id")
        ecdh_hex = msg.get("ecdh_key", "")
        session = await session_manager.get(session_id)
        if not session:
            return
        try:
            session.complete_handshake(bytes.fromhex(ecdh_hex))
        except Exception as e:
            log.error("relay.handshake_failed", session_id=session_id, error=str(e))
            await session_manager.close(session_id, reason="handshake_failed")
            return

        ctrl_conn_id = self._controller_conns.get(session.controller_id)
        if ctrl_conn_id and ctrl_conn_id in self._connections:
            await self._connections[ctrl_conn_id].send_json({
                "type": "session.active",
                "session_id": session_id,
                "ecdh_key": ecdh_hex,
            })

        await device_registry.set_status(conn.device_id, DeviceStatus.BUSY)
        self._stats.active_sessions += 1
        log.info("relay.session_active", session_id=session_id)

    async def _handle_session_reject(self, conn: Connection, msg: dict) -> None:
        session_id = msg.get("session_id")
        reason = msg.get("reason", "rejected_by_agent")
        session = await session_manager.get(session_id)
        if session:
            ctrl_conn_id = self._controller_conns.get(session.controller_id)
            if ctrl_conn_id and ctrl_conn_id in self._connections:
                await self._connections[ctrl_conn_id].send_json({
                    "type": "session.rejected",
                    "session_id": session_id,
                    "reason": reason,
                })
            await session_manager.close(session_id, reason=reason)

    async def _handle_session_close(self, conn: Connection, msg: dict) -> None:
        session_id = msg.get("session_id")
        if not session_id:
            return
        session = await session_manager.get(session_id)
        if not session:
            return

        # Notify the other side
        if conn.identity_type == "controller":
            agent_conn_id = self._agent_conns.get(session.device_id)
            if agent_conn_id and agent_conn_id in self._connections:
                await self._connections[agent_conn_id].send_json({
                    "type": "session.close",
                    "session_id": session_id,
                })
        else:
            ctrl_conn_id = self._controller_conns.get(session.controller_id)
            if ctrl_conn_id and ctrl_conn_id in self._connections:
                await self._connections[ctrl_conn_id].send_json({
                    "type": "session.close",
                    "session_id": session_id,
                })

        await session_manager.close(session_id, reason="user_closed")
        if conn.device_id:
            await device_registry.set_status(conn.device_id, DeviceStatus.ONLINE)
        if self._stats.active_sessions > 0:
            self._stats.active_sessions -= 1

    async def _route_binary(self, conn: Connection, data: bytes) -> None:
        """Route an encrypted binary session frame to its destination."""
        try:
            session_id = data[:36].decode("ascii")
            payload = data[36:]
        except Exception:
            return

        session = await session_manager.get(session_id)
        if not session or session.state != SessionState.ACTIVE:
            return

        # Security: verify the sender is authorised for this session
        if conn.identity_type == "controller":
            if session.controller_id != conn.user_id:
                log.warning("relay.unauthorized_frame",
                            conn_id=conn.conn_id,
                            session_id=session_id)
                return
            await session.handle_from_controller(payload)
        else:
            if session.device_id != conn.device_id:
                log.warning("relay.unauthorized_frame",
                            conn_id=conn.conn_id,
                            session_id=session_id)
                return
            await session.handle_from_agent(payload)

    async def _handle_metrics(self, conn: Connection, msg: dict) -> None:
        from core.registry import DeviceMetrics
        try:
            m = DeviceMetrics(
                device_id=conn.device_id,
                **msg.get("metrics", {}),
            )
            await device_registry.update_metrics(m)
            await device_registry.heartbeat(conn.device_id)
        except Exception as e:
            log.warning("relay.metrics_error", error=str(e))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self, conn: Connection) -> None:
        self._heartbeat.unregister(conn.conn_id)
        self._connections.pop(conn.conn_id, None)

        if conn.identity_type == "agent" and conn.device_id:
            self._agent_conns.pop(conn.device_id, None)
            await device_registry.set_status(conn.device_id, DeviceStatus.OFFLINE)
            await session_manager.close_all_for_device(conn.device_id)
            log.info("relay.agent_disconnected", device_id=conn.device_id)
        elif conn.identity_type == "controller" and conn.user_id:
            self._controller_conns.pop(conn.user_id, None)
            log.info("relay.controller_disconnected", user_id=conn.user_id)

    async def _on_peer_dead(self, conn_id: str) -> None:
        conn = self._connections.get(conn_id)
        if conn:
            log.warning("relay.peer_dead", conn_id=conn_id)
            await conn.close(code=1001, reason="heartbeat_timeout")
            await self._cleanup(conn)

    # ------------------------------------------------------------------
    # Background sweep task
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Periodically clean up stale devices and expired sessions."""
        while True:
            await asyncio.sleep(60)
            try:
                stale = await device_registry.sweep_stale(timeout_s=90)
                if stale:
                    log.info("relay.sweep_stale", count=len(stale), device_ids=stale)
                expired = await session_manager.sweep_expired(
                    timeout_s=settings.relay.session_timeout
                )
                if expired:
                    log.info("relay.sweep_sessions", expired=expired)
                    self._stats.active_sessions = max(0, self._stats.active_sessions - expired)
            except Exception as e:
                log.error("relay.sweep_error", error=str(e))


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="NEXUS Relay Server")
    parser.add_argument("--host", default=settings.relay.host)
    parser.add_argument("--port", type=int, default=settings.relay.port)
    args = parser.parse_args()

    # Override settings from CLI
    settings.relay.host = args.host
    settings.relay.port = args.port
    settings.ensure_dirs()

    relay = RelayServer()
    await relay.start()
    log.info("relay.running", host=args.host, port=args.port)
    await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(_main())
