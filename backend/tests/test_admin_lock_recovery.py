"""Server-shell recovery for a locked administrator (backend/scripts/unlock_admin.py)."""
import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "unlock_admin.py"
spec = importlib.util.spec_from_file_location("unlock_admin", SCRIPT)
unlock_admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(unlock_admin)


def _locked_admin():
    return SimpleNamespace(
        login_locked_at=datetime(2026, 11, 9, 12, 0, 0),
        failed_login_attempts=5,
        login_unlock_key_hash="a" * 64,
        login_unlock_key_expires_at=datetime(2026, 11, 9, 12, 15, 0),
        token_version=3,
    )


def test_recovery_clears_lock_counter_and_pending_key_and_revokes_sessions():
    user = _locked_admin()
    assert unlock_admin.clear_lockout(user) is True
    assert user.login_locked_at is None
    assert user.failed_login_attempts == 0
    assert user.login_unlock_key_hash is None and user.login_unlock_key_expires_at is None
    assert user.token_version == 4


def test_recovery_on_an_unlocked_account_still_revokes_sessions_and_reports_no_lock():
    user = SimpleNamespace(login_locked_at=None, failed_login_attempts=0, login_unlock_key_hash=None,
                           login_unlock_key_expires_at=None, token_version=0)
    assert unlock_admin.clear_lockout(user) is False
    assert user.token_version == 1


def test_script_never_prints_or_stores_a_key_or_password():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "token_urlsafe" not in source and "getpass" not in source
    assert "print(key" not in source and "print(password" not in source


def test_web_doc_fetcher_rejects_non_http_schemes_without_opening_them():
    from backend.app.retrieval.sap_web_doc_ingest import SAPWebDocIngestService

    service = SAPWebDocIngestService.__new__(SAPWebDocIngestService)  # no DB/snapshot setup is needed
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"):
        assert service.fetch_url_content(url) == (None, None, "Only http and https URLs can be fetched.")


def test_failed_login_log_lines_do_not_contain_the_typed_email():
    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "auth.py").read_text(encoding="utf-8")
    assert "invalid credentials for email=" not in source
    assert "unauthorized user email=" not in source
