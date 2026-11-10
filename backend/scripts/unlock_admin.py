"""
Server-side recovery for a locked administrator account.

The dashboard unlock key (Admin > System) can only be issued by an administrator who is already signed in.
If the only administrator is locked out, that route is unavailable, so this command is the recovery path.
It needs shell access to the server (or `docker compose exec backend ...`), so it is not reachable from the
internet.

What it does
------------
* clears the lock, the failed-attempt counter and any pending unlock key;
* revokes every existing session of that account (token_version + 1), so a stolen token stops working;
* clears the shared rate-limit counter for the account when REDIS_URL is configured;
* writes an `admin_lock_recovery` audit event (operator action, no secret in it).

It never prints, stores or logs a key or password. To also change the password, run
`set_admin_password.py` afterwards. The operator must retype the email address to confirm.

Usage (from the repository root, or inside the backend container)
-----------------------------------------------------------------
    python backend/scripts/unlock_admin.py admin@example.com
"""
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def clear_lockout(user: Any) -> bool:
    """Reset the lock state on `user`. Returns True when the account was locked or had failures."""
    was_locked = bool(user.login_locked_at is not None or (user.failed_login_attempts or 0) > 0)
    user.login_locked_at = None
    user.failed_login_attempts = 0
    user.login_unlock_key_hash = None
    user.login_unlock_key_expires_at = None
    user.token_version = (user.token_version or 0) + 1
    return was_locked


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    email = argv[1].strip().lower()

    from backend.app.database import SessionLocal
    from backend.app.models.user import User
    from backend.app.security.ratelimit import rate_limiter
    from backend.app.services import audit_log

    confirm = input(f"Type the administrator email again to unlock it ({email}): ").strip().lower()
    if confirm != email:
        print("Email did not match. Nothing changed.")
        return 1

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).with_for_update().first()
        if user is None or not user.is_admin:
            # Same message for both cases; this tool does not confirm which emails exist.
            print("No administrator account matches that email. Nothing changed.")
            return 1
        was_locked = clear_lockout(user)
        db.commit()
        user_id = user.id

    rate_limiter.clear_login_failures(email)
    audit_log.record(
        "admin_lock_recovery",
        actor_type="operator",
        actor_id="server-shell",
        target=user_id,
        details={"was_locked": was_locked, "sessions_revoked": True},
    )
    print("Administrator unlocked and existing sessions revoked.")
    print("Without REDIS_URL the running backend keeps its in-memory counter: restart the backend container.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
