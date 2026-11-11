"""scripts/production: the .env validator and the structure of the deploy / backup scripts (no Docker needed)."""

import importlib.util
import re
import shutil
import subprocess

import pytest

from backend.app.config import ROOT_DIR

SCRIPTS = ROOT_DIR / "scripts" / "production"
_spec = importlib.util.spec_from_file_location("validate_env", SCRIPTS / "validate_env.py")
validate_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_env)


def _good():
    return {
        "ENVIRONMENT": "production",
        "APP_DOMAIN": "assistant.acme.org",
        "ALLOWED_ORIGINS": "https://assistant.acme.org",
        "POSTGRES_PASSWORD": "p" * 24,
        "SAP_BRIM_DB_PASSWORD": "q" * 24,
        "DATABASE_URL": "postgresql://sap_brim_ai:" + "q" * 24 + "@postgres:5432/sap_brim_ai",
        "REDIS_PASSWORD": "r" * 32,
        "JWT_SECRET": "j" * 48,
        "SUPER_ADMIN_EMAIL": "admin@acme.org",
        "SUPER_ADMIN_PASSWORD": "a-private-admin-password",
        "GROQ_API_KEY": "key",
        "WEB_FALLBACK_ENABLED": "False",
    }


def test_a_complete_production_env_passes():
    errors, _ = validate_env.check(_good())
    assert errors == []


@pytest.mark.parametrize("change, expected", [
    ({"POSTGRES_PASSWORD": "change-me-superuser-password"}, "POSTGRES_PASSWORD still holds a template placeholder"),
    ({"SAP_BRIM_DB_PASSWORD": "change-me-app-password"}, "SAP_BRIM_DB_PASSWORD still holds a template placeholder"),
    ({"ENVIRONMENT": "development"}, "ENVIRONMENT must be set to production"),
    ({"ENVIRONMENT": ""}, "ENVIRONMENT must be set to production"),
    ({"APP_DOMAIN": "assistant.example.com", "ALLOWED_ORIGINS": "https://assistant.example.com"}, "APP_DOMAIN still holds"),
    ({"ALLOWED_ORIGINS": "https://other.acme.org"}, "ALLOWED_ORIGINS must be exactly"),
    ({"JWT_SECRET": "short"}, "JWT_SECRET must be at least 32"),
    ({"REDIS_PASSWORD": ""}, "REDIS_PASSWORD is empty"),
    ({"REDIS_PASSWORD": "has/slash:colon"}, "REDIS_PASSWORD may only contain"),
    ({"POSTGRES_PASSWORD": "q" * 24}, "a value is reused"),
    ({"DATABASE_URL": "postgresql://sap_brim_ai:other@postgres:5432/sap_brim_ai"}, "differs from SAP_BRIM_DB_PASSWORD"),
    ({"DATABASE_URL": "postgresql://sap_brim_ai:" + "q" * 24 + "@localhost:5432/x"}, "host must be 'postgres'"),
    ({"GROQ_API_KEY": ""}, "no LLM provider key"),
])
def test_unsafe_env_is_rejected(change, expected):
    errors, _ = validate_env.check({**_good(), **change})
    assert any(expected in error for error in errors), errors


def test_url_encoded_database_password_is_accepted():
    password = "pa:ss/w@rd"
    values = {**_good(), "SAP_BRIM_DB_PASSWORD": password,
              "DATABASE_URL": "postgresql://sap_brim_ai:pa%3Ass%2Fw%40rd@postgres:5432/sap_brim_ai"}
    assert validate_env.check(values)[0] == []


def test_unfilled_template_is_rejected_and_values_are_never_printed(tmp_path, capsys):
    template = ROOT_DIR / ".env.example"
    assert validate_env.main([str(template)]) == 1
    out = capsys.readouterr().out
    assert "ENV CHECK FAILED" in out and "change-me" not in out


def test_open_sign_up_and_closed_bind_only_warn():
    errors, warnings = validate_env.check({**_good(), "REGISTRATION_ENABLED": "True", "WEB_BIND": "127.0.0.1"})
    assert errors == []
    assert any("REGISTRATION_ENABLED" in w for w in warnings) and any("WEB_BIND" in w for w in warnings)


def _text(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("name", ["deploy.sh", "backup.sh", "restore-check.sh", "install-backup-cron.sh"])
def test_shell_scripts_parse(name):
    # Script text goes in on stdin, so the check does not depend on how this bash maps Windows paths.
    result = subprocess.run(["bash", "-n"], input=_text(name).encode("utf-8"), capture_output=True)  # bytes: keep LF
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_deploy_validates_preflights_and_backs_up_before_replacing_anything():
    script = _text("deploy.sh")
    order = [script.index(marker) for marker in (
        "validate_env.py", '"${COMPOSE[@]}" build', "--expect-production --check-db",
        "scripts/production/backup.sh", "up -d --remove-orphans")]
    assert order == sorted(order)


@pytest.mark.parametrize("name", ["deploy.sh", "backup.sh", "restore-check.sh", "install-backup-cron.sh"])
def test_scripts_never_delete_volumes(name):
    script = "\n".join(line for line in _text(name).splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"\bdown\b[^\n]*(\s-v\b|--volumes)", script)
    assert "volume rm" not in script and "docker volume" not in script


def test_backup_verifies_the_dump_and_restore_check_only_drops_its_scratch_database():
    backup = _text("backup.sh")
    assert "pg_restore --list" in backup and ".partial" in backup
    restore = _text("restore-check.sh")
    assert 'scratch="restore_check_' in restore
    assert [m for m in re.findall(r"dropdb[^\n]*", restore) if "$scratch" not in m] == []


def test_staging_deploy_still_demands_its_locked_down_settings():
    script = (ROOT_DIR / "scripts" / "staging" / "deploy.sh").read_text(encoding="utf-8")
    for expected in ("'COMPOSE_PROJECT_NAME': 'sap_brim_staging'", "'REGISTRATION_ENABLED': 'False'",
                     "'ACCESS_MODE': 'basic_auth'", "'WEB_BIND': '127.0.0.1'"):
        assert expected in script
