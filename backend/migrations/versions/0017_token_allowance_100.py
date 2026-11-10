"""Free token allowance is now 100 (was 1,000): new default and a one-time cap on existing balances.

The refill already tops wallets up to settings.TOKEN_ALLOWANCE, so this only fixes the column default for new
rows and lowers balances that are still above 100 tokens so they do not read "950 of 100". Balances at or below
100 are left alone (the cap never raises a balance). Idempotent.

Revision ID: 0017_token_allowance_100
Revises: 0016_auth_security_controls
Create Date: 2026-11-10
"""
from alembic import op
from sqlalchemy import inspect, text

revision = "0017_token_allowance_100"
down_revision = "0016_auth_security_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("users"):
        return
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "token_balance_milli" not in columns:
        return
    bind.execute(text("ALTER TABLE users ALTER COLUMN token_balance_milli SET DEFAULT 100000"))
    bind.execute(text("UPDATE users SET token_balance_milli = 100000 WHERE token_balance_milli > 100000"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if inspector.has_table("users") and "token_balance_milli" in {c["name"] for c in inspector.get_columns("users")}:
        bind.execute(text("ALTER TABLE users ALTER COLUMN token_balance_milli SET DEFAULT 1000000"))
