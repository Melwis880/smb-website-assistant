"""In-memory abuse limits. Per process, reset on restart: a first line of defence, not a WAF.

Production should still put a reverse proxy with rate limiting in front (see README).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone


class SlidingWindowLimiter:
    """At most `limit` events per `window` seconds per key. limit <= 0 means unlimited."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 10_000):
        self.limit, self.window, self.max_keys = limit, window_seconds, max_keys
        self._events: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = self._events[key] = deque()
            while len(self._events) > self.max_keys:  # bound memory: forget the least recently seen key
                self._events.popitem(last=False)
        self._events.move_to_end(key)
        while events and now - events[0] >= self.window:
            events.popleft()
        return events

    def allowed(self, key: str, now: float | None = None) -> bool:
        """Would one more event be allowed? Does not record anything."""
        if self.limit <= 0:
            return True
        with self._lock:
            return len(self._recent(key, time.time() if now is None else now)) < self.limit

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record an event if allowed. Returns False (and records nothing) when over the limit."""
        if self.limit <= 0:
            return True
        now = time.time() if now is None else now
        with self._lock:
            events = self._recent(key, now)
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class DailyBudget:
    """At most `limit` spends per UTC day across the whole process. limit <= 0 means unlimited."""

    def __init__(self, limit: int):
        self.limit = limit
        self._day = ""
        self._used = 0
        self._lock = threading.Lock()

    def try_spend(self, now: datetime | None = None) -> bool:
        if self.limit <= 0:
            return True
        day = (now or datetime.now(timezone.utc)).date().isoformat()
        with self._lock:
            if day != self._day:
                self._day, self._used = day, 0
            if self._used >= self.limit:
                return False
            self._used += 1
            return True
