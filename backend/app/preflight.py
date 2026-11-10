"""Deployment preflight: validates the backend configuration before containers are (re)started.

    python -m backend.app.preflight [--check-db] [--expect-staging]

Prints one line per check ("OK", "WARN" or "FAIL" plus the setting NAME, never a value) and exits 1 when
any check fails. scripts/staging/deploy.sh runs it inside the freshly built backend image, so a broken
.env stops the deployment before the running version is replaced.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import urlparse

from backend.app.config import Settings, security_settings_errors

PROVIDER_KEYS = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


@dataclass
class Report:
    lines: List[str] = field(default_factory=list)
    failed: bool = False

    def ok(self, message: str) -> None:
        self.lines.append(f"OK    {message}")

    def warn(self, message: str) -> None:
        self.lines.append(f"WARN  {message}")

    def fail(self, message: str) -> None:
        self.failed = True
        self.lines.append(f"FAIL  {message}")


def _fallback_keys(s: Settings, provider: str) -> List[str]:
    """Configured fallback keys of a provider (values are never reported, only how many there are)."""
    values = (getattr(s, f"{provider.upper()}_API_KEY_FALLBACK_{number}", "") for number in (1, 2, 3))
    return [value.strip() for value in values if value and value.strip()]


def _provider_key(s: Settings, provider: str) -> str:
    return getattr(s, PROVIDER_KEYS[provider], "") or s.LLM_API_KEY or next(iter(_fallback_keys(s, provider)), "")


def check_settings(s: Settings, expect_staging: bool = False) -> Report:
    report = Report()
    for problem in security_settings_errors(s):
        report.fail(problem)
    if not report.failed:
        report.ok("secrets and security settings (JWT_SECRET, ADMIN_API_KEY, SUPER_ADMIN_*, origins)")

    url = urlparse(s.DATABASE_URL or "")
    if url.scheme.startswith("postgresql") and url.hostname and url.path.strip("/"):
        report.ok(f"DATABASE_URL points at host '{url.hostname}', database '{url.path.strip('/')}'")
    else:
        report.fail("DATABASE_URL must be postgresql://user:password@host:port/database")

    if not (s.SUPER_ADMIN_EMAIL and s.SUPER_ADMIN_PASSWORD):
        report.warn("SUPER_ADMIN_EMAIL/SUPER_ADMIN_PASSWORD not set: no admin is created on first start")

    allowed = [p.strip().lower() for p in s.LLM_ALLOWED_PROVIDERS.split(",") if p.strip()]
    unknown = [p for p in allowed if p not in PROVIDER_KEYS]
    if unknown:
        report.fail(f"LLM_ALLOWED_PROVIDERS contains unknown providers: {', '.join(unknown)}")
    usable = [p for p in allowed if p in PROVIDER_KEYS and _provider_key(s, p)]
    if usable:
        report.ok("LLM providers with a key: " + ", ".join(
            f"{p} (+{len(_fallback_keys(s, p))} fallback)" if _fallback_keys(s, p) else p for p in usable))
    else:
        report.fail("no allowed LLM provider has an API key: every answer would be 'unavailable'")
    if s.LLM_REQUEST_DEADLINE_SECONDS < 30:
        report.warn("LLM_REQUEST_DEADLINE_SECONDS below 30 s cuts off most Scholar answers")
    if s.WEB_FALLBACK_ENABLED and not s.WEB_SEARCH_API_KEY:
        report.warn("WEB_FALLBACK_ENABLED without WEB_SEARCH_API_KEY: no web evidence will be used")

    if s.ENVIRONMENT.lower() != "production":
        (report.fail if expect_staging else report.warn)(
            "ENVIRONMENT is not 'production': API docs are public and HTTPS is not enforced")
    if expect_staging and s.REGISTRATION_ENABLED:
        report.fail("REGISTRATION_ENABLED must be False on staging (testers get accounts from an admin)")
    if expect_staging and not s.RATE_LIMIT_ENABLED:
        report.fail("RATE_LIMIT_ENABLED must be True on staging")
    if expect_staging:
        if os.getenv("ACCESS_MODE", "") != "basic_auth":
            report.fail("ACCESS_MODE must be basic_auth on staging")
        if not os.getenv("STAGING_BASIC_AUTH_USER") or not os.getenv("STAGING_BASIC_AUTH_HASH"):
            report.fail("STAGING_BASIC_AUTH_USER and STAGING_BASIC_AUTH_HASH must be set on staging")
        if os.getenv("WEB_BIND", "127.0.0.1") != "127.0.0.1":
            report.fail("WEB_BIND must remain 127.0.0.1 during staging deployment")
    return report


def check_database(s: Settings, connect: Optional[Callable[[Settings], None]] = None) -> Report:
    report = Report()
    try:
        (connect or _connect)(s)
        report.ok("database reachable")
    except Exception as exc:  # the message may contain the host only; never the password
        report.fail(f"database not reachable ({type(exc).__name__})")
    return report


def _connect(s: Settings) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(s.DATABASE_URL, pool_pre_ping=True, hide_parameters=True,
                           connect_args={"connect_timeout": s.DATABASE_CONNECT_TIMEOUT})
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def main(argv: Optional[List[str]] = None, settings_factory: Callable[[], Settings] = Settings) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check-db", action="store_true", help="also connect to DATABASE_URL")
    parser.add_argument("--expect-staging", action="store_true",
                        help="require production mode, closed sign-up and rate limits")
    args = parser.parse_args(argv)
    s = settings_factory()
    report = check_settings(s, expect_staging=args.expect_staging)
    if args.check_db:
        db_report = check_database(s)
        report.lines.extend(db_report.lines)
        report.failed = report.failed or db_report.failed
    for line in report.lines:
        print(line)
    print("PREFLIGHT FAILED" if report.failed else "PREFLIGHT PASSED")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
