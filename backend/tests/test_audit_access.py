"""AUDIT_REPORT F-01: registration gate and document-level access control in retrieval.

The retrieval tests use the *_test database: they add two documents (one restricted to a group) and
remove them afterwards. Query embeddings are stubbed, so no model is loaded.
"""

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings, settings
from backend.app.database import SessionLocal, get_db, init_db
from backend.app.main import app
from backend.app.models.chunk import DocumentChunk
from backend.app.models.document import Document
from backend.app.models.user import User
from backend.app.security.access import retrieval_groups
from backend.app.security.auth import create_access_token, hash_password
from backend.app.services.rag_service import rag_service
from backend.app.services.retrieval_service import retrieval_service

DIM = settings.EMBEDDING_DIM
UNIT = [1.0] + [0.0] * (DIM - 1)
SECRET_TOKEN = f"zqxsecret{uuid.uuid4().hex[:8]}"
OPEN_TOKEN = f"zqxopen{uuid.uuid4().hex[:8]}"
RESTRICTED_GROUP = "finance-test"


@pytest.fixture(scope="module")
def two_documents():
    init_db()
    db = SessionLocal()
    # An aborted earlier run (killed process, failed teardown) leaves its audit_* documents in the shared
    # *_test database; they would take part in this module's retrieval queries. conftest guarantees this is
    # a *_test database, so removing only these fixture-owned rows is safe.
    db.query(Document).filter(Document.filename.like("audit\\_%", escape="\\")).delete(synchronize_session=False)
    db.commit()
    open_doc = Document(filename=f"audit_open_{uuid.uuid4().hex}.pdf", title="Open", file_hash="0" * 64,
                        allowed_groups=["all"], source_tier="official_sap")
    secret_doc = Document(filename=f"audit_secret_{uuid.uuid4().hex}.pdf", title="Secret", file_hash="1" * 64,
                          allowed_groups=[RESTRICTED_GROUP], source_tier="official_sap")
    self_doc = Document(filename=f"audit_self_{uuid.uuid4().hex}.pdf", title="Self-authored", file_hash="2" * 64,
                        allowed_groups=["all"], source_tier="self_authored")
    db.add_all([open_doc, secret_doc, self_doc])
    db.flush()
    for doc, token in ((open_doc, OPEN_TOKEN), (secret_doc, SECRET_TOKEN), (self_doc, "zqxself" + uuid.uuid4().hex[:8])):
        db.add(DocumentChunk(document_id=doc.id, document_name=doc.filename, chunk_index=0, page_number=1,
                             section="Posting", embedding=UNIT,
                             embedding_model=settings.EMBEDDING_MODEL, embedding_version=settings.EMBEDDING_VERSION,
                             chunk_text=f"{token} Convergent Invoicing posting area configuration for FI-CA {token}."))
    db.commit()
    retrieval_service.invalidate_cache()
    yield {"open": open_doc.filename, "secret": secret_doc.filename, "self": self_doc.filename,
           "secret_id": secret_doc.id, "db": db}
    db.query(Document).filter(Document.id.in_([open_doc.id, secret_doc.id, self_doc.id])).delete(synchronize_session=False)
    db.commit()
    db.close()
    retrieval_service.invalidate_cache()


def _search(db, user, document_filter=None):
    with patch("backend.app.services.retrieval_service.embedding_service.embed_text", return_value=UNIT):
        rows = retrieval_service.hybrid_search(db, f"{SECRET_TOKEN} {OPEN_TOKEN} posting area", top_k=20,
                                               allowed_groups=retrieval_groups(user), document_filter=document_filter)
    return {r["document_name"] for r in rows}


def regular(groups=("all",)):
    return SimpleNamespace(id="u-regular", is_admin=False, role="consultant", groups=list(groups))


def test_f01_user_outside_the_group_cannot_retrieve_the_chunk(two_documents):
    found = _search(two_documents["db"], regular())
    assert two_documents["open"] in found
    assert two_documents["secret"] not in found


