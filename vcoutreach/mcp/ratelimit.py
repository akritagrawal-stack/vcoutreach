"""In-memory sliding-window rate limits for MCP tools (reset on restart).

Thread-safe, with an injectable clock so tests never sleep.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable


class RateLimited(Exception):
    """Raised when a limit is exceeded. ``retry_after_seconds`` is a whole number >= 1."""

    def __init__(self, retry_after_seconds: int, scope: str = "") -> None:
        super().__init__(f"Rate limited; retry in {retry_after_seconds} seconds.")
        self.retry_after_seconds = retry_after_seconds
        self.scope = scope


class RateLimiter:
    """Allow at most ``limit`` events per ``window`` seconds for each key."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] | None = None) -> None:
        self.limit = int(limit)
        self.window = float(window)
        self._clock = clock or time.monotonic
        self._events: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        events = self._events.setdefault(key, deque())
        while events and now - events[0] >= self.window:
            events.popleft()
        return events

    def retry_after(self, key: str = "") -> int:
        """Seconds until one more event is allowed (0 when allowed now). Does not record."""
        with self._lock:
            now = self._clock()
            events = self._prune(key, now)
            if len(events) < self.limit:
                return 0
            return max(1, int(self.window - (now - events[0]) + 0.999))

    def check(self, key: str = "") -> None:
        """Record one event or raise RateLimited."""
        with self._lock:
            now = self._clock()
            events = self._prune(key, now)
            if len(events) >= self.limit:
                wait = max(1, int(self.window - (now - events[0]) + 0.999))
                raise RateLimited(wait, key)
            events.append(now)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


class ResultCache:
    """Remember the last result per key for ``ttl`` seconds."""

    def __init__(self, ttl: float, clock: Callable[[], float] | None = None) -> None:
        self.ttl = float(ttl)
        self._clock = clock or time.monotonic
        self._items: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stamp, value = item
            if self._clock() - stamp >= self.ttl:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# Module-level limiters used by tools.py (specified numbers from the MCP spec).
ENRICH_PER_PARTNER = RateLimiter(1, 60)
ENRICH_OVERALL = RateLimiter(20, 600)
SEND_TEST = RateLimiter(5, 600)
CONNECTION_CACHE = ResultCache(30)


def reset_all() -> None:
    """Clear every limiter and cache (for tests and server restarts)."""
    for limiter in (ENRICH_PER_PARTNER, ENRICH_OVERALL, SEND_TEST):
        limiter.reset()
    CONNECTION_CACHE.clear()
