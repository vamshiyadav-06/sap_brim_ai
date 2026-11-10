"""Role boundaries: who may call the Super Admin and organisation-administration APIs.

Complements the existing tenant-isolation tests (test_org_knowledge, test_org_accounts_and_admin,
test_projects, test_auth_and_isolation), which cover cross-organisation data access, by checking that
every privileged route rejects callers of a lower role: anonymous, individual, organisation member,
organisation administrator and organisation owner. An organisation owner is *not* a Super Admin.

Runs against the *_test database (conftest.py). Placeholder ids never match a row, so a request that
passes the guard answers 404/422 instead of 401/403; the tests only assert that the guard stops it.
"""

import pytest
from fastapi.testclient import TestClient

from backend.app.database import SessionLocal
from backend.app.main import app
from backend.app.services.security_controls import registration_enabled
from backend.tests.org_helpers import OrgFixtures, headers_for

client = TestClient(app)
X = "no-such-id"

# (method, path, json body) for every Super Admin route outside the document manager.
SUPER_ADMIN_ROUTES = [
    ("GET", "/api/admin/security-controls", None),
    ("PUT", "/api/admin/security-controls/registration", {"registration_enabled": True}),
    ("GET", "/api/admin/login-lockouts", None),
    ("POST", f"/api/admin/users/{X}/login-unlock-key", {}),
    ("GET", "/api/admin/users", None),
    ("PATCH", f"/api/admin/users/{X}/groups", {"groups": ["all"]}),
    ("GET", "/api/admin/audit-events", None),
    ("GET", "/api/admin/system-health", None),
    ("POST", "/api/admin/documents/ingest", {}),
    ("GET", "/api/admin/organisations", None),
    ("POST", "/api/admin/organisations", {}),
    ("GET", f"/api/admin/organisations/{X}", None),
    ("POST", f"/api/admin/organisations/{X}/approve", {}),
    ("POST", f"/api/admin/organisations/{X}/reject", {}),
    ("POST", f"/api/admin/organisations/{X}/activation-codes", {}),
    ("PATCH", f"/api/admin/organisations/{X}/status", {"status": "suspended"}),
    ("GET", "/api/admin/individual-users", None),
    ("POST", "/api/admin/individual-users", {}),
    ("PATCH", f"/api/admin/users/{X}/status", {"is_active": False}),
    ("GET", "/api/rag-inspect/system-config", None),
    ("GET", "/api/rag-inspect/documents", None),
    ("POST", "/api/rag-inspect/similarity-search", {"query": "posting area"}),
]

# The document manager answers 403 (not 401) to anyone who is not a Super Admin.
DOCUMENT_ROUTES = [
    ("GET", "/api/documents", None),
    ("GET", "/api/documents/processing-status", None),
    ("POST", "/api/documents/process-all", {}),
    ("POST", f"/api/documents/{X}/process", {}),
    ("GET", f"/api/documents/{X}", None),
    ("PATCH", f"/api/documents/{X}/access", {"allowed_groups": ["all"]}),
    ("PATCH", f"/api/documents/{X}/tier", {"source_tier": "unknown"}),
    ("DELETE", f"/api/documents/{X}", None),
]

# Any organisation account may call these; individual accounts may not.
ORG_MEMBER_ROUTES = [
    ("GET", "/api/org/me", None),
    ("GET", "/api/org/knowledge", None),
    ("GET", "/api/org/knowledge/categories", None),
    ("GET", "/api/org/knowledge/shared-message-ids", None),
    ("POST", "/api/org/knowledge", {"message_id": X}),
    ("GET", f"/api/org/knowledge/{X}", None),
    ("PATCH", f"/api/org/knowledge/{X}", {"category": "ci"}),
    ("DELETE", f"/api/org/knowledge/{X}", None),
    ("POST", f"/api/org/knowledge/{X}/like", None),
    ("DELETE", f"/api/org/knowledge/{X}/like", None),
    ("POST", f"/api/org/knowledge/{X}/comments", {"body": "hi"}),
    ("DELETE", f"/api/org/knowledge/{X}/comments/{X}", None),
]

