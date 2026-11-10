"""AUDIT_REPORT Phase 2 regressions: authentication and access control. Offline (fake DB session)."""

import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.database import get_db
from backend.app.main import app
from backend.app.security.auth import hash_password
from backend.app.security.ratelimit import MemoryBackend, rate_limiter

PASSWORD = "correct-horse-battery-1"
PASSWORD_HASH = hash_password(PASSWORD)


class FakeQuery:
    def __init__(self, user):
        self._user = user

    def filter(self, *args, **kwargs):
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self._user


class FakeSession:
    """Just enough of a SQLAlchemy session for the auth endpoints."""

    def __init__(self, users):
        self.users = {**{u.email: u for u in users}, **{u.id: u for u in users}}
        self.last_email = None
        self.added = []

    def query(self, *args):
        return self

    def filter(self, *conditions):
        # User.email == "<value>": SQLAlchemy keeps the bound value on the right side.
        self.last_email = getattr(getattr(conditions[0], "right", None), "value", None)
        return self

    def with_for_update(self):
        return self

    def first(self):
        return self.users.get(self.last_email)

    def commit(self):
        pass

    def add(self, row):
        # A successful sign-in stores a refresh-token row (models/refresh_token.py).
        self.added.append(row)

    def execute(self, *args, **kwargs):
        # Sign-in and /me top up the token wallet with UPDATE statements (services/token_wallet.py);
        # rowcount 0 = nothing was due, so no commit/refresh follows.
        return SimpleNamespace(rowcount=0)


def make_user(email, is_admin=False):
    return SimpleNamespace(id=str(uuid.uuid4()), email=email, name="Test User", password_hash=PASSWORD_HASH,
                           is_admin=is_admin, role="admin" if is_admin else "consultant", token_version=0,
                           created_at=__import__("datetime").datetime(2026, 1, 1), failed_login_attempts=0,
                           login_locked_at=None, login_unlock_key_hash=None, login_unlock_key_expires_at=None,
                           account_type="individual", is_active=True, must_change_password=False)


@pytest.fixture
def client():
    session = FakeSession([make_user("alice@example.com"), make_user("bob@example.com"),
                           make_user("admin@example.com", is_admin=True)])
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


def login(client, email, password, **headers):
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=headers)


# --- F-06: rate limiting and lockout -------------------------------------------------------------

def test_f06_random_authorization_header_does_not_reset_login_limit(client):
    limit = settings.LOGIN_RATE_LIMIT_PER_MINUTE
    statuses = [
        login(client, f"user{i}@example.com", "wrong-password-123", Authorization=f"Bearer random-{uuid.uuid4()}").status_code
        for i in range(limit + 2)
    ]
    assert statuses[:limit] == [401] * limit
    assert statuses[limit:] == [429, 429], statuses


def test_f06_spoofed_forwarded_for_is_ignored_from_untrusted_peer(client):
    limit = settings.LOGIN_RATE_LIMIT_PER_MINUTE
    statuses = [
        login(client, f"user{i}@example.com", "wrong-password-123", **{"X-Forwarded-For": f"203.0.113.{i}"}).status_code
        for i in range(limit + 1)
    ]
    assert statuses[-1] == 429


def test_f06_account_locks_after_repeated_failures_even_with_correct_password(client):
    for _ in range(settings.LOGIN_MAX_FAILURES - 1):
        assert login(client, "alice@example.com", "wrong-password-123").status_code == 401
    assert login(client, "alice@example.com", "wrong-password-123").status_code == 423
    assert login(client, "alice@example.com", PASSWORD).status_code == 423
    assert login(client, "bob@example.com", PASSWORD).status_code == 200, "other accounts are unaffected"


def test_f06_admin_login_failures_count_toward_lockout(client):
    for _ in range(settings.LOGIN_MAX_FAILURES):
        resp = client.post("/api/auth/admin-login", json={"email": "admin@example.com", "password": "wrong-password-1"})
        assert resp.status_code == (401 if _ < settings.LOGIN_MAX_FAILURES - 1 else 423)
    resp = client.post("/api/auth/admin-login", json={"email": "admin@example.com", "password": PASSWORD})
    assert resp.status_code == 423


