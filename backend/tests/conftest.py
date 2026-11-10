"""
Test database isolation.

Many tests create users, chats and documents through SessionLocal, so the suite must
never run against the live database. Tests use "<DATABASE_URL database>_test" on the
same PostgreSQL server (or TEST_DATABASE_URL when set). Create it as a clone of the
live database, which also gives retrieval tests the real indexed knowledge base:

    docker exec sap_assistant_postgres createdb -U sap_assistant -O sap_brim_ai -T sap_brim_ai sap_brim_ai_test

This module runs before any test module imports backend.app.database, so the engine
is bound to the test database from the start.
"""

import os

from sqlalchemy.engine import make_url

from backend.app.config import settings


def _resolve_test_database_url() -> str:
    explicit = os.getenv("TEST_DATABASE_URL")
    if explicit:
        return explicit
    if not settings.DATABASE_URL:
        return ""
    url = make_url(settings.DATABASE_URL)
    if url.database.endswith("_test"):
        return settings.DATABASE_URL
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


TEST_DATABASE_URL = _resolve_test_database_url()
if TEST_DATABASE_URL:
    if not make_url(TEST_DATABASE_URL).database.endswith("_test"):
        raise RuntimeError(
            "Refusing to run tests against a database whose name does not end in '_test' "
            f"({make_url(TEST_DATABASE_URL).database}). Tests write data."
        )
    settings.DATABASE_URL = TEST_DATABASE_URL
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Public self-registration is off by default (AUDIT_REPORT F-01); many tests register throwaway accounts.
# Tests of the default patch it back to False.
settings.REGISTRATION_ENABLED = True

# No background index rebuilds racing the tests that ingest, delete or patch retrieval; tests of the
# warm-up enable it explicitly.
settings.RETRIEVAL_CACHE_WARMUP = False


import pytest


@pytest.fixture(scope="session", autouse=True)
def _test_database_at_head_revision():
    """Bring the *_test database to the latest Alembic revision once per run. Most tests use SessionLocal
    or a TestClient without its lifespan, so nothing else would add columns from new migrations (e.g. the
    users columns of 0006_organisations) before they write rows. Migrations are idempotent."""
    if TEST_DATABASE_URL:
        from sqlalchemy.exc import OperationalError
        from backend.app.database import DatabaseConnectionError, run_migrations
        try:
            run_migrations()
        except (OperationalError, DatabaseConnectionError):
            pass  # tests that need PostgreSQL report the connection problem themselves
    yield


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    """A dependency override left behind by one test (e.g. a fake current user) must not
    authenticate requests in later tests; that masked auth regressions in test_security.py."""
    yield
    from backend.app.main import app
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate limits stay enabled in tests, but every test starts with empty counters, so the shared
    TestClient address cannot exhaust a budget across unrelated tests."""
    from backend.app.security.ratelimit import rate_limiter
    rate_limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _reset_model_cooldowns():
    """A fake 404/429 in one test must not make later tests skip that model name."""
    from backend.app.services.llm_service import model_cooldowns
    model_cooldowns.clear()
    yield
    model_cooldowns.clear()


# Modules that test the strict evidence-only behavior (verify -> repair -> guard -> notice, citation-only
# prompts). Reference mode is the default; the strict behavior stays available with
# REFERENCE_MODE_ENABLED=False, and these modules keep guarding it. test_reference_mode.py covers the default.
_STRICT_GROUNDING_MODULES = {
    "test_adaptive_rag_pipeline", "test_answer_style_router", "test_audit_rag_pipeline",
    "test_bp_block_regression", "test_llm_web_fallback_chain", "test_provider_search_fallbacks",
    "test_solution_design_answers",
}


@pytest.fixture(autouse=True)
def _reference_mode_per_module(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in _STRICT_GROUNDING_MODULES:
        monkeypatch.setattr(settings, "REFERENCE_MODE_ENABLED", False)
    yield


@pytest.fixture(autouse=True)
def _intent_analyzer_offline(monkeypatch):
    """The LLM intent analyzer is the first call of every question. Tests run offline, so it reports 'model
    unavailable' (None) and the rule-based routing answers; test_intent_analysis.py supplies fake replies."""
    from backend.app.services.llm_service import llm_service
    monkeypatch.setattr(llm_service, "analyze_question_intent", lambda prompt: None)
    yield


@pytest.fixture
def open_scope(monkeypatch):
    """Lets a test exercise the open-scope (SAP_ONLY_SCOPE off) paths that answer non-SAP questions."""
    from backend.app.config import settings
    monkeypatch.setattr(settings, "SAP_ONLY_SCOPE", False)
