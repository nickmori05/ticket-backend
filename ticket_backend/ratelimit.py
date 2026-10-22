"""Per-token request rate limits, kept in this process's memory.

Each token has a bucket holding up to its limit of requests, refilled continuously at that many per minute, so a
token can burst up to its limit and then continues at the steady rate. Buckets are not shared between processes.
"""

from dataclasses import dataclass
import math
import os
import threading
import time


DEFAULT_RATE_LIMIT = 120
MAX_RATE_LIMIT = 100_000


@dataclass(frozen=True)
class Decision:
    allowed: bool
    # Whole seconds until the next request would be allowed; 0 when this one was.
    retry_after: int
    # True for the first refusal after an allowed request, so callers can report a token once per episode.
    newly_limited: bool = False


@dataclass
class _Bucket:
    capacity: int
    available: float
    updated: float
    limited: bool = False


def require_rate_limit(value: int, name: str = "Rate limit") -> int:
    if type(value) is not int or not 0 <= value <= MAX_RATE_LIMIT:
        raise ValueError(f"{name} must be a whole number of requests per minute from 0 to {MAX_RATE_LIMIT}.")
    return value


def rate_limit_from_environment() -> int:
    """Read TICKETS_RATE_LIMIT, the default requests per minute for each token; 0 turns limits off."""
    value = os.environ.get("TICKETS_RATE_LIMIT", "").strip()
    if not value:
        return DEFAULT_RATE_LIMIT
    try:
        parsed = int(value)
    except ValueError:
        parsed = None
    return require_rate_limit(parsed, "TICKETS_RATE_LIMIT")


class RateLimiter:
    """Token buckets keyed by name. Safe to call from several threads at once."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int) -> Decision:
        """Spend one request from this key's bucket, which holds up to limit requests and refills limit per minute."""
        if limit == 0:
            return Decision(True, 0)
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None or bucket.capacity != limit:
                bucket = self._buckets[key] = _Bucket(limit, float(limit), now)
            bucket.available = min(limit, bucket.available + (now - bucket.updated) * limit / 60)
            bucket.updated = now
            if bucket.available >= 1:
                bucket.available -= 1
                bucket.limited = False
                return Decision(True, 0)
            newly_limited, bucket.limited = not bucket.limited, True
            return Decision(False, max(1, math.ceil((1 - bucket.available) * 60 / limit)), newly_limited)
