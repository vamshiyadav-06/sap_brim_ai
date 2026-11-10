"""Deployment configuration: the two .env templates, the production Docker stack and the startup guard
against template passwords. Pure file/settings checks: no database, network or Docker needed."""

import ipaddress
import re
from urllib.parse import urlparse

import pytest
import yaml

from backend.app.config import ROOT_DIR, Settings, security_settings_errors

DEV_TEMPLATE = ROOT_DIR / ".env.example"
PROD_TEMPLATE = ROOT_DIR / ".env.production.example"
PROD_COMPOSE = ROOT_DIR / "docker-compose.prod.yml"
BACKEND_DOCKERFILE = ROOT_DIR / "backend" / "Dockerfile"
FRONTEND_DOCKERFILE = ROOT_DIR / "frontend" / "Dockerfile"
CADDYFILE = ROOT_DIR / "frontend" / "Caddyfile"
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.MULTILINE)
SECRET_KEYS = ("JWT_SECRET", "ADMIN_API_KEY", "SUPER_ADMIN_EMAIL", "SUPER_ADMIN_PASSWORD", "GROQ_API_KEY",
               "GEMINI_API_KEY", "OPENROUTER_API_KEY", "WEB_SEARCH_API_KEY", "REDIS_PASSWORD")
PRIVATE_JWT = "x" * 48


def _pairs(path):
    return ASSIGNMENT.findall(path.read_text(encoding="utf-8"))


def _values(path):
    return {key: value.strip() for key, value in _pairs(path)}


def _compose_variables(path):
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", path.read_text(encoding="utf-8")))


def _settings_from(values, **overrides):
    """Settings built only from the template values (init arguments beat the process environment)."""
    return Settings(_env_file=None, **{**values, **overrides})


# --- production template -------------------------------------------------------------------------------

def test_production_template_has_no_duplicate_keys():
    keys = [key for key, _ in _pairs(PROD_TEMPLATE)]
    assert len(keys) == len(set(keys))


def test_production_template_covers_every_production_compose_variable():
    assert _compose_variables(PROD_COMPOSE) <= set(_values(PROD_TEMPLATE))


def test_every_production_template_key_is_used():
    # COMPOSE_FILE is read by the docker compose CLI itself.
    used = _compose_variables(PROD_COMPOSE) | set(Settings.model_fields) | {"COMPOSE_FILE"}
    assert [key for key in _values(PROD_TEMPLATE) if key not in used] == []


def test_production_env_selects_the_production_compose_file():
    assert _values(PROD_TEMPLATE)["COMPOSE_FILE"] == PROD_COMPOSE.name


def test_production_template_ships_no_secrets():
    values = _values(PROD_TEMPLATE)
    assert {key: values[key] for key in SECRET_KEYS if values.get(key)} == {}


def test_production_template_targets_the_compose_database_and_https_origin():
    values = _values(PROD_TEMPLATE)
    url = urlparse(values["DATABASE_URL"])
    assert (url.scheme, url.hostname, url.port, url.path) == ("postgresql", "postgres", 5432, "/sap_brim_ai")
    assert url.username == "sap_brim_ai"
    assert url.password == values["SAP_BRIM_DB_PASSWORD"]
    assert values["ENVIRONMENT"] == "production"
    assert values["ALLOWED_ORIGINS"] == f"https://{values['APP_DOMAIN']}"
    assert values["INCLUDE_SELF_AUTHORED_DOCS"] == "False"
    assert values["REGISTRATION_ENABLED"] == "False"
    assert values["LOGIN_MAX_FAILURES"] == "5"
    assert values["LOGIN_UNLOCK_KEY_TTL_MINUTES"] == "15"


def test_untouched_production_template_is_refused_at_startup():
    values = _values(PROD_TEMPLATE)
    errors = " ".join(security_settings_errors(_settings_from(values)))
    assert "JWT_SECRET" in errors
    assert "DATABASE_URL still uses the template database password" in errors


