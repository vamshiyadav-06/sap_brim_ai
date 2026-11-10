"""
Request rate limits and login lockout (AUDIT_REPORT F-06).

Keys are a client IP (resolved by ProxyHeadersMiddleware, which trusts forwarding headers only from
TRUSTED_PROXIES) or a verified user id, and for logins also the lower-cased target email. The raw
Authorization header is never part of a key: an attacker could send a fresh value per request to get
a fresh bucket.

Storage is Redis (through the `limits` package) when REDIS_URL is set, so every API worker shares the
same counters; otherwise an in-memory store with expiry and a key cap, which is per-process.
"""

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Deque, Optional

from backend.app.config import settings

logger = logging.getLogger(__name__)


class MemoryBackend:
    """Sliding-window counters in process memory. Expired buckets are evicted and the number of keys
    is capped, so a flood of distinct keys cannot grow memory without bound."""

    def __init__(self, max_keys: int = 50_000):
        self.max_keys = max_keys
        self._buckets: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._windows: dict = {}
        self._lock = threading.Lock()
        self._last_sweep = 0.0

    def _prune(self, key: str, window: float, now: float) -> Deque[float]:
        bucket = self._buckets.get(key)
        if bucket is None:
            return deque()
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        return bucket

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 30:
            return
        self._last_sweep = now
        for key in [k for k, b in self._buckets.items() if not b or now - b[-1] >= self._windows.get(k, 0)]:
            self._buckets.pop(key, None)
            self._windows.pop(key, None)

    def hit(self, key: str, limit: int, window: float) -> bool:
        """Record one event; False when the key is already at its limit (the event is not recorded)."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            bucket = self._prune(key, window, now)
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            self._buckets[key] = bucket
            self._windows[key] = window
            self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                oldest, _ = self._buckets.popitem(last=False)
                self._windows.pop(oldest, None)
            return True

    def count(self, key: str, window: float) -> int:
        with self._lock:
            return len(self._prune(key, window, time.monotonic()))

    def clear(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
            self._windows.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._windows.clear()

    def __len__(self) -> int:
        return len(self._buckets)


class LimitsRedisBackend:
    """Shared counters in Redis via the `limits` package (moving window)."""

    def __init__(self, redis_url: str):
        from limits import RateLimitItemPerSecond
        from limits.storage import storage_from_string
        from limits.strategies import MovingWindowRateLimiter

        self._item = RateLimitItemPerSecond
        self._storage = storage_from_string(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        if not self._storage.check():
            raise ConnectionError("Redis rate-limit storage is not reachable")
        self._limiter = MovingWindowRateLimiter(self._storage)

    def _item_for(self, limit: int, window: float):
        return self._item(limit, max(1, int(window)))

    def hit(self, key: str, limit: int, window: float) -> bool:
        return self._limiter.hit(self._item_for(limit, window), key)

    def count(self, key: str, window: float) -> int:
        stats = self._limiter.get_window_stats(self._item_for(10_000, window), key)
        return 10_000 - stats.remaining

    def clear(self, key: str) -> None:
        self._limiter.clear(self._item_for(10_000, settings.LOGIN_LOCKOUT_WINDOW_SECONDS), key)

    def reset(self) -> None:
        self._storage.reset()

    def check(self) -> bool:
        return bool(self._storage.check())


def _build_backend():
    if settings.REDIS_URL:
        try:
            return LimitsRedisBackend(settings.REDIS_URL)
        except Exception as exc:  # missing package or unreachable Redis: fail safe to per-process limits
            if settings.ENVIRONMENT.lower() == "production":
                # Do not silently claim shared rate limiting when production Redis is misconfigured.
                raise RuntimeError("Configured Redis rate-limit storage is unavailable") from exc
            logger.error("Rate limiter could not use REDIS_URL (%s); falling back to in-memory limits", type(exc).__name__)
    return MemoryBackend(max_keys=settings.RATE_LIMIT_MAX_KEYS)


class RateLimiter:
    def __init__(self, backend=None):
        self.backend = backend or _build_backend()

    @property
    def shared(self) -> bool:
        return not isinstance(self.backend, MemoryBackend)

    def check(self) -> bool:
        """Report whether configured shared storage is currently reachable."""
        if isinstance(self.backend, MemoryBackend):
            return not bool(settings.REDIS_URL)
        try:
            return bool(self.backend.check())
        except Exception as exc:
            logger.error("Redis rate-limit health check failed (%s)", type(exc).__name__)
            return False

    def allow(self, scope: str, identity: str, limit: int, window: float = 60) -> bool:
        if not settings.RATE_LIMIT_ENABLED:
            return True
        return self.backend.hit(f"{scope}:{identity}", limit, window)

    # Login lockout: too many failed passwords for one account within the window locks that account.
    @staticmethod
    def _account_key(email: str) -> str:
        return f"login-fail:{(email or '').strip().lower()}"

    def account_locked(self, email: str) -> bool:
        if not settings.RATE_LIMIT_ENABLED:
            return False
        return self.backend.count(self._account_key(email), settings.LOGIN_LOCKOUT_WINDOW_SECONDS) >= settings.LOGIN_MAX_FAILURES

    def record_login_failure(self, email: str) -> None:
        if settings.RATE_LIMIT_ENABLED:
            self.backend.hit(self._account_key(email), 10_000, settings.LOGIN_LOCKOUT_WINDOW_SECONDS)

    def clear_login_failures(self, email: str) -> None:
        self.backend.clear(self._account_key(email))

    def reset(self) -> None:
        self.backend.reset()


rate_limiter = RateLimiter()


def client_ip(request) -> str:
    """Peer address after ProxyHeadersMiddleware; forwarding headers are honoured only from TRUSTED_PROXIES."""
    return request.client.host if request.client else "unknown"


def warn_if_not_shared() -> Optional[str]:
    if settings.ENVIRONMENT.lower() == "production" and not rate_limiter.shared:
        message = ("Rate limits and login lockout are per-process (REDIS_URL not set or unusable); "
                   "with several API workers an attacker gets one budget per worker.")
        logger.warning(message)
        return message
    return None
