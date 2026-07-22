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


# ---------------------------------------------------------------------------
# utils/ratelimit.py  (included here as a second class for compactness)
# ---------------------------------------------------------------------------

"""
utils/ratelimit.py
------------------
In-memory token-bucket rate limiter. Thread/async-safe.
"""

from collections import defaultdict


class RateLimiter:
    """
    Token-bucket rate limiter keyed by an arbitrary string (IP, user ID, etc.).

    Args:
        max_calls: Maximum calls allowed in the window.
        window_seconds: Rolling window in seconds.
    """

    def __init__(self, max_calls: int = 100, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        # { key: [timestamp, ...] }
        self._buckets: Dict[str, list] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        bucket = self._buckets[key]
        # Evict expired timestamps
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)
        if len(bucket) < self.max_calls:
            bucket.append(now)
            return True
        return False

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)
