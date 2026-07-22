"""
transport/websocket_transport.py
---------------------------------
WebSocket transport layer for NEXUS.

Improvements over v1:
  - Updated to use websockets.asyncio.server.ServerConnection type
  - WSClient.send() accepts dict directly (auto-serialises to JSON)
  - ConnectionPool.broadcast() returns count of successful sends
  - Added WSClient.connected property based on ws state
  - Graceful close with timeout in WSClient.close()
  - Added per-connection metadata dict for upstream use
"""

from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, Callable, Coroutine, Dict, Optional

import websockets
from websockets.asyncio.server import ServerConnection

from utils.logger import get_logger

log = get_logger("nexus.transport.ws")

MessageHandler = Callable[[bytes | str], Coroutine]


# ---------------------------------------------------------------------------
# WSServer
# ---------------------------------------------------------------------------

class WSServer:
    """
    Thin WebSocket server wrapper around websockets.serve().
    """

    def __init__(
        self,
        host: str,
        port: int,
        handler: Callable[[ServerConnection], Coroutine],
        ssl_context: Optional[ssl.SSLContext] = None,
        max_size: int = 10 * 1024 * 1024,
        ping_interval: Optional[int] = None,
    ):
        self.host = host
        self.port = port
        self._handler = handler
        self._ssl = ssl_context
        self._max_size = max_size
        self._ping_interval = ping_interval
        self._server = None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            ssl=self._ssl,
            max_size=self._max_size,
            ping_interval=self._ping_interval,
        )
        proto = "wss" if self._ssl else "ws"
        log.info("ws_server.started", url=f"{proto}://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("ws_server.stopped")

    async def serve_forever(self) -> None:
        await self.start()
        await asyncio.Future()


# ---------------------------------------------------------------------------
# WSClient
# ---------------------------------------------------------------------------

class WSClient:
    """
    Auto-reconnecting WebSocket client.

    Args:
        url: ws:// or wss:// URL.
        on_message: Async callback(data: bytes | str) per message.
        on_connect: Optional async callback fired after each connection.
        on_disconnect: Optional async callback fired after each drop.
        ssl_context: Optional TLS context.
        reconnect_delay: Base reconnect back-off in seconds.
        max_retries: Max reconnect attempts (0 = infinite).
    """

    def __init__(
        self,
        url: str,
        on_message: MessageHandler,
        on_connect: Optional[Callable[[], Coroutine]] = None,
        on_disconnect: Optional[Callable[[], Coroutine]] = None,
        ssl_context: Optional[ssl.SSLContext] = None,
        reconnect_delay: float = 5.0,
        max_retries: int = 10,
    ):
        self.url = url
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._ssl = ssl_context
        self._reconnect_delay = reconnect_delay
        self._max_retries = max_retries
        self._ws = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        attempt = 0

        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ssl=self._ssl,
                    ping_interval=None,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    log.info("ws_client.connected", url=self.url)
                    if self._on_connect:
                        await self._on_connect()
                    await self._receive_loop(ws)

            except websockets.exceptions.ConnectionClosed as e:
                log.warning("ws_client.closed", code=e.code, reason=e.reason)
            except (OSError, websockets.exceptions.WebSocketException) as e:
                log.warning("ws_client.connect_error", error=str(e))
            except Exception as e:
                log.error("ws_client.error", error=str(e))
            finally:
                self._ws = None
                if self._on_disconnect:
                    try:
                        await self._on_disconnect()
                    except Exception:
                        pass

            if not self._running:
                break
            attempt += 1
            if self._max_retries and attempt >= self._max_retries:
                log.error("ws_client.max_retries_reached", url=self.url)
                break
            delay = min(self._reconnect_delay * (2 ** (attempt - 1)), 120)
            log.info("ws_client.reconnecting", in_s=delay, attempt=attempt)
            await asyncio.sleep(delay)

    async def _receive_loop(self, ws) -> None:
        async for message in ws:
            try:
                await self._on_message(message)
            except Exception as e:
                log.error("ws_client.handler_error", error=str(e))

    async def send(self, data: bytes | str | dict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        if isinstance(data, dict):
            data = json.dumps(data)
        await self._ws.send(data)

    async def close(self) -> None:
        self._running = False
        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=3.0)
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        if self._ws is None:
            return False
        # Check using the state attribute available in websockets library
        try:
            return not self._ws.closed
        except AttributeError:
            return self._ws is not None


# ---------------------------------------------------------------------------
# ConnectionPool
# ---------------------------------------------------------------------------

class ConnectionPool:
    """
    Thread-safe registry of active WebSocket connections keyed by a string ID.
    """

    def __init__(self):
        self._pool: Dict[str, Any] = {}
        self._meta: Dict[str, Dict] = {}     # per-connection metadata
        self._lock = asyncio.Lock()

    async def add(self, conn_id: str, ws, meta: dict | None = None) -> None:
        async with self._lock:
            self._pool[conn_id] = ws
            self._meta[conn_id] = meta or {}

    async def remove(self, conn_id: str) -> None:
        async with self._lock:
            self._pool.pop(conn_id, None)
            self._meta.pop(conn_id, None)

    async def get(self, conn_id: str):
        return self._pool.get(conn_id)

    def get_meta(self, conn_id: str) -> dict:
        return self._meta.get(conn_id, {})

    async def broadcast(self, data: bytes | str, exclude: Optional[str] = None) -> int:
        """Broadcast to all connections. Returns number of successful sends."""
        dead = []
        sent = 0
        for cid, ws in list(self._pool.items()):
            if cid == exclude:
                continue
            try:
                await ws.send(data)
                sent += 1
            except Exception:
                dead.append(cid)
        for cid in dead:
            async with self._lock:
                self._pool.pop(cid, None)
                self._meta.pop(cid, None)
        return sent

    async def send_to(self, conn_id: str, data: bytes | str) -> bool:
        ws = self._pool.get(conn_id)
        if ws is None:
            return False
        try:
            await ws.send(data)
            return True
        except Exception:
            async with self._lock:
                self._pool.pop(conn_id, None)
                self._meta.pop(conn_id, None)
            return False

    def count(self) -> int:
        return len(self._pool)

    def ids(self) -> list[str]:
        return list(self._pool.keys())
