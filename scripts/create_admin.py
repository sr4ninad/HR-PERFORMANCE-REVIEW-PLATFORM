"""Create the first HR admin account.

Demo accounts are seeded automatically only outside production
(HR_REVIEW_SEED_DEMO), so a real deployment starts with no users at all.
Run this once against that database to create the first hr_admin; they can
provision everyone else from the Users page.

Usage (from the project root, with the same env vars as the app):
    python scripts/create_admin.py <username>
The password is prompted for, never passed on the command line.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.audit import log_action  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.db_migrations import upgrade_database  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security import hash_password_argon2  # noqa: E402
from app.services import password_problem, username_problem  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    username = sys.argv[1].strip()
    if problem := username_problem(username):
        print(problem)
        return 1

    password = getpass.getpass("Password: ")
    if problem := password_problem(password, username):
        print(problem)
        return 1
    if getpass.getpass("Confirm password: ") != password:
        print("Passwords don't match.")
        return 1

    upgrade_database(engine)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == username).first() is not None:
            print(f"User '{username}' already exists.")
            return 1
        admin = User(username=username, password_hash=hash_password_argon2(password), role=Role.HR_ADMIN.value)
        db.add(admin)
        db.flush()
        log_action(db, None, "create_user", f"user:{username}", {"role": Role.HR_ADMIN.value, "via": "create_admin.py"})
        db.commit()
    finally:
        db.close()
    print(f"Created hr_admin '{username}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
