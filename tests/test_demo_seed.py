"""The app's demo data (app/seed.py), checked on its own fresh database --
the rest of the suite uses tests/fixture_org.py instead."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import security
from app.database import Base
from app.models import ReviewCycle, Role, User
from app.org import position_title
from app.seed import DEMO_ACCOUNTS, seed_if_empty
from app.services import password_problem


@pytest.fixture()
def demo_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'demo.db'}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    seed_if_empty(session)
    yield session
    session.close()
    engine.dispose()


def _user(db, username) -> User:
    return db.query(User).filter_by(username=username).one()


def test_demo_org_has_everyone_with_the_right_role_and_manager(demo_db):
    assert {u.username for u in demo_db.query(User)} == {a.username for a in DEMO_ACCOUNTS}
    assert len(DEMO_ACCOUNTS) == 8
    for account in DEMO_ACCOUNTS:
        user = _user(demo_db, account.username)
        assert user.role == account.role
        assert (user.manager.username if user.manager else None) == account.manager


def test_demo_org_shape(demo_db):
    roles = [u.role for u in demo_db.query(User)]
    assert roles.count(Role.HR_ADMIN.value) == 1
    assert roles.count(Role.MANAGER.value) == 2
    assert roles.count(Role.EMPLOYEE.value) == 5
    assert {u.username for u in _user(demo_db, "priya.sharma").direct_reports} == {
        "arjun.nair", "sneha.kulkarni", "rohan.desai", "meera.pillai", "vikram.reddy",
    }
    assert demo_db.query(ReviewCycle).one().status == "active"


@pytest.mark.parametrize("account", DEMO_ACCOUNTS, ids=lambda a: a.username)
def test_every_demo_password_meets_the_password_rules(account):
    assert password_problem(account.password, account.username) is None


def test_demo_passwords_are_all_different():
    assert len({a.password for a in DEMO_ACCOUNTS}) == len(DEMO_ACCOUNTS)


def test_demo_passwords_are_hashed_with_argon2id_and_verify(demo_db):
    for account in DEMO_ACCOUNTS:
        user = _user(demo_db, account.username)
        assert user.password_hash.startswith(security.ARGON2_PREFIX)
        assert security.verify_password(demo_db, user, account.password)
        assert not security.verify_password(demo_db, user, "password123")


def test_positions_reflect_the_org(demo_db):
    assert position_title(_user(demo_db, "kavya.iyer")) == "HR Admin"
    assert position_title(_user(demo_db, "rajesh.menon")) == "Director"  # manages a manager
    assert position_title(_user(demo_db, "priya.sharma")) == "Manager"
    assert position_title(_user(demo_db, "arjun.nair")) == "Employee"


def test_seed_does_nothing_on_a_database_that_has_users(demo_db):
    seed_if_empty(demo_db)
    assert demo_db.query(User).count() == len(DEMO_ACCOUNTS)


def test_display_names_come_from_usernames(demo_db):
    assert _user(demo_db, "priya.sharma").display_name == "Priya Sharma"
    assert _user(demo_db, "sneha.kulkarni").display_name == "Sneha Kulkarni"
