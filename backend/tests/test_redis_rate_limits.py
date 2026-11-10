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