def test_completed_production_template_passes_the_startup_checks():
    values = _values(PROD_TEMPLATE)
    settings = _settings_from(
        values,
        JWT_SECRET=PRIVATE_JWT,
        DATABASE_URL=values["DATABASE_URL"].replace("change-me-app-password", "Q2v8_private-db-pass"),
        SUPER_ADMIN_EMAIL="admin@assistant.example.com",
        SUPER_ADMIN_PASSWORD="a-private-admin-password",
    )
    assert security_settings_errors(settings) == []
    origins = [origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",")]
    assert all(origin.startswith("https://") and "localhost" not in origin for origin in origins)


# --- development template ------------------------------------------------------------------------------

def test_development_template_database_url_matches_the_compose_values():
    values = _values(DEV_TEMPLATE)
    url = urlparse(values["DATABASE_URL"])
    assert url.hostname == "127.0.0.1"
    assert url.port == int(values["POSTGRES_HOST_PORT"])
    assert url.password == values["SAP_BRIM_DB_PASSWORD"]
    assert values["ENVIRONMENT"] == "development"


def test_development_template_ships_no_secrets():
    values = _values(DEV_TEMPLATE)
    assert {key: values[key] for key in SECRET_KEYS if values.get(key)} == {}


def test_both_templates_use_the_same_embedding_model():
    # Vectors record their model name: a mismatch makes a restored development database unusable.
    assert _values(DEV_TEMPLATE)["EMBEDDING_MODEL"] == _values(PROD_TEMPLATE)["EMBEDDING_MODEL"]


# --- startup guard against template passwords ----------------------------------------------------------

@pytest.mark.parametrize("password", ["change-me-app-password", "CHANGE_ME", "changeme123"])
def test_production_refuses_a_template_database_password(password):
    settings = _settings_from({}, ENVIRONMENT="production", JWT_SECRET=PRIVATE_JWT,
                              DATABASE_URL=f"postgresql://sap_brim_ai:{password}@postgres:5432/sap_brim_ai")
    assert any("template database password" in error for error in security_settings_errors(settings))


def test_production_refuses_a_template_admin_password():
    settings = _settings_from({}, ENVIRONMENT="production", JWT_SECRET=PRIVATE_JWT,
                              DATABASE_URL="postgresql://sap_brim_ai:Q2v8_private@postgres:5432/sap_brim_ai",
                              SUPER_ADMIN_EMAIL="admin@example.com", SUPER_ADMIN_PASSWORD="change-me-admin-pass")
    assert any("SUPER_ADMIN_PASSWORD still uses a template value" in e for e in security_settings_errors(settings))


def test_development_keeps_accepting_the_template_database_password():
    settings = _settings_from({}, ENVIRONMENT="development", JWT_SECRET=PRIVATE_JWT, ADMIN_API_KEY="",
                              SUPER_ADMIN_EMAIL="", SUPER_ADMIN_PASSWORD="",
                              DATABASE_URL="postgresql://sap_brim_ai:change-me-app-password@127.0.0.1:5434/sap_brim_ai")
    assert security_settings_errors(settings) == []


# --- Docker stack ------------------------------------------------------------------------------------

def _compose():
    return yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))


def test_only_the_web_container_is_published():
    services = _compose()["services"]
    # redis holds the shared rate-limit/lockout counters; like postgres and the backend it is internal only.
    assert set(services) == {"postgres", "redis", "backend", "web"}
    assert "ports" not in services["postgres"]
    assert "ports" not in services["redis"]
    assert "ports" not in services["backend"]
    assert services["web"]["ports"]


