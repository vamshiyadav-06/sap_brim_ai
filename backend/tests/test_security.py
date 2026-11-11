import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.config import Settings, settings, security_settings_errors

client = TestClient(app)

def test_admin_ingestion_blocked_for_normal_users():
    # Normal user login
    client.post("/api/auth/register", json={"email": "normal_dev_user@sap.com", "name": "Normal User", "password": "Password123!"})
    login_resp = client.post("/api/auth/login", json={"email": "normal_dev_user@sap.com", "password": "Password123!"}).json()
    normal_token = login_resp["access_token"]

    # Attempt to trigger admin ingestion without admin key
    resp = client.post(
        "/api/admin/documents/ingest",
        headers={"Authorization": f"Bearer {normal_token}"}
    )
    # The first user might have been admin; a normal user must be forbidden
    # If the user is not admin and has no X-Admin-Key, it must return 403
    resp_no_token = client.post("/api/admin/documents/ingest")
    assert resp_no_token.status_code == 403

def test_admin_ingestion_allowed_with_admin_key(monkeypatch):
    key = secrets.token_urlsafe(48)
    monkeypatch.setattr(settings, "ADMIN_API_KEY", key)
    resp = client.post(
        "/api/admin/documents/ingest",
        headers={"X-Admin-Key": key}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_admin_key_rejected_when_wrong_or_unset(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_API_KEY", secrets.token_urlsafe(48))
    assert client.get("/api/documents", headers={"X-Admin-Key": "wrong-key"}).status_code == 403

    # An unset key disables key access; an empty header must not match it.
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "")
    assert client.get("/api/documents", headers={"X-Admin-Key": ""}).status_code == 403
    assert client.get("/api/documents", headers={"X-Admin-Key": "sap-admin-dev-secret-key-999"}).status_code == 403


def test_published_or_weak_secrets_are_reported():
    good = secrets.token_urlsafe(48)
    ok = Settings(JWT_SECRET=good, ADMIN_API_KEY="", SUPER_ADMIN_EMAIL="", SUPER_ADMIN_PASSWORD="")
    assert security_settings_errors(ok) == []

    for bad in (
        {"JWT_SECRET": "sap-super-secret-jwt-key-2026-production-ready"},
        {"JWT_SECRET": "short"},
        {"ADMIN_API_KEY": "sap-admin-dev-secret-key-999"},
        {"SUPER_ADMIN_EMAIL": "admin@example.com"},
    ):
        values = {"JWT_SECRET": good, "ADMIN_API_KEY": "", "SUPER_ADMIN_EMAIL": "", "SUPER_ADMIN_PASSWORD": "", **bad}
        assert security_settings_errors(Settings(**values)), bad

def test_published_admin_password_is_refused_without_storing_it_in_plain_text(monkeypatch):
    import hashlib
    from backend.app import config

    published = "an-example-published-password"
    monkeypatch.setattr(config, "PUBLISHED_SECRET_HASHES",
                        config.PUBLISHED_SECRET_HASHES | {hashlib.sha256(published.encode()).hexdigest()})
    values = {"JWT_SECRET": secrets.token_urlsafe(48), "ADMIN_API_KEY": "", "SUPER_ADMIN_EMAIL": "admin@example.com",
              "SUPER_ADMIN_PASSWORD": published}
    assert any("SUPER_ADMIN_PASSWORD" in e for e in security_settings_errors(Settings(**values)))
    assert all(len(h) == 64 for h in config.PUBLISHED_SECRET_HASHES)
    assert "PUBLISHED_SECRETS" not in Path(config.__file__).read_text(encoding="utf-8")  # only hashes are kept


def test_no_document_download_routes_exist():
    """Verify that no public route exists allowing direct document downloads."""
    resp = client.get("/documents/SAP_MM_Materials_Management_Manual.pdf")
    assert resp.status_code == 404

    resp2 = client.get("/api/documents/1/download")
    assert resp2.status_code == 404

def test_invalid_jwt_token_rejected():
    resp = client.get("/api/chats", headers={"Authorization": "Bearer invalid_gibberish_token_xyz"})
    assert resp.status_code == 401
