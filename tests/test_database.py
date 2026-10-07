import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Review, ReviewCycle, ReviewerAssignment, Role, User
from app.security import hash_password_argon2


def test_fixture_org_has_expected_users(db_session):
    usernames = {u.username for u in db_session.query(User).all()}
    assert usernames == {"hr_admin", "director1", "manager1", "user1", "user2", "user3", "user4"}


def test_fixture_org_has_one_active_cycle(db_session):
    cycles = db_session.query(ReviewCycle).all()
    assert len(cycles) == 1
    assert cycles[0].status == "active"


def test_username_must_be_unique(db_session):
    db_session.add(User(username="user1", password_hash=hash_password_argon2("x"), role=Role.EMPLOYEE.value))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_assignment_triplet_is_unique(db_session):
    cycle = db_session.query(ReviewCycle).first()
    user1 = db_session.query(User).filter_by(username="user1").first()
    user2 = db_session.query(User).filter_by(username="user2").first()

    db_session.add(ReviewerAssignment(cycle_id=cycle.id, reviewee_id=user1.id, reviewer_id=user2.id))
    db_session.commit()

    db_session.add(ReviewerAssignment(cycle_id=cycle.id, reviewee_id=user1.id, reviewer_id=user2.id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_review_is_one_to_one_with_assignment(db_session):
    cycle = db_session.query(ReviewCycle).first()
    user1 = db_session.query(User).filter_by(username="user1").first()
    user2 = db_session.query(User).filter_by(username="user2").first()

    assignment = ReviewerAssignment(cycle_id=cycle.id, reviewee_id=user1.id, reviewer_id=user2.id)
    db_session.add(assignment)
    db_session.commit()

    db_session.add(Review(assignment_id=assignment.id))
    db_session.commit()

    db_session.add(Review(assignment_id=assignment.id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_manager_direct_reports_relationship(db_session):
    manager = db_session.query(User).filter_by(username="manager1").first()
    reports = {u.username for u in manager.direct_reports}
    assert reports == {"user1", "user2", "user3", "user4"}
