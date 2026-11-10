"""Persistent registration switch and account unlock state.

Revision ID: 0016_auth_security_controls
Revises: 0015_question_language
Create Date: 2026-11-09
"""
from alembic import op
from sqlalchemy import inspect, text

revision = "0016_auth_security_controls"
down_revision = "0015_question_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        additions = (
            ("failed_login_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("login_locked_at", "TIMESTAMP NULL"),
            ("login_unlock_key_hash", "VARCHAR(64) NULL"),
            ("login_unlock_key_expires_at", "TIMESTAMP NULL"),
        )
        for name, definition in additions:
            if name not in columns:
                bind.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))

    if not inspect(bind).has_table("security_settings"):
        bind.execute(text(
            "CREATE TABLE security_settings ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "registration_enabled BOOLEAN NOT NULL DEFAULT FALSE)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("security_settings"):
        bind.execute(text("DROP TABLE security_settings"))
    if inspector.has_table("users"):
        columns = {column["name"] for column in inspector.get_columns("users")}
        for name in ("login_unlock_key_expires_at", "login_unlock_key_hash", "login_locked_at", "failed_login_attempts"):
            if name in columns:
                bind.execute(text(f"ALTER TABLE users DROP COLUMN {name}"))
