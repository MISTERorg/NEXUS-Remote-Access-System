"""
transport/tunnel.py
--------------------
Encrypted TCP tunnelling for NAT traversal and SSH relay hops.

Two modes:
  1. **ReverseTunnel** — agent initiates an outbound connection to the relay,
     which then forwards controller traffic back through that channel.
     Works through NAT without port-forwarding.

  2. **SSHTunnel** — wraps paramiko to create an SSH port-forward tunnel.
     Useful when an SSH daemon is already reachable on the target.

Both modes expose the same async send()/receive() interface so upstream
code is transport-agnostic.

Usage (reverse tunnel):
    tunnel = ReverseTunnel(
        relay_host="relay.example.com",
        relay_port=7100,
        device_id="my-server-01",
        token="agent-token",
        ssl_context=TLSContextFactory.client_context(),
    )
    await tunnel.connect()
    await tunnel.send(b"hello from agent")
    data = await tunnel.receive()

Usage (SSH tunnel):
    tunnel = SSHTunnel(
        ssh_host="192.168.1.50",
        ssh_user="ubuntu",
        ssh_key_path="~/.ssh/id_rsa",
        remote_port=7000,
        local_port=17000,
    )
    await tunnel.open()
    # Traffic to localhost:17000 is forwarded to target:7000
"""

from __future__ import annotations

import asyncio
import json
import ssl
import struct
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger("nexus.transport.tunnel")

HEADER_FMT = ">I"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAX_FRAME = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(HEADER_SIZE)
    (length,) = struct.unpack(HEADER_FMT, header)
    if length > MAX_FRAME:
        raise ValueError(f"Frame too large: {length}")
    return await reader.readexactly(length)


def _make_frame(data: bytes) -> bytes:
    return struct.pack(HEADER_FMT, len(data)) + data


# ---------------------------------------------------------------------------
# ReverseTunnel
# ---------------------------------------------------------------------------

class ReverseTunnel:
    """
    Outbound TCP tunnel from agent to relay.

    The agent makes the first connection so the relay never needs to reach
    through NAT to the agent. The relay identifies this as a tunnel channel
    via an initial handshake message.

    Protocol after TCP connect + TLS:
      Agent → Relay:  JSON { "type": "tunnel.register", "device_id": ..., "token": ... }
      Relay → Agent:  JSON { "type": "tunnel.ok" }
      Then: bidirectional length-prefixed binary frames.
    """

    def __init__(
        self,
        relay_host: str,
        relay_port: int,
        device_id: str,
        token: str,
        ssl_context: Optional[ssl.SSLContext] = None,
        reconnect_delay: float = 5.0,
        max_retries: int = 0,      # 0 = infinite
    ):
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.device_id = device_id
        self.token = token
        self._ssl = ssl_context
        self._reconnect_delay = reconnect_delay
        self._max_retries = max_retries
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False
        self._recv_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

    async def connect(self) -> None:
        """Connect, register, and start background receive loop."""
        self._running = True
        attempt = 0

        while self._running:
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    self.relay_host, self.relay_port, ssl=self._ssl
                )
                # Register as tunnel channel
                reg_msg = json.dumps({
                    "type": "tunnel.register",
                    "device_id": self.device_id,
                    "token": self.token,
                }).encode()
                self._writer.write(_make_frame(reg_msg))
                await self._writer.drain()

                # Wait for ACK
                ack_raw = await _read_frame(self._reader)
                ack = json.loads(ack_raw)
                if ack.get("type") != "tunnel.ok":
                    raise RuntimeError(f"Tunnel registration rejected: {ack}")

                log.info("tunnel.connected",
                         relay=f"{self.relay_host}:{self.relay_port}",
                         device_id=self.device_id)
                attempt = 0
                await self._receive_loop()

            except asyncio.IncompleteReadError:
                log.warning("tunnel.remote_closed")
            except (OSError, ConnectionRefusedError) as e:
                log.warning("tunnel.connect_failed", error=str(e))
            except Exception as e:
                log.error("tunnel.error", error=str(e))
            finally:
                self._reader = None
                self._writer = None

            if not self._running:
                break
            attempt += 1
            if self._max_retries and attempt >= self._max_retries:
                log.error("tunnel.max_retries")
                break
            delay = self._reconnect_delay * min(attempt, 8)
            log.info("tunnel.reconnecting", delay=delay, attempt=attempt)
            await asyncio.sleep(delay)

    async def _receive_loop(self) -> None:
        while self._running and self._reader:
            frame = await _read_frame(self._reader)
            await self._recv_queue.put(frame)

    async def send(self, data: bytes) -> None:
        if not self._writer:
            raise RuntimeError("Tunnel not connected")
        self._writer.write(_make_frame(data))
        await self._writer.drain()

    async def receive(self, timeout: float = 30.0) -> bytes:
        return await asyncio.wait_for(self._recv_queue.get(), timeout=timeout)

    async def close(self) -> None:
        self._running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        log.info("tunnel.closed", device_id=self.device_id)

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()


