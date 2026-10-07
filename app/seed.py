"""Demo data for a fresh database (development only by default; see
HR_REVIEW_SEED_DEMO in config.py).

The org:

    Kavya Iyer      HR Admin
    Rajesh Menon    Director
      └─ Priya Sharma   Manager
           ├─ Arjun Nair
           ├─ Sneha Kulkarni
           ├─ Rohan Desai
           ├─ Meera Pillai
           └─ Vikram Reddy   (employees)

Every password meets the app's own password rules (services.password_problem).
These are demo credentials, published in the README -- never reuse them for
a real deployment, which seeds nothing (use scripts/create_admin.py).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import CycleStatus, ReviewCycle, Role, User
from app.security import hash_password_argon2


@dataclass(frozen=True)
class DemoAccount:
    username: str
    password: str
    role: str
    manager: str | None = None  # username of their manager


DEMO_ACCOUNTS = [
    DemoAccount("kavya.iyer", "Lotus-Harbor-82!", Role.HR_ADMIN.value),
    DemoAccount("rajesh.menon", "Cedar-Summit-19!", Role.MANAGER.value),
    DemoAccount("priya.sharma", "Willow-Bridge-47!", Role.MANAGER.value, manager="rajesh.menon"),
    DemoAccount("arjun.nair", "Copper-Falcon-31!", Role.EMPLOYEE.value, manager="priya.sharma"),
    DemoAccount("sneha.kulkarni", "Amber-River-64!", Role.EMPLOYEE.value, manager="priya.sharma"),
    DemoAccount("rohan.desai", "Granite-Echo-58!", Role.EMPLOYEE.value, manager="priya.sharma"),
    DemoAccount("meera.pillai", "Indigo-Meadow-23!", Role.EMPLOYEE.value, manager="priya.sharma"),
    DemoAccount("vikram.reddy", "Silver-Monsoon-76!", Role.EMPLOYEE.value, manager="priya.sharma"),
]


def seed_if_empty(db: Session) -> None:
    if db.query(User).first() is not None:
        return

    # Managers are listed before their reports, so each manager exists
    # (and has an id) by the time someone points at them.
    created: dict[str, User] = {}
    for account in DEMO_ACCOUNTS:
        user = User(
            username=account.username,
            password_hash=hash_password_argon2(account.password),
            role=account.role,
            manager_id=created[account.manager].id if account.manager else None,
        )
        db.add(user)
        db.flush()
        created[account.username] = user

    now = datetime.now(timezone.utc)
    db.add(
        ReviewCycle(
            name="Q1 2026",
            start_date=now,
            end_date=now + timedelta(days=30),
            status=CycleStatus.ACTIVE.value,
        )
    )
    db.commit()
