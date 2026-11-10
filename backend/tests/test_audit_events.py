"""AUDIT_REPORT F-25: security audit trail. Uses the *_test database."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.api.documents import authenticate_document_operator
from backend.app.config import settings
from backend.app.database import SessionLocal, init_db
from backend.app.main import app
from backend.app.models.audit_event import AuditEvent
from backend.app.models.chat import Chat
from backend.app.models.document import Document
from backend.app.models.message import Message
from backend.app.models.user import User
from backend.app.security.auth import create_access_token, hash_password

PASSWORD = "audit-trail-password-1"


@pytest.fixture(scope="module")
def people():
    init_db()
    db = SessionLocal()
    admin = User(email=f"audit_admin_{uuid.uuid4().hex[:8]}@example.com", name="Audit Admin",
                 password_hash=hash_password(PASSWORD), is_admin=True, role="admin")
    member = User(email=f"audit_user_{uuid.uuid4().hex[:8]}@example.com", name="Audit User",
                  password_hash=hash_password(PASSWORD), is_admin=False, role="consultant")
    db.add_all([admin, member])
    db.commit()
    yield db, admin, member
    db.query(User).filter(User.id.in_([admin.id, member.id])).delete(synchronize_session=False)
    db.commit()
    db.close()


def events(db, **filters):
    db.expire_all()
    query = db.query(AuditEvent)
    for key, value in filters.items():
        query = query.filter(getattr(AuditEvent, key) == value)
    return query.order_by(AuditEvent.created_at.desc()).all()


def bearer(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': user.id, 'ver': user.token_version})}"}


def test_login_success_failure_and_logout_are_recorded(people):
    db, admin, member = people
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"email": member.email, "password": "wrong-password-xx"}).status_code == 401
        ok = client.post("/api/auth/login", json={"email": member.email, "password": PASSWORD},
                         headers={"User-Agent": "audit-test-agent"})
        assert ok.status_code == 200
        client.post("/api/auth/logout", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})
    failure = events(db, action="login", outcome="failure", target=member.email)[0]
    success = events(db, action="login", outcome="success", actor_id=member.id)[0]
    assert failure.actor_type == "anonymous" and failure.ip
    assert success.actor_type == "user" and success.user_agent == "audit-test-agent"
    assert events(db, action="logout", actor_id=member.id)


def test_admin_login_and_non_admin_attempt_are_recorded(people):
    db, admin, member = people
    with TestClient(app) as client:
        assert client.post("/api/auth/admin-login", json={"email": admin.email, "password": PASSWORD}).status_code == 200
        assert client.post("/api/auth/admin-login", json={"email": member.email, "password": PASSWORD}).status_code == 401
    assert events(db, action="admin_login", outcome="success", actor_id=admin.id)
    assert events(db, action="admin_login", outcome="denied", actor_id=member.id)


def test_document_delete_and_admin_key_use_are_recorded(people):
    db, admin, _ = people
    doc = Document(filename=f"audit_delete_{uuid.uuid4().hex}.pdf", title="To delete", file_hash="2" * 64)
    db.add(doc)
    db.commit()
    doc_id = doc.id
    key = "k" * 40
    with patch.object(settings, "ADMIN_API_KEY", key), TestClient(app) as client:
        resp = client.delete(f"/api/documents/{doc_id}", headers={"X-Admin-Key": key})
    assert resp.status_code == 200
    deleted = events(db, action="document_delete", target=doc_id)[0]
    assert deleted.actor_type == "admin_key" and deleted.actor_id is None
    assert events(db, action="admin_key_used", target=f"/api/documents/{doc_id}")


def test_share_create_and_revoke_are_recorded(people):
    db, _, member = people
    chat = Chat(user_id=member.id, title="Audit share")
    db.add(chat)
    db.flush()
    question = Message(chat_id=chat.id, role="user", content="How is the posting area configured?")
    db.add(question)
    db.commit()
    with TestClient(app) as client:
        assert client.post(f"/api/chats/{chat.id}/messages/{question.id}/share", headers=bearer(member)).status_code == 200
        assert client.delete(f"/api/chats/{chat.id}/messages/{question.id}/share", headers=bearer(member)).status_code == 204
    assert events(db, action="share_create", target=question.id, actor_id=member.id)
    assert events(db, action="share_revoke", target=question.id, actor_id=member.id)


def test_audit_events_endpoint_is_admin_only_and_filterable(people):
    db, admin, member = people
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"email": member.email, "password": "wrong-password-yy"})
        assert client.get("/api/admin/audit-events", headers=bearer(member)).status_code == 403
        assert client.get("/api/admin/audit-events").status_code == 401
        with patch.object(settings, "ADMIN_API_KEY", "k" * 40):
            assert client.get("/api/admin/audit-events", headers={"X-Admin-Key": "k" * 40}).status_code == 401
        resp = client.get("/api/admin/audit-events", params={"action": "login", "outcome": "failure", "limit": 5},
                          headers=bearer(admin))
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1 and len(body["events"]) <= 5
    assert all(e["action"] == "login" and e["outcome"] == "failure" for e in body["events"])
    # No credential value or credential-bearing field may be exposed. Fixed reason codes such as
    # "invalid_unlock_key_or_password" are labels, not secrets, so the bare word is allowed.
    assert "wrong-password-yy" not in resp.text
    for event in body["events"]:
        assert not {"password", "password_hash", "unlock_key", "token"} & set(event)
        assert not {"password", "password_hash", "unlock_key", "token"} & set(event.get("details") or {})


def test_audit_write_failure_never_breaks_the_request():
    from backend.app.services import audit_log
    with patch.object(audit_log, "SessionLocal", side_effect=RuntimeError("db down")):
        audit_log.record("login", "failure", actor_type="anonymous")  # must not raise