def test_f01_group_members_and_admins_can_retrieve_it(two_documents):
    assert two_documents["secret"] in _search(two_documents["db"], regular(groups=(RESTRICTED_GROUP,)))
    assert two_documents["secret"] in _search(
        two_documents["db"], SimpleNamespace(id="a", is_admin=True), two_documents["secret"]
    )


def test_f08_self_authored_documents_are_excluded_before_ranking_by_default(two_documents):
    with patch.object(settings, "INCLUDE_SELF_AUTHORED_DOCS", False):
        assert two_documents["self"] not in _search(two_documents["db"], regular(), two_documents["self"])
    with patch.object(settings, "INCLUDE_SELF_AUTHORED_DOCS", True):
        assert two_documents["self"] in _search(two_documents["db"], regular(), two_documents["self"])
    with patch.object(settings, "INCLUDE_SELF_AUTHORED_DOCS", False):
        assert two_documents["self"] not in _search(two_documents["db"], regular(), two_documents["self"])


def test_f07_retrieval_excludes_embeddings_from_another_model_version(two_documents):
    db = two_documents["db"]
    chunk = db.query(DocumentChunk).filter(DocumentChunk.document_name == two_documents["open"]).first()
    original_version = chunk.embedding_version
    try:
        chunk.embedding_version = "older-incompatible-version"
        db.commit()
        retrieval_service.invalidate_cache()
        with patch("backend.app.services.retrieval_service.embedding_service.embed_text", return_value=UNIT):
            results = retrieval_service.hybrid_search(
                db, "unrelated-vector-only-query", top_k=5, document_filter=two_documents["open"],
                allowed_groups=retrieval_groups(regular()),
            )
        assert results == []
    finally:
        chunk.embedding_version = original_version
        db.commit()
        retrieval_service.invalidate_cache()
def test_f01_restricted_chunk_never_reaches_the_answer_or_citations(two_documents):
    question = f"How do I configure the posting area {SECRET_TOKEN} step by step?"
    with patch("backend.app.services.retrieval_service.embedding_service.embed_text", return_value=UNIT), \
            patch.object(settings, "AUTO_MULTI_SOURCE_FALLBACK", False), \
            patch.object(settings, "WEB_FALLBACK_ENABLED", False), \
            patch.object(settings, "ANSWER_STYLE_LLM_CLASSIFIER_ENABLED", False), \
            patch("backend.app.services.rag_service.llm_service.generate_answer",
                  side_effect=lambda q, evidence, **kw: " ".join(p.get("chunk_text", "") for p in evidence)), \
            patch("backend.app.services.rag_service.llm_service.repair_answer",
                  side_effect=lambda **kw: kw["candidate_answer"]):
        result = rag_service.process_query(two_documents["db"], question, user=regular())
    assert SECRET_TOKEN not in result["answer"]
    assert all(c.get("document") != two_documents["secret"] for c in result["citations"])


def test_f01_retrieval_groups_semantics():
    assert retrieval_groups(SimpleNamespace(is_admin=True)) is None
    assert retrieval_groups(regular(groups=(" finance ", ""))) == frozenset({"finance"})
    assert retrieval_groups(None) == frozenset({settings.DEFAULT_ACCESS_GROUP})


# --- registration gate -----------------------------------------------------------------------------

class RegistrationSession:
    def __init__(self):
        self.added = []

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return None

    def add(self, obj):
        obj.id, obj.token_version, obj.created_at = str(uuid.uuid4()), 0, datetime(2026, 1, 1)
        # Column defaults the real database fills in on insert and `refresh` reloads.
        obj.preferred_language = "en"
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        pass


@pytest.fixture
def reg_client():
    session = RegistrationSession()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        yield client, session


BODY = {"email": "newuser@example.com", "name": "New User", "password": "long-enough-password-1"}


# Owner decision (2026-11-04, F-01): individuals sign up themselves with no code; only organisations
# use the Super-Admin-issued activation code (docs/ORGANISATION_PORTAL.md).