def test_f06_successful_login_clears_failure_count(client):
    for _ in range(settings.LOGIN_MAX_FAILURES - 1):
        login(client, "alice@example.com", "wrong-password-123")
    assert login(client, "alice@example.com", PASSWORD).status_code == 200
    for _ in range(settings.LOGIN_MAX_FAILURES - 1):
        login(client, "alice@example.com", "wrong-password-123")
    assert login(client, "alice@example.com", PASSWORD).status_code == 200


def test_f06_unknown_accounts_lock_too_so_lockout_does_not_reveal_existence(client):
    statuses = [login(client, "nobody@example.com", "wrong-password-123").status_code
                for _ in range(settings.LOGIN_MAX_FAILURES + 1)]
    assert statuses == [401] * (settings.LOGIN_MAX_FAILURES - 1) + [423, 423]


def test_f06_locked_account_requires_correct_one_time_admin_key_and_password(client):
    session = app.dependency_overrides[get_db]()
    user = session.users["alice@example.com"]
    for _ in range(settings.LOGIN_MAX_FAILURES):
        login(client, "alice@example.com", "wrong-password-123")
    key = "one-time-key-which-is-long-enough"
    user.login_unlock_key_hash = hashlib.sha256(key.encode()).hexdigest()
    user.login_unlock_key_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD,
                                               "unlock_key": "wrong-key"}).status_code == 401
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD,
                                                    "unlock_key": key})
    assert response.status_code == 200
    assert user.login_locked_at is None and user.login_unlock_key_hash is None


def test_f06_memory_backend_caps_keys_and_expires_buckets():
    backend = MemoryBackend(max_keys=3)
    for i in range(10):
        assert backend.hit(f"k{i}", 1, 60)
    assert len(backend) == 3
    assert backend.hit("k9", 1, 60) is False
    expired_at = backend._buckets["k9"][-1] + 61
    with patch("backend.app.security.ratelimit.time.monotonic", return_value=expired_at):
        assert backend.hit("k9", 1, 60) is True, "the window expired"


def test_f06_rate_limit_can_be_disabled_explicitly():
    with patch.object(settings, "RATE_LIMIT_ENABLED", False):
        assert all(rate_limiter.allow("t", "x", 1) for _ in range(5))


# --- F-39: constant-time login, quiet registration ------------------------------------------------

@pytest.mark.parametrize("email", ["alice@example.com", "nobody@example.com"])
def test_f39_login_runs_one_bcrypt_check_whether_or_not_the_account_exists(client, email):
    import bcrypt
    real = bcrypt.checkpw
    with patch("backend.app.security.auth.bcrypt.checkpw", side_effect=real) as checkpw:
        assert login(client, email, "wrong-password-123").status_code == 401
    assert checkpw.call_count == 1


