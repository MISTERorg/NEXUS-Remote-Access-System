"""
utils/ratelimit.py
------------------
In-memory token-bucket rate limiter. Thread/async-safe for a single process.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List


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
        self._buckets: Dict[str, List[float]] = defaultdict(list)

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