def test_f01_registration_is_closed_by_default():
    assert Settings.model_fields["REGISTRATION_ENABLED"].default is False


def test_f01_individual_sign_up_needs_no_code_and_does_not_sign_in(reg_client):
    """Owner decision 2026-11-04: sign-up only creates the account; a session starts at /login."""
    client, session = reg_client
    resp = client.post("/api/auth/register", json=BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert "access_token" not in body and "token_type" not in body
    assert body["email"] == BODY["email"]
    assert body["groups"] == [settings.DEFAULT_ACCESS_GROUP] and body["is_admin"] is False
    assert len(session.added) == 1


def test_f01_a_leftover_invite_code_setting_no_longer_blocks_sign_up(reg_client):
    client, session = reg_client
    with patch.object(settings, "INVITE_CODE", "old-invite-code-value", create=True):
        assert client.post("/api/auth/register", json=BODY).status_code == 201


def test_f01_registration_can_still_be_switched_off(reg_client):
    client, session = reg_client
    with patch.object(settings, "REGISTRATION_ENABLED", False):
        resp = client.post("/api/auth/register", json=BODY)
    assert resp.status_code == 403 and not session.added


@pytest.mark.parametrize("password, status", [("abc1234", 422), ("abcd1234", 201)])
def test_sign_up_password_minimum_is_8_characters(reg_client, password, status):
    client, _ = reg_client
    assert client.post("/api/auth/register", json={**BODY, "password": password}).status_code == status


def test_sign_up_never_creates_an_organisation_or_admin_account(reg_client):
    client, session = reg_client
    resp = client.post("/api/auth/register", json={
        **BODY, "account_type": "organisation", "organization_id": "org-1", "org_role": "owner",
        "is_admin": True, "role": "admin",
    })
    assert resp.status_code == 201
    created = session.added[0]
    assert created.account_type == "individual"
    assert created.organization_id is None and created.org_role is None
    assert created.is_admin is False and created.role == "consultant"


# --- admin endpoints for groups ----------------------------------------------------------------------

@pytest.fixture
def admin_and_user():
    init_db()
    db = SessionLocal()
    admin = User(email=f"acl_admin_{uuid.uuid4().hex[:8]}@example.com", name="ACL Admin",
                 password_hash=hash_password(uuid.uuid4().hex), is_admin=True, role="admin")
    member = User(email=f"acl_user_{uuid.uuid4().hex[:8]}@example.com", name="ACL User",
                  password_hash=hash_password(uuid.uuid4().hex), is_admin=False, role="consultant")
    db.add_all([admin, member])
    db.commit()
    tokens = {u: create_access_token({"sub": u.id, "ver": u.token_version}) for u in (admin, member)}
    yield db, admin, member, tokens
    db.query(User).filter(User.id.in_([admin.id, member.id])).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_f01_admin_sets_user_and_document_groups(two_documents, admin_and_user):
    db, admin, member, tokens = admin_and_user
    admin_headers = {"Authorization": f"Bearer {tokens[admin]}"}
    member_headers = {"Authorization": f"Bearer {tokens[member]}"}
    with TestClient(app) as client:
        assert client.patch(f"/api/admin/users/{member.id}/groups", json={"groups": ["x"]},
                            headers=member_headers).status_code == 403
        resp = client.patch(f"/api/admin/users/{member.id}/groups", json={"groups": [" finance-test ", "all"]},
                            headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["groups"] == ["finance-test", "all"]

        doc_id = two_documents["secret_id"]
        assert client.patch(f"/api/documents/{doc_id}/access", json={"allowed_groups": ["all"]},
                            headers=member_headers).status_code == 403
        with patch.object(retrieval_service, "invalidate_cache") as invalidate:
            resp = client.patch(f"/api/documents/{doc_id}/access", json={"allowed_groups": [RESTRICTED_GROUP, "audit"]},
                                headers=admin_headers)
        assert resp.status_code == 200 and resp.json()["allowed_groups"] == [RESTRICTED_GROUP, "audit"]
        invalidate.assert_called_once()