# ---------------------------------------------------------------------------
# SSHTunnel
# ---------------------------------------------------------------------------

class SSHTunnel:
    """
    SSH port-forward tunnel using paramiko.

    Opens an SSH connection and creates a local port forward:
        localhost:{local_port}  →  {remote_host}:{remote_port}

    This is a blocking setup wrapped in a thread executor so it doesn't
    stall the event loop.

    Args:
        ssh_host: SSH server hostname.
        ssh_port: SSH server port (default: 22).
        ssh_user: SSH username.
        ssh_key_path: Path to private key file. If None, uses password.
        ssh_password: Password (used when ssh_key_path is None).
        remote_host: Host to forward to (from the SSH server's perspective).
        remote_port: Port to forward to on remote_host.
        local_port: Local port to bind (0 = pick free port).
    """

    def __init__(
        self,
        ssh_host: str,
        ssh_user: str,
        remote_port: int,
        remote_host: str = "localhost",
        ssh_port: int = 22,
        ssh_key_path: Optional[str] = None,
        ssh_password: Optional[str] = None,
        local_port: int = 0,
    ):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_key_path = ssh_key_path
        self.ssh_password = ssh_password
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port
        self._actual_local_port: Optional[int] = None
        self._client = None
        self._server = None

    async def open(self) -> int:
        """
        Open the SSH tunnel.
        Returns the local port that was bound (useful when local_port=0).
        """
        loop = asyncio.get_event_loop()
        self._actual_local_port = await loop.run_in_executor(None, self._open_sync)
        log.info("ssh_tunnel.opened",
                 local_port=self._actual_local_port,
                 remote=f"{self.remote_host}:{self.remote_port}")
        return self._actual_local_port

    def _open_sync(self) -> int:
        try:
            import paramiko
            from paramiko.transport import Transport
        except ImportError:
            raise RuntimeError("paramiko is required for SSH tunnelling: pip install paramiko")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = dict(
            hostname=self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
        )
        if self.ssh_key_path:
            key_path = Path(self.ssh_key_path).expanduser()
            connect_kwargs["key_filename"] = str(key_path)
        elif self.ssh_password:
            connect_kwargs["password"] = self.ssh_password
        else:
            connect_kwargs["look_for_keys"] = True

        client.connect(**connect_kwargs)
        self._client = client

        transport = client.get_transport()
        transport.request_port_forward("", self.local_port or 0)

        # Get the actual port
        actual_port = self.local_port
        if actual_port == 0:
            # paramiko doesn't directly expose the bound port from request_port_forward
            # for local forwards; use standard socket approach instead.
            import socket
            import threading

            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            actual_port = sock.getsockname()[1]
            sock.close()

            def forward():
                import socketserver

                class Handler(socketserver.BaseRequestHandler):
                    def handle(inner_self):
                        chan = transport.open_channel(
                            "direct-tcpip",
                            (self.remote_host, self.remote_port),
                            inner_self.request.getpeername(),
                        )
                        if chan is None:
                            return
                        # Bidirectional copy
                        import select
                        while True:
                            r, _, _ = select.select([inner_self.request, chan], [], [], 1)
                            if inner_self.request in r:
                                data = inner_self.request.recv(1024)
                                if not data:
                                    break
                                chan.send(data)
                            if chan in r:
                                data = chan.recv(1024)
                                if not data:
                                    break
                                inner_self.request.send(data)
                        chan.close()

                class Server(socketserver.ThreadingTCPServer):
                    daemon_threads = True
                    allow_reuse_address = True

                self._server = Server(("127.0.0.1", actual_port), Handler)
                self._server.serve_forever()

            t = threading.Thread(target=forward, daemon=True)
            t.start()

        return actual_port

    @property
    def local_port_bound(self) -> Optional[int]:
        return self._actual_local_port

    async def close(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._close_sync)
        log.info("ssh_tunnel.closed")

    def _close_sync(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# TunnelManager — registry of active tunnels
# ---------------------------------------------------------------------------

class TunnelManager:
    """Tracks active reverse tunnels keyed by device_id."""

    def __init__(self):
        self._tunnels: dict[str, ReverseTunnel] = {}

    def register(self, device_id: str, tunnel: ReverseTunnel) -> None:
        self._tunnels[device_id] = tunnel
        log.info("tunnel_manager.registered", device_id=device_id)

    def unregister(self, device_id: str) -> None:
        self._tunnels.pop(device_id, None)

    def get(self, device_id: str) -> Optional[ReverseTunnel]:
        return self._tunnels.get(device_id)

    def all_device_ids(self) -> list[str]:
        return list(self._tunnels.keys())

    async def close_all(self) -> None:
        for tunnel in list(self._tunnels.values()):
            await tunnel.close()
        self._tunnels.clear()
        log.info("tunnel_manager.all_closed")


tunnel_manager = TunnelManager()