def test_f39_duplicate_registration_hashes_and_gives_a_generic_error(client):
    with patch("backend.app.api.auth.hash_password", wraps=hash_password) as hasher:
        resp = client.post("/api/auth/register",
                           json={"email": "alice@example.com", "name": "Alice", "password": "another-password-9"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Registration could not be completed."
    assert hasher.call_count == 1, "the duplicate path pays the same hashing cost"


# --- F-40: token claims ---------------------------------------------------------------------------

def _forge(claims, secret=None, algorithm="HS256"):
    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    base = {"sub": "u1", "ver": 0, "iat": now, "exp": now + timedelta(minutes=5), "jti": "x",
            "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE}
    base.update(claims)
    return pyjwt.encode(base, secret or settings.JWT_SECRET, algorithm=algorithm)


def test_f40_token_has_issuer_audience_jti_and_no_email(client):
    import jwt as pyjwt
    resp = login(client, "alice@example.com", PASSWORD)
    token = resp.json()["access_token"]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert "email" not in claims
    assert claims["iss"] == settings.JWT_ISSUER and claims["aud"] == settings.JWT_AUDIENCE
    second = pyjwt.decode(login(client, "alice@example.com", PASSWORD).json()["access_token"],
                          options={"verify_signature": False})
    assert claims["jti"] != second["jti"]


@pytest.mark.parametrize("claims", [{"aud": "some-other-service"}, {"iss": "someone-else"}, {"jti": None}])
def test_f40_tokens_with_wrong_issuer_or_audience_are_rejected(claims):
    from backend.app.security.auth import decode_access_token
    import jwt as pyjwt
    token = _forge({k: v for k, v in claims.items() if v is not None})
    if claims.get("jti", "set") is None:
        token = _forge({}, None)
        payload = pyjwt.decode(token, options={"verify_signature": False})
        payload.pop("jti")
        token = pyjwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
    with pytest.raises(pyjwt.PyJWTError):
        decode_access_token(token)


def test_f40_wrong_audience_gets_401_from_the_api(client):
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {_forge({'aud': 'other'})}"})
    assert resp.status_code == 401


def test_f40_unsigned_none_algorithm_token_is_rejected():
    import jwt as pyjwt
    from backend.app.security.auth import decode_access_token
    token = pyjwt.encode({"sub": "u1", "ver": 0}, key=None, algorithm="none")
    with pytest.raises(pyjwt.PyJWTError):
        decode_access_token(token)


@pytest.mark.parametrize("algorithm", ["none", "RS256", "HS1"])
def test_f40_startup_rejects_non_hmac_jwt_algorithm(algorithm):
    from backend.app.config import Settings, security_settings_errors
    s = Settings(JWT_SECRET="x" * 40, JWT_ALGORITHM=algorithm, ADMIN_API_KEY="", SUPER_ADMIN_EMAIL="",
                 SUPER_ADMIN_PASSWORD="")
    assert any("JWT_ALGORITHM" in e for e in security_settings_errors(s))


# --- F-26: API responses are never cached and can never render ------------------------------------

@pytest.mark.parametrize("path", ["/api/auth/me", "/", "/api/shared/prompts/unknown"])
def test_f26_api_responses_are_no_store_and_locked_down(client, path):
    resp = client.get(path)
    assert resp.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_f26_streams_are_no_store_too(client):
    from backend.app.api import rag as rag_api
    with patch.object(rag_api.rag_service, "stream_query", side_effect=lambda *a, **k: iter([{"type": "done", "answer": "x"}])):
        token = login(client, "alice@example.com", PASSWORD).json()["access_token"]
        resp = client.post("/api/rag/query/stream", json={"query": "q"}, headers={"Authorization": f"Bearer {token}"})
    assert resp.headers["Cache-Control"] == "no-store"


# --- F-42: public surface ------------------------------------------------------------------------

def test_f42_health_is_status_only_for_anonymous_callers(client):
    body = client.get("/health").json()
    assert body["status"] in ("healthy", "unhealthy")
    assert "knowledge_base" not in body and "version" not in body and "service" not in body


def test_f42_system_health_details_are_admin_only(client):
    assert client.get("/api/admin/system-health").status_code == 401
    member = login(client, "alice@example.com", PASSWORD).json()["access_token"]
    assert client.get("/api/admin/system-health", headers={"Authorization": f"Bearer {member}"}).status_code == 403


def test_f42_api_docs_are_disabled_in_production():
    import os
    import subprocess
    import sys
    probe = ("from fastapi.testclient import TestClient; from backend.app.main import app; "
             "c = TestClient(app, base_url='https://testserver'); "
             "print(app.docs_url, app.openapi_url, c.get('/docs').status_code, c.get('/openapi.json').status_code)")
    env = {**os.environ, "ENVIRONMENT": "production"}
    out = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.split()[-4:] == ["None", "None", "404", "404"]


# --- F-41: HTTPS check behind a TLS-terminating proxy ---------------------------------------------

def _proxied_app(trusted):
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    return ProxyHeadersMiddleware(app, trusted_hosts=trusted)


def test_f41_https_from_a_trusted_proxy_is_accepted_in_production():
    # No `with`: these tests need the request path only, not production startup checks.
    proxied = TestClient(_proxied_app(["testclient"]), base_url="http://testserver")
    with patch.object(settings, "ENVIRONMENT", "production"):
        assert proxied.get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 200
        assert proxied.get("/").status_code == 400, "plain HTTP is still refused"


def test_f41_forwarded_proto_from_an_untrusted_peer_is_ignored():
    direct = TestClient(_proxied_app(["10.9.8.7"]), base_url="http://testserver")
    with patch.object(settings, "ENVIRONMENT", "production"):
        assert direct.get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 400


def test_f41_health_probe_works_over_plain_http_in_production():
    plain = TestClient(app, base_url="http://testserver")
    with patch.object(settings, "ENVIRONMENT", "production"):
        assert plain.get("/health").status_code in (200, 503)