def test_backend_trusts_only_the_single_caddy_address_and_does_not_auto_migrate_in_process():
    compose = _compose()
    backend = compose["services"]["backend"]
    assert backend["environment"]["AUTO_MIGRATE"] == "False"
    # Whole private ranges (10/8, 172.16/12, 192.168/16) would let any container on the network forge client IPs.
    trusted = backend["environment"]["TRUSTED_PROXIES"]
    assert "," not in trusted and "/" not in trusted
    default_ip = ipaddress.ip_address(re.search(r":-([0-9.]+)\}", trusted).group(1))
    subnet = ipaddress.ip_network(re.search(r":-([0-9./]+)\}", compose["networks"]["internal"]["ipam"]["config"][0]["subnet"]).group(1))
    assert default_ip.is_private and default_ip in subnet and subnet.prefixlen >= 24
    web_ip = compose["services"]["web"]["networks"]["internal"]["ipv4_address"]
    assert re.search(r":-([0-9.]+)\}", web_ip).group(1) == str(default_ip)
    assert backend["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert compose["services"]["web"]["depends_on"]["backend"]["condition"] == "service_healthy"
    assert "./knowledge_base:/app/knowledge_base" in backend["volumes"]


def test_database_volume_is_named_and_kept():
    compose = _compose()
    assert "pgdata:/var/lib/postgresql/data" in compose["services"]["postgres"]["volumes"]
    assert "pgdata" in compose["volumes"]


def test_backend_image_migrates_then_serves_one_worker_as_an_unprivileged_user():
    dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER app$", dockerfile, re.MULTILINE)
    command = dockerfile[dockerfile.index("CMD"):]
    assert command.index("alembic -c backend/alembic.ini upgrade head") < command.index("uvicorn backend.app.main:app")
    assert "--workers 1" in command
    assert "download.pytorch.org/whl/cpu" in dockerfile


def test_backend_build_context_excludes_secrets_and_local_data():
    lines = [line.strip() for line in (ROOT_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    rules = [line for line in lines if line and not line.startswith("#")]
    assert rules[0] == "*" and "!backend/" in rules
    assert "backend/**/.env" in rules and "backend/**/.env.*" in rules
    assert not any(rule.startswith("!") and ".env" in rule for rule in rules)


def test_web_image_builds_the_app_and_proxies_the_api_with_streaming():
    dockerfile = FRONTEND_DOCKERFILE.read_text(encoding="utf-8")
    assert "npm ci" in dockerfile and "npm run build" in dockerfile
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    api_block = caddyfile[caddyfile.index("handle /api/*"):caddyfile.index("handle /health")]
    assert "reverse_proxy backend:8000" in api_block and "flush_interval -1" in api_block
    assert "try_files {path} /index.html" in caddyfile
    assert "frame-ancestors 'none'" in caddyfile
    assert "VITE_REGISTRATION_ENABLED" in dockerfile
    assert _compose()["services"]["web"]["build"]["args"]["VITE_REGISTRATION_ENABLED"] == "${REGISTRATION_ENABLED:-False}"


# --- staging template, gate and image pins ---------------------------------------------------------------

STAGING_TEMPLATE = ROOT_DIR / ".env.staging.example"
GATE = ROOT_DIR / "frontend" / "caddy" / "access" / "basic_auth.caddy"
LOCK = ROOT_DIR / "backend" / "requirements.lock"


def test_staging_template_covers_compose_and_uses_only_known_keys():
    values = _values(STAGING_TEMPLATE)
    keys = [key for key, _ in _pairs(STAGING_TEMPLATE)]
    assert len(keys) == len(set(keys))
    assert _compose_variables(PROD_COMPOSE) <= set(values)
    used = _compose_variables(PROD_COMPOSE) | set(Settings.model_fields) | {"COMPOSE_FILE", "COMPOSE_PROJECT_NAME"}
    assert [key for key in values if key not in used] == []
    assert {key: values[key] for key in SECRET_KEYS if values.get(key)} == {}
    assert values["STAGING_BASIC_AUTH_HASH"] in ("", "''")


def test_staging_template_is_locked_down():
    values = _values(STAGING_TEMPLATE)
    assert values["ENVIRONMENT"] == "production"
    assert values["REGISTRATION_ENABLED"] == "False"
    assert values["ACCESS_MODE"] == "basic_auth"
    assert values["WEB_BIND"] == "127.0.0.1"
    assert values["COMPOSE_PROJECT_NAME"] not in ("", "sap_brim", "sap_brim_prod")
    assert values["ALLOWED_ORIGINS"] == f"https://{values['APP_DOMAIN']}"
    url = urlparse(values["DATABASE_URL"])
    assert (url.hostname, url.port, url.password) == ("postgres", 5432, values["SAP_BRIM_DB_PASSWORD"])
    assert values["EMBEDDING_MODEL"] == _values(DEV_TEMPLATE)["EMBEDDING_MODEL"]


def test_completed_staging_template_passes_the_staging_preflight(monkeypatch):
    from backend.app.preflight import check_settings
    values = _values(STAGING_TEMPLATE)
    values.pop("STAGING_BASIC_AUTH_HASH")
    monkeypatch.setenv("ACCESS_MODE", "basic_auth")
    monkeypatch.setenv("STAGING_BASIC_AUTH_USER", "staging")
    monkeypatch.setenv("STAGING_BASIC_AUTH_HASH", "bcrypt-hash")
    monkeypatch.setenv("WEB_BIND", "127.0.0.1")
    settings = _settings_from(
        values, JWT_SECRET=PRIVATE_JWT, OPENROUTER_API_KEY="staging-key",
        DATABASE_URL=values["DATABASE_URL"].replace("change-me-app-password", "Q2v8_private-db-pass"),
        SUPER_ADMIN_EMAIL="admin@staging.example.com", SUPER_ADMIN_PASSWORD="a-private-admin-password",
    )
    report = check_settings(settings, expect_staging=True)
    assert not report.failed, report.lines


def test_staging_preflight_rejects_missing_gate_or_public_bind(monkeypatch):
    from backend.app.preflight import check_settings
    values = _values(STAGING_TEMPLATE)
    settings = _settings_from(
        values,
        JWT_SECRET=PRIVATE_JWT,
        OPENROUTER_API_KEY="staging-key",
        DATABASE_URL=values["DATABASE_URL"].replace("change-me-app-password", "Q2v8_private-db-pass"),
        SUPER_ADMIN_EMAIL="admin@staging.example.com",
        SUPER_ADMIN_PASSWORD="a-private-admin-password",
    )
    monkeypatch.setenv("ACCESS_MODE", "open")
    monkeypatch.delenv("STAGING_BASIC_AUTH_USER", raising=False)
    monkeypatch.delenv("STAGING_BASIC_AUTH_HASH", raising=False)
    monkeypatch.setenv("WEB_BIND", "0.0.0.0")
    report = check_settings(settings, expect_staging=True)
    assert report.failed
    assert any("ACCESS_MODE" in line for line in report.lines)
    assert any("STAGING_BASIC_AUTH" in line for line in report.lines)
    assert any("WEB_BIND" in line for line in report.lines)


def test_staging_gate_covers_the_app_and_every_api_request():
    gate = GATE.read_text(encoding="utf-8")
    assert "basic_auth @staging_gate" in gate and "not path /health" in gate
    assert 'header Authorization "Bearer *"' not in gate
    assert not any(endpoint in gate for endpoint in (
        "/api/auth/register", "/api/auth/login", "/api/auth/admin-login", "/api/auth/refresh",
        "/api/org/auth/login", "/api/org/requests", "/api/org/activation/", "/api/shared/",
    ))
    caddyfile = CADDYFILE.read_text(encoding="utf-8")
    assert "import /etc/caddy/access/{$ACCESS_MODE:open}.caddy" in caddyfile
    web_env = _compose()["services"]["web"]["environment"]
    assert {"ACCESS_MODE", "STAGING_BASIC_AUTH_USER", "STAGING_BASIC_AUTH_HASH"} <= set(web_env)


def test_backend_container_gets_no_unneeded_secrets_and_drops_privileges():
    backend = _compose()["services"]["backend"]
    for key in ("POSTGRES_PASSWORD", "SAP_BRIM_DB_PASSWORD", "STAGING_BASIC_AUTH_HASH"):
        assert backend["environment"][key] == ""
    assert "no-new-privileges:true" in backend["security_opt"] and backend["cap_drop"] == ["ALL"]


def test_redis_is_private_password_protected_and_required_healthy_for_backend():
    compose = _compose()
    services = compose["services"]
    redis = services["redis"]
    backend = services["backend"]

    assert "ports" not in redis
    assert redis["networks"] == ["cache"]
    assert backend["networks"] == ["internal", "cache"]
    assert "--requirepass" in redis["command"]
    assert backend["environment"]["REDIS_URL"].startswith("redis://:")
    assert backend["depends_on"]["redis"]["condition"] == "service_healthy"
    assert compose["networks"]["cache"]["internal"] is True


def test_every_service_has_limits_healthchecks_and_pinned_images():
    services = _compose()["services"]
    for name in ("postgres", "redis", "backend", "web"):
        limits = services[name]["deploy"]["resources"]["limits"]
        assert limits["cpus"] and limits["memory"]
    assert "healthcheck" in services["postgres"] and "healthcheck" in services["redis"] and "healthcheck" in services["web"]
    assert "HEALTHCHECK" in BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    assert re.fullmatch(r"pgvector/pgvector:\d+\.\d+\.\d+-pg16", services["postgres"]["image"])
    assert re.fullmatch(r"redis:\d+\.\d+\.\d+-alpine", services["redis"]["image"])
    for dockerfile in (BACKEND_DOCKERFILE, FRONTEND_DOCKERFILE):
        for image in re.findall(r"^FROM (\S+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE):
            assert re.search(r":\d+\.\d+\.\d+", image), f"{dockerfile.name}: {image} is not pinned"


def test_backend_image_installs_the_hashed_lock_without_unused_vulnerable_packages():
    dockerfile = BACKEND_DOCKERFILE.read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile and "requirements.lock" in dockerfile
    lock = LOCK.read_text(encoding="utf-8").lower()
    pinned = set(re.findall(r"^([a-z0-9_.\-]+)==", lock, re.MULTILINE))
    for banned in ("python-jose", "ecdsa", "passlib"):
        assert banned not in pinned
    assert not any(name.startswith("nvidia-") for name in pinned)
    wanted = re.findall(r"^([A-Za-z0-9_.\-]+)", (ROOT_DIR / "backend" / "requirements.txt").read_text(encoding="utf-8"),
                        re.MULTILINE)
    normalise = lambda n: re.sub(r"[-_.]+", "-", n.lower())
    assert {normalise(n) for n in wanted} <= {normalise(n) for n in pinned}


def test_public_root_route_hides_service_name_version_and_docs_path_in_production(monkeypatch):
    from backend.app import main

    monkeypatch.setattr(main, "_production", True)
    assert main.root() == {"status": "running"}
    monkeypatch.setattr(main, "_production", False)
    assert "version" in main.root()


def _env_keys(path):
    return {line.split("=", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.lstrip().startswith("#")}


def test_every_required_compose_variable_is_in_the_production_and_staging_templates():
    """docker-compose.prod.yml fails fast on `${VAR:?...}`; the templates must list each such variable so an
    operator is told to fill it in (a missing key only surfaces at `docker compose up`)."""
    required = set(re.findall(r"\$\{(\w+):\?", PROD_COMPOSE.read_text(encoding="utf-8")))
    assert "REDIS_PASSWORD" in required
    for template in (".env.production.example", ".env.staging.example"):
        missing = required - _env_keys(ROOT_DIR / template)
        assert not missing, f"{template} lacks {sorted(missing)}"


def test_staging_env_initialiser_generates_the_redis_password():
    script = (ROOT_DIR / "scripts" / "staging" / "init-env.sh").read_text(encoding="utf-8")
    assert re.search(r"'REDIS_PASSWORD':\s*secrets\.token_hex\(", script)


def _load_yaml_rejecting_duplicate_keys(path):
    """PyYAML keeps the last of two equal keys without a word, but Docker Compose refuses the file."""
    class StrictLoader(yaml.SafeLoader):
        pass

    def construct(loader, node, deep=False):
        seen = set()
        for key_node, _value in node.value:
            key = loader.construct_object(key_node, deep=deep)
            assert key not in seen, f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)


def test_compose_files_have_no_duplicate_keys():
    for name in ("docker-compose.prod.yml", "docker-compose.yml", "docker-compose.e2e.yml"):
        _load_yaml_rejecting_duplicate_keys(ROOT_DIR / name)


def test_backend_grace_period_outlasts_the_shutdown_drain():
    grace = _load_yaml_rejecting_duplicate_keys(PROD_COMPOSE)["services"]["backend"]["stop_grace_period"]
    seconds = int(re.fullmatch(r"(\d+)s", str(grace)).group(1))
    assert seconds > Settings().SHUTDOWN_DRAIN_SECONDS, "running answers would be killed before they are saved"
