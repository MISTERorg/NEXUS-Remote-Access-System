"""
utils/heartbeat.py
------------------
Async heartbeat manager — sends periodic pings and detects dead connections.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Coroutine, Dict, Optional

from utils.logger import get_logger

log = get_logger("nexus.heartbeat")


class HeartbeatManager:
    """
    Tracks liveness of WebSocket connections.
    If a peer misses `max_misses` consecutive heartbeats it is considered dead.
    """

    def __init__(
        self,
        interval: int = 30,
        max_misses: int = 3,
        on_dead: Optional[Callable[[str], Coroutine]] = None,
    ):
        self.interval = interval
        self.max_misses = max_misses
        self.on_dead = on_dead
        self._last_pong: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None

    def register(self, peer_id: str) -> None:
        self._last_pong[peer_id] = time.monotonic()

    def unregister(self, peer_id: str) -> None:
        self._last_pong.pop(peer_id, None)

    def record_pong(self, peer_id: str) -> None:
        self._last_pong[peer_id] = time.monotonic()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            deadline = time.monotonic() - (self.interval * self.max_misses)
            dead = [pid for pid, ts in self._last_pong.items() if ts < deadline]
            for pid in dead:
                log.warning("heartbeat.dead", peer_id=pid)
                self.unregister(pid)
                if self.on_dead:
                    await self.on_dead(pid)
