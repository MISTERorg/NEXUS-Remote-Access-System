"""
transport/tcp_transport.py
--------------------------
Raw TCP transport with TLS support.

Used for direct peer-to-peer tunnelling when NAT traversal succeeds,
or as a fallback transport when WebSockets are not available.

Provides:
  - TCPServer: async TCP listener (wraps asyncio.start_server)
  - TCPClient: async TCP client with auto-reconnect
  - FrameProtocol: length-prefixed framing over raw TCP
"""

from __future__ import annotations

import asyncio
import ssl
import struct
from typing import Callable, Coroutine, Optional

from utils.logger import get_logger

log = get_logger("nexus.transport.tcp")

# ---------------------------------------------------------------------------
# Frame protocol — 4-byte big-endian length prefix
# ---------------------------------------------------------------------------

HEADER_FMT = ">I"  # unsigned int, big-endian
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAX_FRAME_BYTES = 16 * 1024 * 1024  # 16 MB safety cap


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed frame from a stream reader."""
    header = await reader.readexactly(HEADER_SIZE)
    (length,) = struct.unpack(HEADER_FMT, header)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"Frame too large: {length} bytes")
    return await reader.readexactly(length)


def make_frame(data: bytes) -> bytes:
    """Wrap data in a length-prefixed frame."""
    return struct.pack(HEADER_FMT, len(data)) + data


# ---------------------------------------------------------------------------
# TCPServer
# ---------------------------------------------------------------------------

MessageHandler = Callable[[bytes], Coroutine]


class TCPServer:
    """
    Async TCP server with TLS support.

    Args:
        host: Bind address.
        port: Bind port.
        on_message: Async callback(data: bytes) invoked per received frame.
        ssl_context: Optional TLS context; plain TCP if None.
    """

    def __init__(
            self,
            host: str,
            port: int,
            on_message: MessageHandler,
            ssl_context: Optional[ssl.SSLContext] = None,
    ):
        self.host = host
        self.port = port
        self._on_message = on_message
        self._ssl = ssl_context
        self._server: Optional[asyncio.Server] = None
        self._clients: dict[str, asyncio.StreamWriter] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
            ssl=self._ssl,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("tcp_server.started", addrs=addrs)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("tcp_server.stopped")

    async def _handle_client(
            self,
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername", "unknown")
        client_key = str(addr)
        self._clients[client_key] = writer
        log.info("tcp_server.client_connected", addr=addr)
        try:
            while True:
                frame = await read_frame(reader)
                await self._on_message(frame)
        except asyncio.IncompleteReadError:
            pass  # Client disconnected cleanly
        except Exception as e:
            log.warning("tcp_server.client_error", addr=addr, error=str(e))
        finally:
            self._clients.pop(client_key, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("tcp_server.client_disconnected", addr=addr)

    async def broadcast(self, data: bytes) -> None:
        """Send a frame to all connected clients."""
        frame = make_frame(data)
        dead = []
        for key, writer in list(self._clients.items()):
            try:
                writer.write(frame)
                await writer.drain()
            except Exception:
                dead.append(key)
        for key in dead:
            self._clients.pop(key, None)


# ---------------------------------------------------------------------------
# TCPClient
# ---------------------------------------------------------------------------

class TCPClient:
    """
    Async TCP client with automatic reconnection.

    Args:
        host: Remote host.
        port: Remote port.
        on_message: Async callback(data: bytes) per received frame.
        ssl_context: Optional TLS context.
        reconnect_delay: Seconds between reconnect attempts.
        max_retries: Maximum reconnect attempts (0 = infinite).
    """

    def __init__(
            self,
            host: str,
            port: int,
            on_message: MessageHandler,
            ssl_context: Optional[ssl.SSLContext] = None,
            reconnect_delay: float = 5.0,
            max_retries: int = 10,
    ):
        self.host = host
        self.port = port
        self._on_message = on_message
        self._ssl = ssl_context
        self._reconnect_delay = reconnect_delay
        self._max_retries = max_retries
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False

    async def connect(self) -> None:
        """Connect and start receiving. Reconnects on drop."""
        self._running = True
        attempt = 0

        while self._running:
            try:
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, ssl=self._ssl
                )
                self._writer = writer
                attempt = 0
                log.info("tcp_client.connected", host=self.host, port=self.port)
                await self._receive_loop(reader)
            except (ConnectionRefusedError, OSError) as e:
                log.warning("tcp_client.connect_failed", error=str(e), attempt=attempt)
            except Exception as e:
                log.error("tcp_client.error", error=str(e))
            finally:
                self._writer = None

            if not self._running:
                break
            attempt += 1
            if self._max_retries and attempt >= self._max_retries:
                log.error("tcp_client.max_retries_reached")
                break
            await asyncio.sleep(self._reconnect_delay * min(attempt, 6))

    async def _receive_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await read_frame(reader)
            await self._on_message(frame)

    async def send(self, data: bytes) -> None:
        if not self._writer:
            raise RuntimeError("Not connected")
        self._writer.write(make_frame(data))
        await self._writer.drain()

    async def disconnect(self) -> None:
        self._running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        log.info("tcp_client.disconnected")

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()
