#!/usr/bin/env python3
"""Check a production .env before deploying. Prints setting NAMES only, never a value.

    python3 scripts/production/validate_env.py [.env]

Exit status 1 when anything blocks a safe deployment. Compose only rejects EMPTY values, and the backend
never sees POSTGRES_PASSWORD (it is blanked for it), so template placeholders in those would otherwise
go live silently.
"""
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

PLACEHOLDER = re.compile(r"change[-_]?me|replace[-_]?with|your[-_]|example\.com", re.IGNORECASE)
SECRETS = ("POSTGRES_PASSWORD", "SAP_BRIM_DB_PASSWORD", "REDIS_PASSWORD", "JWT_SECRET", "SUPER_ADMIN_PASSWORD")
REQUIRED = ("APP_DOMAIN", "ALLOWED_ORIGINS", "DATABASE_URL", "SUPER_ADMIN_EMAIL", *SECRETS)
PROVIDER_KEYS = [f"{p}_API_KEY{s}" for p in ("GROQ", "GEMINI", "OPENROUTER")
                 for s in ("", "_FALLBACK_1", "_FALLBACK_2", "_FALLBACK_3")]
REDIS_SAFE = re.compile(r"[A-Za-z0-9_-]+")


def parse(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def check(values: dict):
    errors, warnings = [], []

    for key in REQUIRED:
        if not values.get(key):
            errors.append(f"{key} is empty")
    for key in ("APP_DOMAIN", "ALLOWED_ORIGINS", *SECRETS):
        if values.get(key) and PLACEHOLDER.search(values[key]):
            errors.append(f"{key} still holds a template placeholder")

    if values.get("ENVIRONMENT", "").lower() != "production":
        errors.append("ENVIRONMENT must be set to production")
    domain = values.get("APP_DOMAIN", "")
    if domain and values.get("ALLOWED_ORIGINS") != f"https://{domain}":
        errors.append("ALLOWED_ORIGINS must be exactly https://<APP_DOMAIN>")
    if domain and (domain.startswith(("http://", "https://")) or "/" in domain):
        errors.append("APP_DOMAIN must be a bare host name (no scheme or path)")

    if len(values.get("JWT_SECRET", "")) < 32:
        errors.append("JWT_SECRET must be at least 32 characters")
    if len(values.get("SUPER_ADMIN_PASSWORD", "")) < 12:
        errors.append("SUPER_ADMIN_PASSWORD must be at least 12 characters")
    if values.get("ADMIN_API_KEY") and len(values["ADMIN_API_KEY"]) < 32:
        errors.append("ADMIN_API_KEY must be empty or at least 32 characters")

    present = [values[k] for k in SECRETS if values.get(k)]
    if len(present) != len(set(present)):
        errors.append("every secret and password must be different (a value is reused)")

    redis = values.get("REDIS_PASSWORD", "")
    if redis and not REDIS_SAFE.fullmatch(redis):
        errors.append("REDIS_PASSWORD may only contain letters, digits, - and _ (it is written into a URL)")

    url = urlsplit(values.get("DATABASE_URL", ""))
    if values.get("DATABASE_URL"):
        if url.hostname != "postgres":
            errors.append("DATABASE_URL host must be 'postgres' (the database container)")
        if unquote(url.password or "") != values.get("SAP_BRIM_DB_PASSWORD"):
            errors.append("DATABASE_URL password differs from SAP_BRIM_DB_PASSWORD")

    if not any(values.get(k) for k in PROVIDER_KEYS):
        errors.append("no LLM provider key is set (GROQ_API_KEY, GEMINI_API_KEY or OPENROUTER_API_KEY)")

    if values.get("ACCESS_MODE", "open") != "open":
        warnings.append("ACCESS_MODE is not 'open': visitors need the staging password")
    if values.get("WEB_BIND", "0.0.0.0") != "0.0.0.0":
        warnings.append("WEB_BIND is not 0.0.0.0: the site is not reachable from the internet")
    if values.get("REGISTRATION_ENABLED", "False").lower() in ("true", "1"):
        warnings.append("REGISTRATION_ENABLED is True: anyone can create an account (no e-mail verification)")
    if values.get("WEB_SEARCH_API_KEY", "") == "" and values.get("WEB_FALLBACK_ENABLED", "True").lower() in ("true", "1"):
        warnings.append("WEB_FALLBACK_ENABLED without WEB_SEARCH_API_KEY: web evidence is unavailable")
    return errors, warnings


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0] if argv else ".env")
    if not path.is_file():
        print(f"FAIL  {path} not found (start with: cp .env.example .env)")
        return 1
    errors, warnings = check(parse(path.read_text(encoding="utf-8")))
    for message in errors:
        print(f"FAIL  {message}")
    for message in warnings:
        print(f"WARN  {message}")
    print("ENV CHECK FAILED" if errors else "ENV CHECK PASSED")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