# Organisation owners and administrators only; a plain member may not.
ORG_ADMIN_ROUTES = [
    ("GET", "/api/org/admin/members", None),
    ("POST", "/api/org/admin/members", {"name": "N", "email": "n@example.com", "password": "Pw-for-boundary-1"}),
    ("PATCH", f"/api/org/admin/members/{X}", {"is_active": False}),
    ("POST", f"/api/org/admin/members/{X}/reset-password", {"password": "Pw-for-boundary-2"}),
    ("GET", "/api/org/admin/activity", None),
    ("PATCH", "/api/org/admin/organisation", {}),
]


def _call(method, path, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


@pytest.fixture
def fx():
    fixtures = OrgFixtures()
    yield fixtures
    fixtures.cleanup()


@pytest.fixture
def people(fx):
    org = fx.organisation()
    return {
        "individual": fx.user(),
        "org_member": fx.user(org=org, role="member"),
        "org_admin": fx.user(org=org, role="admin"),
        "org_owner": fx.user(org=org, role="owner"),
        "super_admin": fx.user(is_admin=True),
    }


def _label(route):
    return f"{route[0]} {route[1].replace(X, '{id}')}"


@pytest.mark.parametrize("route", SUPER_ADMIN_ROUTES, ids=_label)
def test_super_admin_routes_reject_every_other_role(route, people):
    method, path, body = route
    assert _call(method, path, body).status_code in (401, 403), "anonymous caller reached a Super Admin route"
    for role in ("individual", "org_member", "org_admin", "org_owner"):
        status = _call(method, path, body, headers_for(people[role].id)).status_code
        assert status == 403, f"{role} got {status} on {_label(route)}"


@pytest.mark.parametrize("route", DOCUMENT_ROUTES, ids=_label)
def test_document_manager_rejects_every_non_super_admin(route, people):
    method, path, body = route
    assert _call(method, path, body).status_code in (401, 403)
    for role in ("individual", "org_member", "org_admin", "org_owner"):
        status = _call(method, path, body, headers_for(people[role].id)).status_code
        assert status in (401, 403), f"{role} got {status} on {_label(route)}"


def test_the_guards_are_not_vacuous_a_super_admin_gets_through(people):
    """Positive control: the 403s above come from the role check, not from a broken route."""
    headers = headers_for(people["super_admin"].id)
    assert _call("GET", "/api/admin/security-controls", None, headers).status_code == 200
    assert _call("GET", "/api/admin/users", None, headers).status_code == 200
    assert _call("GET", "/api/documents", None, headers).status_code == 200


def test_denied_registration_change_leaves_the_setting_unchanged(people):
    with SessionLocal() as db:
        before = registration_enabled(db)
    for role in ("individual", "org_member", "org_admin", "org_owner"):
        assert _call("PUT", "/api/admin/security-controls/registration", {"registration_enabled": not before},
                     headers_for(people[role].id)).status_code == 403
    with SessionLocal() as db:
        assert registration_enabled(db) is before


@pytest.mark.parametrize("route", ORG_MEMBER_ROUTES + ORG_ADMIN_ROUTES, ids=_label)
def test_organisation_routes_reject_anonymous_and_individual_accounts(route, people):
    method, path, body = route
    assert _call(method, path, body).status_code == 401
    status = _call(method, path, body, headers_for(people["individual"].id)).status_code
    assert status == 403, f"an individual account got {status} on {_label(route)}"
    # A Super Admin account is not an organisation member either.
    status = _call(method, path, body, headers_for(people["super_admin"].id)).status_code
    assert status == 403, f"a Super Admin (no organisation) got {status} on {_label(route)}"


@pytest.mark.parametrize("route", ORG_ADMIN_ROUTES, ids=_label)
def test_organisation_administration_rejects_plain_members(route, people):
    method, path, body = route
    status = _call(method, path, body, headers_for(people["org_member"].id)).status_code
    assert status == 403, f"a plain member got {status} on {_label(route)}"


def test_organisation_administration_is_open_to_owners_and_admins(people):
    """Positive control for the member/admin split."""
    for role in ("org_owner", "org_admin"):
        assert _call("GET", "/api/org/admin/members", None, headers_for(people[role].id)).status_code == 200
    assert _call("GET", "/api/org/me", None, headers_for(people["org_member"].id)).status_code == 200
