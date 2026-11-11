"""The project has one settings file: the repository-root .env, read by Docker Compose
(docker-compose.prod.yml) and by the backend (backend/app/config.py). Its template is .env.example."""

import re

from backend.app.config import ROOT_DIR, Settings

TEMPLATE = ROOT_DIR / ".env.example"
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)


def _template_keys():
    return ASSIGNMENT.findall(TEMPLATE.read_text(encoding="utf-8"))


def _compose_variables():
    compose = (ROOT_DIR / "docker-compose.prod.yml").read_text(encoding="utf-8")
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", compose))


def test_backend_reads_only_the_repository_root_env_file():
    assert Settings.model_config["env_file"] == str(ROOT_DIR / ".env")


def test_there_is_one_template_and_no_backend_copy():
    assert TEMPLATE.is_file()
    assert not (ROOT_DIR / "backend" / ".env.example").exists()


def test_template_has_no_duplicate_keys():
    keys = _template_keys()
    assert len(keys) == len(set(keys))


def test_template_covers_every_compose_variable():
    variables = _compose_variables()
    assert variables, "docker-compose.prod.yml no longer interpolates any variable"
    assert variables <= set(_template_keys())


def test_every_template_key_is_read_by_compose_or_the_backend():
    # Catches keys left behind by removed services (e.g. the old SearXNG/Siftdog settings).
    used = _compose_variables() | set(Settings.model_fields) | {"COMPOSE_FILE"}
    assert [k for k in _template_keys() if k not in used] == []


def test_compose_only_keys_in_the_shared_file_do_not_break_backend_settings(tmp_path, monkeypatch):
    for key in ("TOP_K", "POSTGRES_PASSWORD", "SAP_BRIM_DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_HOST_PORT=5434\nPOSTGRES_PASSWORD=compose-only\nSAP_BRIM_DB_PASSWORD=compose-only\nTOP_K=7\n",
        encoding="utf-8",
    )
    assert Settings(_env_file=env_file).TOP_K == 7
