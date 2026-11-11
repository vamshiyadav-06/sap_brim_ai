"""Configuration and failure-mode tests for optional shared Redis rate limits."""

import pytest

from backend.app.config import settings
from backend.app.security import ratelimit


def test_configured_redis_backend_is_selected_and_checked(monkeypatch):
    calls = []

    class FakeRedisBackend:
        def __init__(self, url):
            # Mirrors LimitsRedisBackend: constructing it verifies connectivity and raises if unreachable.
            calls.append(("init", url))
            self.check()

        def check(self):
            calls.append(("check", True))
            return True

    monkeypatch.setattr(settings, "REDIS_URL", "redis://:fake@redis:6379/0")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(ratelimit, "LimitsRedisBackend", FakeRedisBackend)

    backend = ratelimit._build_backend()

    assert isinstance(backend, FakeRedisBackend)
    assert calls == [("init", "redis://:fake@redis:6379/0"), ("check", True)]


def test_production_does_not_silently_fall_back_when_redis_is_unavailable(monkeypatch):
    class UnavailableRedisBackend:
        def __init__(self, _url):
            raise ConnectionError("unreachable")

    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(ratelimit, "LimitsRedisBackend", UnavailableRedisBackend)

    with pytest.raises(RuntimeError, match="Configured Redis rate-limit storage is unavailable"):
        ratelimit._build_backend()


def test_development_keeps_safe_in_memory_fallback_and_reports_unshared_state(monkeypatch):
    class UnavailableRedisBackend:
        def __init__(self, _url):
            raise ConnectionError("unreachable")

    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(ratelimit, "LimitsRedisBackend", UnavailableRedisBackend)
    monkeypatch.setattr(ratelimit, "settings", settings)

    limiter = ratelimit.RateLimiter()

    assert isinstance(limiter.backend, ratelimit.MemoryBackend)
    assert limiter.shared is False
    assert limiter.check() is False


def test_unconfigured_development_storage_is_healthy_and_local(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "")
    limiter = ratelimit.RateLimiter(backend=ratelimit.MemoryBackend())

    assert limiter.check() is True


class _DownRedis:
    """A shared store that fails every call, like a Redis that stopped after startup."""

    def __init__(self):
        self.calls = 0

    def _fail(self, *_args):
        self.calls += 1
        raise ConnectionError("redis went away")

    hit = count = clear = reset = check = _fail


def test_redis_outage_degrades_to_per_process_limits_instead_of_failing_requests(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    down = _DownRedis()
    limiter = ratelimit.RateLimiter(backend=down)

    assert [limiter.allow("answer", "ip:1.2.3.4", 2, 60) for _ in range(3)] == [True, True, False]
    assert down.calls == 1, "Redis is not retried on every request while it is down"
    assert limiter.check() is False  # /health still reports the outage


def test_login_lockout_keeps_working_while_redis_is_down(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(settings, "LOGIN_MAX_FAILURES", 3)
    limiter = ratelimit.RateLimiter(backend=_DownRedis())

    for _ in range(3):
        limiter.record_login_failure("a@x.com")
    assert limiter.account_locked("a@x.com") is True
    limiter.clear_login_failures("a@x.com")
    assert limiter.account_locked("a@x.com") is False


def test_redis_is_used_again_after_the_retry_pause(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    class Flaky:
        up = False

        def hit(self, *_a):
            if not self.up:
                raise ConnectionError("down")
            return True

    flaky = Flaky()
    limiter = ratelimit.RateLimiter(backend=flaky)
    assert limiter.allow("auth", "ip:9", 5, 60) is True  # served by the fallback
    flaky.up = True
    limiter._redis_retry_at = 1.0  # the pause is over (monotonic clock is far past 1)
    assert limiter.allow("auth", "ip:9", 5, 60) is True
    assert limiter._redis_retry_at == 0.0
