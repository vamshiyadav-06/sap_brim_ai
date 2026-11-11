"""Deployment preflight (backend/app/preflight.py) and the tester-account script (backend/scripts/create_user.py)."""

import importlib.util
import uuid

import pytest

from backend.app.config import ROOT_DIR, Settings
from backend.app.database import SessionLocal
from backend.app.models.user import User
from backend.app.preflight import check_database, check_settings, main
from backend.app.security.auth import verify_password

PRIVATE = {
    "JWT_SECRET": "j" * 48,
    "ADMIN_API_KEY": "",
    "SUPER_ADMIN_EMAIL": "admin@staging.example.com",
    "SUPER_ADMIN_PASSWORD": "a-private-admin-password",
    "DATABASE_URL": "postgresql://sap_brim_ai:Q2v8_private-db@postgres:5432/sap_brim_ai",
    "ALLOWED_ORIGINS": "https://staging.example.com",
    "ENVIRONMENT": "production",
    "REGISTRATION_ENABLED": False,
    "RATE_LIMIT_ENABLED": True,
    "LLM_ALLOWED_PROVIDERS": "groq,gemini,openrouter",
    "GROQ_API_KEY": "", "GEMINI_API_KEY": "", "OPENROUTER_API_KEY": "or-test-key", "LLM_API_KEY": "",
    "WEB_FALLBACK_ENABLED": False, "WEB_SEARCH_API_KEY": "",
}


def _settings(**overrides):
    return Settings(_env_file=None, **{**PRIVATE, **overrides})


def test_a_complete_staging_configuration_passes(monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "basic_auth")
    monkeypatch.setenv("STAGING_BASIC_AUTH_USER", "stage")
    monkeypatch.setenv("STAGING_BASIC_AUTH_HASH", "hashed-password")
    monkeypatch.setenv("WEB_BIND", "127.0.0.1")
    report = check_settings(_settings(), expect_staging=True)
    assert not report.failed, report.lines
    assert any("LLM providers with a key: openrouter" in line for line in report.lines)


@pytest.mark.parametrize("overrides, expected", [
    ({"JWT_SECRET": "short"}, "JWT_SECRET"),
    ({"DATABASE_URL": "postgresql://sap_brim_ai:change-me-app-password@postgres:5432/sap_brim_ai"}, "template database password"),
    ({"ALLOWED_ORIGINS": "https://localhost"}, "ALLOWED_ORIGINS"),
    ({"OPENROUTER_API_KEY": ""}, "no allowed LLM provider has an API key"),
    ({"LLM_ALLOWED_PROVIDERS": "groq,acme"}, "unknown providers: acme"),
    ({"DATABASE_URL": "sqlite:///x.db"}, "DATABASE_URL must be postgresql"),
])
def test_unsafe_or_incomplete_settings_fail(overrides, expected):
    report = check_settings(_settings(**overrides))
    assert report.failed
    assert any(expected in line for line in report.lines), report.lines


def test_staging_requires_closed_sign_up_and_production_mode():
    open_signup = check_settings(_settings(REGISTRATION_ENABLED=True), expect_staging=True)
    assert open_signup.failed and any("REGISTRATION_ENABLED" in line for line in open_signup.lines)
    dev_mode = check_settings(_settings(ENVIRONMENT="development", ALLOWED_ORIGINS="http://localhost:5173"),
                              expect_staging=True)
    assert dev_mode.failed and any("ENVIRONMENT" in line for line in dev_mode.lines)


def test_report_never_contains_secret_values():
    settings = _settings(JWT_SECRET="short-secret-value")
    lines = "\n".join(check_settings(settings).lines + check_database(
        settings, connect=lambda s: (_ for _ in ()).throw(RuntimeError(s.DATABASE_URL))).lines)
    for value in ("short-secret-value", "Q2v8_private-db", "or-test-key", "a-private-admin-password"):
        assert value not in lines


def test_main_exit_codes_and_database_check(capsys, monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "basic_auth")
    monkeypatch.setenv("STAGING_BASIC_AUTH_USER", "stage")
    monkeypatch.setenv("STAGING_BASIC_AUTH_HASH", "hashed-password")
    monkeypatch.setenv("WEB_BIND", "127.0.0.1")
    assert main(["--expect-staging"], settings_factory=_settings) == 0
    assert "PREFLIGHT PASSED" in capsys.readouterr().out
    assert main([], settings_factory=lambda: _settings(JWT_SECRET="x")) == 1
    assert "PREFLIGHT FAILED" in capsys.readouterr().out
    unreachable = check_database(_settings(), connect=lambda s: (_ for _ in ()).throw(OSError("down")))
    assert unreachable.failed and "database not reachable (OSError)" in unreachable.lines[0]


def _create_user_module():
    spec = importlib.util.spec_from_file_location("create_user", ROOT_DIR / "backend" / "scripts" / "create_user.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_user_script_creates_a_usable_individual_account_once():
    module = _create_user_module()
    email = f"Tester-{uuid.uuid4().hex[:10]}@Staging.Example.com"
    db = SessionLocal()
    try:
        user = module.create_individual_user(db, email, "  Staging Tester ", "tester-pass-1")
        assert user.email == email.lower() and user.name == "Staging Tester"
        assert not user.is_admin and user.account_type == "individual" and user.is_active
        assert verify_password("tester-pass-1", user.password_hash)
        with pytest.raises(module.AccountError, match="already exists"):
            module.create_individual_user(db, email.upper(), "Other", "tester-pass-2")
        with pytest.raises(module.AccountError, match="password"):
            module.create_individual_user(db, f"x{uuid.uuid4().hex[:8]}@example.com", "Name", "short")
    finally:
        db.query(User).filter(User.email == email.lower()).delete()
        db.commit()
        db.close()


def test_production_mode_requires_rate_limits_and_a_shared_store():
    ok = check_settings(_settings(REDIS_URL="redis://:x@redis:6379/0"), expect_production=True)
    assert not ok.failed, ok.lines

    bad = check_settings(_settings(ENVIRONMENT="development", RATE_LIMIT_ENABLED=False, REDIS_URL=""),
                         expect_production=True)
    text = " ".join(bad.lines)
    assert bad.failed
    assert "ENVIRONMENT must be 'production'" in text
    assert "RATE_LIMIT_ENABLED must be True" in text
    assert "REDIS_URL is not set" in text


def test_production_mode_warns_about_open_sign_up(monkeypatch):
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    report = check_settings(_settings(REGISTRATION_ENABLED=True, REDIS_URL="redis://:x@redis:6379/0"),
                            expect_production=True)
    assert not report.failed
    assert any(line.startswith("WARN") and "REGISTRATION_ENABLED" in line for line in report.lines)
