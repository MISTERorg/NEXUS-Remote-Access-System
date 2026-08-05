"""
transport/hole_punch.py
------------------------
Phase 1 (endpoint exchange) and Phase 2 (hole-punch attempt) of direct
agent↔peer connectivity, as an addition on top of — never a replacement
for — the relay path that's the entire reason NEXUS can reach devices
across arbitrary NATs and CGNAT today.

READ THIS BEFORE WIRING ANYTHING ELSE INTO IT
-----------------------------------------------
The controller in this project is a browser. Browsers have no API for
raw UDP sockets — that's a deliberate, permanent security boundary in
every mainstream browser, not a missing feature that might get added.
So the classes in this file (real, tested, working Python-to-Python UDP
hole punching) currently have exactly one usable side: the agent. There
is no browser-side counterpart, and adding one is a genuinely different,
much larger project — WebRTC (`RTCPeerConnection` in JS + `aiortc` in
Python), with its own ICE/STUN/TURN/DTLS/SCTP stack and a heavy new
Python dependency (aiortc needs PyAV/FFmpeg bindings). That is NOT what
this file implements.

What this file IS good for today: any future non-browser controller
(a native/CLI/desktop client, or agent-to-agent scenarios), and as the
exact groundwork a future WebRTC bridge would reuse for signaling (the
relay-mediated endpoint exchange below doesn't care what either side
does with the exchanged endpoint — punch raw UDP, or hand it to an ICE
stack as a candidate). This matches this project's existing pattern —
transport/tcp_transport.py and transport/tunnel.py are the same shape:
real, tested capabilities not yet wired into relay.py's default path.

WIRE PROTOCOL — PunchRendezvousServer (STUN-lite)
---------------------------------------------------
Relay-side UDP listener. A client sends a small authenticated probe;
the relay replies with the address:port it observed the packet coming
from — the client's real public endpoint, as seen from the outside,
exactly what STUN's XOR-MAPPED-ADDRESS gives you, minus the XOR
obfuscation (irrelevant here — this isn't trying to dodge middlebox
NAT-rewriting-in-transit the way STUN's obfuscation historically did).

Deliberately NOT an open reflector: every probe must carry a token
that's HMAC-derived from the session's own encryption context (see
`make_probe_token`), so this can't be abused by third parties as a free
"what's my IP" or UDP amplification service — the response is the same
size as the request either way, so it's not useful for amplification
regardless, but requiring a valid session token means only genuine
session participants can use it at all.

    Request  (client -> relay):  {"type": "probe", "token": "<hex>"}
    Response (relay -> client):  {"type": "probe_ack", "endpoint": [ip, port]}

WIRE PROTOCOL — hole punch (agent <-> agent, once both have offers)
----------------------------------------------------------------------
Once both sides have exchanged observed endpoints (via
MessageType.P2P_ENDPOINT_OFFER over the existing relay-signaled,
authenticated channel — see core/session.py), each side fires UDP
packets at the other's reported endpoint while listening on the same
local socket used for the STUN-lite probe above (same 5-tuple, so the
NAT mapping just created by that probe is what a symmetric-NAT peer's
inbound packet needs to match). A successful punch is confirmed by
receiving a "punch_ack" carrying the token back.

    Punch    :  {"type": "punch", "token": "<hex>"}
    Punch ack:  {"type": "punch_ack", "token": "<hex>"}

This inherently does NOT work for every NAT type — see
PunchAttempt.run()'s docstring for the honest success-rate picture.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Optional, Tuple

log = logging.getLogger("nexus.transport.hole_punch")

MAX_DATAGRAM = 512          # generous for our tiny JSON messages; keeps us
                             # well clear of any path MTU concerns
PROBE_TOKEN_TTL = 120        # seconds a probe token remains valid


def make_probe_token(session_secret: bytes, session_id: str) -> str:
    """
    Derive a short-lived probe token from material already established by
    the session's own ECDH handshake (session_secret) — never a fresh,
    independently-guessable value. Both the client and the relay can
    compute this independently (the relay already holds the session's key
    material for encryption), so no extra round trip is needed to hand
    out a token before it can be used.
    """
    mac = hmac.new(session_secret, session_id.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]


# ─────────────────────────────────────────────────────────────────────────────
# Relay side — STUN-lite rendezvous
# ─────────────────────────────────────────────────────────────────────────────

class _RendezvousProtocol(asyncio.DatagramProtocol):
    def __init__(self, valid_token_fn):
        super().__init__()
        self._valid_token_fn = valid_token_fn
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        if len(data) > MAX_DATAGRAM:
            return  # ignore anything oversized — not a valid probe
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if msg.get("type") != "probe":
            return
        token = msg.get("token", "")
        if not token or not self._valid_token_fn(token):
            log.debug("hole_punch.rejected_probe", addr=addr)
            return

        reply = json.dumps({"type": "probe_ack", "endpoint": list(addr)}).encode("utf-8")
        if self.transport:
            self.transport.sendto(reply, addr)


class PunchRendezvousServer:
    """
    Relay-side STUN-lite UDP reflector. One instance per relay process,
    started alongside the existing TCP/WSS listeners in core/relay.py.

    `token_validator(token) -> bool` is supplied by the relay — typically
    "does this token match make_probe_token() for any currently-active
    session's secret". Kept as an injected callback so this file has no
    dependency on core/session.py's internals.
    """

    def __init__(self, host: str, port: int, token_validator):
        self.host = host
        self.port = port
        self._token_validator = token_validator
        self._transport: Optional[asyncio.DatagramTransport] = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _RendezvousProtocol(self._token_validator),
            local_addr=(self.host, self.port),
        )
        log.info("hole_punch.rendezvous_started", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None


# ─────────────────────────────────────────────────────────────────────────────
# Client side (agent) — probe + hole punch
# ─────────────────────────────────────────────────────────────────────────────

class _PunchClientProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        super().__init__()
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.probe_ack_endpoint: Optional[Tuple[str, int]] = None
        self.punch_ack_received = asyncio.Event()
        self._expected_token: Optional[str] = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return

        if msg.get("type") == "probe_ack":
            ep = msg.get("endpoint")
            if isinstance(ep, list) and len(ep) == 2:
                self.probe_ack_endpoint = (ep[0], int(ep[1]))

        elif msg.get("type") == "punch":
            # Peer's punch reached us — echo an ack back so they know it landed.
            token = msg.get("token", "")
            reply = json.dumps({"type": "punch_ack", "token": token}).encode("utf-8")
            if self.transport:
                self.transport.sendto(reply, addr)

        elif msg.get("type") == "punch_ack":
            if msg.get("token") == self._expected_token:
                self.punch_ack_received.set()

    def expect_token(self, token: str) -> None:
        self._expected_token = token


async def discover_public_endpoint(
    transport: asyncio.DatagramTransport,
    protocol: "_PunchClientProtocol",
    rendezvous_host: str,
    rendezvous_port: int,
    token: str,
    timeout: float = 5.0,
) -> Optional[Tuple[str, int]]:
    """
    Phase 1: ask the relay's PunchRendezvousServer what our own public
    UDP endpoint looks like from the outside. Returns None on timeout —
    this is a best-effort discovery, never fatal to the caller, since the
    relay path keeps working with or without a P2P attempt.

    Takes an already-open (transport, protocol) pair from
    open_punch_socket() rather than opening its own — the subsequent
    hole-punch attempt MUST reuse this exact same local socket, since
    many NATs key their mapping on (local socket, remote target); probing
    from one socket and punching from another can invalidate whatever
    mapping the probe just established.
    """
    protocol.probe_ack_endpoint = None
    probe = json.dumps({"type": "probe", "token": token}).encode("utf-8")
    transport.sendto(probe, (rendezvous_host, rendezvous_port))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if protocol.probe_ack_endpoint:
            return protocol.probe_ack_endpoint
        await asyncio.sleep(0.05)
    return None


async def attempt_p2p_upgrade(
    rendezvous_host: str,
    rendezvous_port: int,
    token: str,
    peer_endpoint: Tuple[str, int],
) -> Tuple[bool, Optional[asyncio.DatagramTransport]]:
    """
    Full Phase 1 + Phase 2 flow in one call: open a socket, discover our
    own public endpoint (informational — logged, not required for the
    punch itself to proceed, since we already have the peer's endpoint
    to punch toward), then attempt the punch.

    Returns (success, transport). On success, the caller owns `transport`
    and can use it to send/receive raw UDP datagrams directly to the
    peer — closing it is the caller's responsibility once the P2P path
    is no longer needed. On failure, transport is still returned (closed
    or not is the caller's call) but success=False means "keep using the
    relay, don't try to read from this."
    """
    transport, protocol, local_port = await open_punch_socket()

    own_endpoint = await discover_public_endpoint(
        transport, protocol, rendezvous_host, rendezvous_port, token
    )
    log.info(
        "hole_punch.own_endpoint_discovered" if own_endpoint else "hole_punch.own_endpoint_unknown",
        local_port=local_port,
        observed=own_endpoint,
    )

    attempt = PunchAttempt(transport, protocol)
    success = await attempt.run(peer_endpoint, token)
    return success, transport


class PunchAttempt:
    """
    Phase 2: given the peer's reported public endpoint, attempt a direct
    UDP hole punch to it.

    Honest expectations, not aspirational ones:
      - Works reliably when both peers are behind cone-type NAT (the
        common case for most home routers).
      - Fails — by design of the network, not a bug here — when either
        peer is behind symmetric NAT (common on some carrier/mobile
        networks and some corporate firewalls) or a firewall that drops
        unsolicited inbound UDP outright.
      - There is no way to know in advance which case you're in; that's
        exactly why this always has a deadline and always reports
        failure cleanly rather than hanging, so the caller can keep using
        the relay path without ever blocking on this.

    This class does not touch the relay path or fall back to anything
    itself — it only ever reports success/failure. Falling back to relay
    is trivial in this codebase: it's simply "don't switch away from what
    every frame sender already does," since nothing in Phase 1/2 changes
    the existing relay-based send path at all.
    """

    def __init__(self, local_transport: asyncio.DatagramTransport, protocol: _PunchClientProtocol):
        self._transport = local_transport
        self._protocol = protocol

    async def run(
        self, peer_endpoint: Tuple[str, int], token: str, attempts: int = 8, interval: float = 0.25
    ) -> bool:
        """
        Fires `attempts` UDP packets at peer_endpoint, spaced `interval`
        apart, while the same socket listens for the peer doing the same
        thing back. Returns True the moment a punch_ack is received,
        False if none arrives before attempts run out.
        """
        self._protocol.expect_token(token)
        self._protocol.punch_ack_received.clear()

        packet = json.dumps({"type": "punch", "token": token}).encode("utf-8")

        for i in range(attempts):
            if self._protocol.punch_ack_received.is_set():
                break
            try:
                self._transport.sendto(packet, peer_endpoint)
            except OSError as e:
                log.debug("hole_punch.send_failed", attempt=i, error=str(e))
            try:
                await asyncio.wait_for(self._protocol.punch_ack_received.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

        success = self._protocol.punch_ack_received.is_set()
        log.info("hole_punch.attempt_result", peer=peer_endpoint, success=success)
        return success


async def open_punch_socket() -> Tuple[asyncio.DatagramTransport, _PunchClientProtocol, int]:
    """
    Opens the local UDP socket used for both the STUN-lite probe and the
    subsequent punch attempt — same 5-tuple for both, which matters: many
    NATs create a mapping keyed by (local socket, remote target), so the
    punch needs to originate from the exact socket that was just probed,
    not a fresh one, or the "learned" public port may not even be valid
    for the punch.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _PunchClientProtocol, local_addr=("0.0.0.0", 0)
    )
    local_port = transport.get_extra_info("socket").getsockname()[1]
    return transport, protocol, local_port
