"""
Set a new password for an existing admin account.

The password is typed at a hidden prompt, so it never appears in shell history or logs.

Usage (from the repository root)
--------------------------------
    python backend/scripts/set_admin_password.py admin@example.com
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.config import MIN_ADMIN_PASSWORD_LENGTH, is_published_secret  # noqa: E402
from backend.app.database import SessionLocal  # noqa: E402
from backend.app.models.user import User  # noqa: E402
from backend.app.security.auth import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    email = sys.argv[1].strip()

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).first()
        if not user or not user.is_admin:
            print(f"No admin account found for {email}.")
            return 1

        password = getpass.getpass("New password: ")
        if len(password) < MIN_ADMIN_PASSWORD_LENGTH or is_published_secret(password):
            print(f"Password must be private and at least {MIN_ADMIN_PASSWORD_LENGTH} characters. Nothing changed.")
            return 1
        if getpass.getpass("Repeat new password: ") != password:
            print("Passwords do not match. Nothing changed.")
            return 1

        user.password_hash = hash_password(password)
        db.commit()
    print(f"Password updated for {email}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
