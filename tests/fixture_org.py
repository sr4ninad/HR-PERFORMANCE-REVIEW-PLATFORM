"""The small, fixed org every test runs against.

Kept separate from the app's demo data (app/seed.py) on purpose, so tests
don't break whenever the demo accounts change:

    hr_admin   (hr_admin)
    director1  (manager, no manager of their own)
      └─ manager1  (manager)
           └─ user1 .. user4  (employees)

plus one active cycle, "Q1 2026". Passwords are deliberately simple and
are written straight into the database, bypassing the password rules: tests
that need a password *set* through the app use rule-compliant ones.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import CycleStatus, ReviewCycle, Role, User
from app.security import hash_password_argon2

TEST_PASSWORD = "password123"
TEST_ADMIN_PASSWORD = "admin123"


def seed_test_org(db: Session) -> None:
    hr_admin = User(username="hr_admin", password_hash=hash_password_argon2(TEST_ADMIN_PASSWORD), role=Role.HR_ADMIN.value)
    director1 = User(username="director1", password_hash=hash_password_argon2(TEST_PASSWORD), role=Role.MANAGER.value)
    db.add_all([hr_admin, director1])
    db.flush()

    manager1 = User(
        username="manager1",
        password_hash=hash_password_argon2(TEST_PASSWORD),
        role=Role.MANAGER.value,
        manager_id=director1.id,
    )
    db.add(manager1)
    db.flush()

    db.add_all(
        User(
            username=f"user{i}",
            password_hash=hash_password_argon2(TEST_PASSWORD),
            role=Role.EMPLOYEE.value,
            manager_id=manager1.id,
        )
        for i in range(1, 5)
    )

    now = datetime.now(timezone.utc)
    db.add(ReviewCycle(name="Q1 2026", start_date=now, end_date=now + timedelta(days=30), status=CycleStatus.ACTIVE.value))
    db.commit()
