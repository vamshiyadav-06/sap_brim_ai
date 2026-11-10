"""Free token wallet for individual accounts.

Every individual account has an allowance (settings.TOKEN_ALLOWANCE, 100) that is topped back up to the full
allowance every settings.TOKEN_REFILL_DAYS days (3): unused tokens do not pile up, and the refill happens the
first time the account is touched after the date (sign-in, profile, a question), so no scheduler is needed.
Each answer costs
between 0 and 1 token, by how hard the question was: the more LLM work an answer needed (measured with the
same estimate as the savings counter), the closer to 1; answers that needed no model call (conversation
memory, refusals) cost 0. Balances are stored in thousandths of a token so no floating-point drift builds up.
Administrators and organisation members are not metered.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models.user import User

MILLI = 1000
# An answer that used at least this many estimated LLM tokens (prompts plus replies) costs a full token.
FULL_COST_LLM_TOKENS = 6000
MIN_COST_MILLI = 10  # any answer that called a model costs at least 0.01 token


def is_metered(user: Any) -> bool:
    return not getattr(user, "is_admin", False) and getattr(user, "account_type", "individual") == "individual"


def cost_milli(llm_tokens_used: int) -> int:
    """0 when no model was called, otherwise 0.01 to 1.00 token, rising with the work the answer needed."""
    if llm_tokens_used <= 0:
        return 0
    return max(MIN_COST_MILLI, min(MILLI, round(llm_tokens_used * MILLI / FULL_COST_LLM_TOKENS)))


def cost_tokens(llm_tokens_used: int) -> float:
    return cost_milli(llm_tokens_used) / MILLI  # exactly what is subtracted, to three decimals


def balance_tokens(user: Any) -> float:
    return (getattr(user, "token_balance_milli", None) or 0) / MILLI


def has_balance(user: Any) -> bool:
    return not is_metered(user) or (user.token_balance_milli or 0) > 0


def charge(db: Session, user_id: str, amount_milli: int) -> int:
    """Subtract atomically (never below 0) and return the new balance in thousandths."""
    if amount_milli > 0:
        db.execute(update(User).where(User.id == user_id)
                   .values(token_balance_milli=func.greatest(User.token_balance_milli - amount_milli, 0)))
        db.commit()
    return db.query(User.token_balance_milli).filter(User.id == user_id).scalar() or 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def refill_if_due(db: Session, user: Any) -> None:
    """Top the wallet back up to the allowance when its refill date has passed (atomic, safe to call often)."""
    if not is_metered(user):
        return
    now = _utcnow()
    next_refill = now + timedelta(days=settings.TOKEN_REFILL_DAYS)
    first_touch = db.execute(update(User).where(User.id == user.id, User.token_refill_at.is_(None))
                             .values(token_refill_at=next_refill)).rowcount
    due = db.execute(update(User).where(User.id == user.id, User.token_refill_at <= now)
                     .values(token_balance_milli=settings.TOKEN_ALLOWANCE * MILLI, token_refill_at=next_refill)).rowcount
    if first_touch or due:
        db.commit()
        db.refresh(user)
