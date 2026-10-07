"""Regression tests for the input-validation, CSRF, session and audit
hardening -- each one pins a hole that existed before and is now closed."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import security
from app.models import (
    AuditLog,
    CycleStatus,
    PasswordResetToken,
    ReviewCycle,
    ReviewerAssignment,
    Role,
    SKILL_AREAS,
    User,
)
from tests.conftest import login, logout, nominate_and_approve


def _user(db, username) -> User:
    return db.query(User).filter_by(username=username).one()


def _active_cycle(db) -> ReviewCycle:
    return db.query(ReviewCycle).filter_by(status=CycleStatus.ACTIVE.value).one()


def _review_form(notes="Solid quarter.", rating="4") -> dict:
    form = {"additional_notes": notes}
    for area in SKILL_AREAS:
        form[f"{area}_text"] = f"{area} comment"
        form[f"{area}_rating"] = rating
    return form


def _submitted_review(client, db):
    """user2's reviewers are nominated and approved; user1 submits a review for user2. Returns the assignment."""
    nominate_and_approve(client)
    assignment = (
        db.query(ReviewerAssignment)
        .filter_by(reviewee_id=_user(db, "user2").id, reviewer_id=_user(db, "user1").id)
        .one()
    )
    login(client, "user1")
    assert client.post(f"/reviews/write/{assignment.id}", data=_review_form()).status_code == 303
    logout(client)
    return assignment


# --- CSRF -------------------------------------------------------------------

def test_post_without_csrf_token_is_rejected(client):
    login(client, "hr_admin", "admin123")
    r = client.post(
        "/admin/cycles",
        data={"name": "Q2", "start_date": "2026-04-01T00:00", "end_date": "2026-06-30T00:00", "csrf_token": ""},
    )
    assert r.status_code == 403
    assert "form session expired" in r.text.lower()


def test_post_with_wrong_csrf_token_is_rejected(client, db_session):
    login(client, "hr_admin", "admin123")
    cycle = _active_cycle(db_session)
    r = client.post(f"/admin/cycles/{cycle.id}/close", data={"csrf_token": "x" * 43})
    assert r.status_code == 403
    db_session.refresh(cycle)
    assert cycle.status == CycleStatus.ACTIVE.value


def test_login_itself_requires_csrf_token(client):
    r = client.post("/login", data={"username": "user1", "password": "password123", "csrf_token": "nope"})
    assert r.status_code == 403


def test_login_rotates_csrf_token(client):
    client.get("/login")
    before = client.cookies.get("csrf_token")
    login(client, "user1")
    after = client.cookies.get("csrf_token")
    assert before and after and before != after


def test_logout_is_post_only(client):
    login(client, "user1")
    assert client.get("/logout").status_code == 405
    assert logout(client).status_code == 303
    assert client.get("/my/dashboard").status_code == 303  # really logged out


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_missing_form_field_renders_html_error_not_json(client):
    r = client.post("/login", data={"username": "user1"})
    assert r.status_code == 400
    assert "text/html" in r.headers["content-type"]


# --- Reviewer selection -------------------------------------------------------

@pytest.mark.parametrize("bad_pick", ["user1", "hr_admin"])
def test_cannot_select_self_or_hr_admin_as_reviewer(client, db_session, bad_pick):
    login(client, "user1")
    r = client.post("/assignments/select", data={"reviewers": [bad_pick, "user2", "user3"]})
    assert r.status_code == 400
    assert db_session.query(ReviewerAssignment).count() == 0


def test_hr_cannot_assign_hr_admin_as_reviewer(client, db_session):
    login(client, "hr_admin", "admin123")
    r = client.post("/admin/assignments", data={"reviewee": "user1", "reviewer": "hr_admin"})
    assert r.status_code == 400
    assert db_session.query(ReviewerAssignment).count() == 0


# --- Cycles -----------------------------------------------------------------

def test_activating_nonexistent_cycle_404s_and_leaves_active_cycle_alone(client, db_session):
    login(client, "hr_admin", "admin123")
    cycle = _active_cycle(db_session)
    assert client.post("/admin/cycles/999/activate").status_code == 404
    db_session.refresh(cycle)
    assert cycle.status == CycleStatus.ACTIVE.value


def test_closing_nonexistent_cycle_404s(client):
    login(client, "hr_admin", "admin123")
    assert client.post("/admin/cycles/999/close").status_code == 404


@pytest.mark.parametrize(
    "start,end",
    [("garbage", "2026-06-30T00:00"), ("2026-06-30T00:00", "2026-04-01T00:00"), ("2026-04-01T00:00", "2026-04-01T00:00")],
)
def test_invalid_cycle_dates_rerender_with_error(client, db_session, start, end):
    login(client, "hr_admin", "admin123")
    r = client.post("/admin/cycles", data={"name": "Q2", "start_date": start, "end_date": end})
    assert r.status_code == 400
    assert db_session.query(ReviewCycle).count() == 1


def test_activating_a_cycle_closes_the_previous_one(client, db_session):
    login(client, "hr_admin", "admin123")
    old = _active_cycle(db_session)
    client.post("/admin/cycles", data={"name": "Q2", "start_date": "2026-04-01T00:00", "end_date": "2026-06-30T00:00"})
    new = db_session.query(ReviewCycle).filter_by(name="Q2").one()
    assert client.post(f"/admin/cycles/{new.id}/activate").status_code == 303
    db_session.refresh(old)
    db_session.refresh(new)
    assert (old.status, new.status) == (CycleStatus.CLOSED.value, CycleStatus.ACTIVE.value)


def test_database_rejects_two_active_cycles(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(ReviewCycle(name="dup", start_date=now, end_date=now + timedelta(days=1), status="active"))
    with pytest.raises(IntegrityError):
        db_session.commit()


# --- Reviews & closed cycles -------------------------------------------------

def test_cannot_submit_review_in_closed_cycle(client, db_session):
    nominate_and_approve(client)
    cycle = _active_cycle(db_session)
    cycle.status = CycleStatus.CLOSED.value
    db_session.commit()

    assignment = db_session.query(ReviewerAssignment).filter_by(reviewer_id=_user(db_session, "user1").id).one()
    login(client, "user1")
    assert "cycle is closed" in client.get(f"/reviews/write/{assignment.id}").text.lower()
    assert client.post(f"/reviews/write/{assignment.id}", data=_review_form()).status_code == 403
    db_session.refresh(assignment)
    assert assignment.review is None


def test_retract_preserves_full_content_in_audit_log(client, db_session):
    assignment = _submitted_review(client, db_session)
    login(client, "hr_admin", "admin123")
    assert client.post(f"/reviews/{assignment.review.id}/retract").status_code == 303

    entry = db_session.query(AuditLog).filter_by(action="retract_review").one()
    cleared = json.loads(entry.details)["cleared"]
    assert cleared["texts"]["communication"] == "communication comment"
    assert cleared["additional_notes"] == "Solid quarter."
    assert cleared["ratings"]["initiative"] == 4

def _submitted_review_again(client, db):
    """user3 submits their review for user2 (after _submitted_review set up the assignments)."""
    logout(client)
    assignment = (
        db.query(ReviewerAssignment)
        .filter_by(reviewee_id=_user(db, "user2").id, reviewer_id=_user(db, "user3").id)
        .one()
    )
    login(client, "user3")
    client.post(f"/reviews/write/{assignment.id}", data=_review_form())
    logout(client)
    return assignment



def test_cannot_retract_in_closed_cycle_or_retract_twice(client, db_session):
    assignment = _submitted_review(client, db_session)
    review_id = assignment.review.id
    login(client, "hr_admin", "admin123")
    assert client.post(f"/reviews/{review_id}/retract").status_code == 303
    assert client.post(f"/reviews/{review_id}/retract").status_code == 400  # already unlocked

    other = _submitted_review_again(client, db_session)
    _active_cycle(db_session).status = CycleStatus.CLOSED.value
    db_session.commit()
    login(client, "hr_admin", "admin123")
    assert client.post(f"/reviews/{other.review.id}/retract").status_code == 400


# --- Audit atomicity -----------------------------------------------------------

def test_audit_row_rolls_back_with_its_action(db_session):
    from app.audit import log_action

    log_action(db_session, None, "should_vanish", "x:1")
    db_session.rollback()
    assert db_session.query(AuditLog).filter_by(action="should_vanish").count() == 0


# --- Foreign keys, registration ----------------------------------------------

def test_sqlite_enforces_foreign_keys(db_session):
    db_session.add(User(username="orphan", password_hash="argon2id$x", role="employee", manager_id=9999))
    with pytest.raises(IntegrityError):
        db_session.commit()


@pytest.mark.parametrize("manager_id", ["abc", "9999", "<employee>"])  # junk, nonexistent, not a manager
def test_register_rejects_bad_manager_id(client, db_session, manager_id):
    if manager_id == "<employee>":
        manager_id = str(_user(db_session, "user1").id)
    login(client, "hr_admin", "admin123")
    r = client.post(
        "/register", data={"username": "newbie", "role": "employee", "manager_id": manager_id}
    )
    assert r.status_code == 400
    assert db_session.query(User).filter_by(username="newbie").first() is None


def test_register_rejects_bad_username(client, db_session):
    login(client, "hr_admin", "admin123")
    r = client.post("/register", data={"username": "a b<script>", "role": "employee"})
    assert r.status_code == 400


# --- Timing-safe login -------------------------------------------------------

def test_unknown_username_still_runs_a_password_hash(client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.routers.auth.burn_password_check", lambda pw: calls.append(pw))
    r = login(client, "no-such-user", "whatever")
    assert r.status_code == 401
    assert calls == ["whatever"]


def test_login_lockout_via_route(client):
    for _ in range(security.MAX_FAILED_ATTEMPTS):
        assert login(client, "user4", "wrong").status_code == 401
    r = login(client, "user4", "password123")  # right password, but locked out
    assert r.status_code == 429


# --- Session versioning, password change & reset -------------------------------

def test_password_change_signs_out_other_sessions(client, db_session):
    from app.main import app
    from tests.conftest import CSRFAwareClient

    login(client, "user1")
    with CSRFAwareClient(app, follow_redirects=False) as other_device:
        login(other_device, "user1")
        assert other_device.get("/my/dashboard").status_code == 200

        r = client.post(
            "/account/password",
            data={"current_password": "password123", "new_password": "Brand-New-Pw9", "confirm_password": "Brand-New-Pw9"},
        )
        assert r.status_code == 200
        assert client.get("/my/dashboard").status_code == 200  # this browser stays logged in
        assert other_device.get("/my/dashboard").status_code == 303  # the other one is signed out

    logout(client)
    assert login(client, "user1", "password123").status_code == 401
    assert login(client, "user1", "Brand-New-Pw9").status_code == 303


def test_password_change_requires_current_password(client):
    login(client, "user1")
    r = client.post(
        "/account/password",
        data={"current_password": "wrong", "new_password": "Brand-New-Pw9", "confirm_password": "Brand-New-Pw9"},
    )
    assert r.status_code == 400


def _issue_reset_link(client, db, username) -> str:
    login(client, "hr_admin", "admin123")
    r = client.post(f"/admin/users/{_user(db, username).id}/reset-link")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    logout(client)
    return re.search(r"/reset-password/[A-Za-z0-9_-]+", r.text).group(0)


def test_reset_link_flow_is_single_use(client, db_session):
    path = _issue_reset_link(client, db_session, "user3")

    assert client.get(path).status_code == 200
    r = client.post(path, data={"new_password": "Reset-Pw-123", "confirm_password": "Reset-Pw-123"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?notice=reset"

    assert login(client, "user3", "Reset-Pw-123").status_code == 303
    logout(client)
    # second use of the same link fails
    assert client.post(path, data={"new_password": "Again-Pw-123", "confirm_password": "Again-Pw-123"}).status_code == 400


def test_reset_token_is_stored_hashed_and_new_link_voids_old(client, db_session):
    first = _issue_reset_link(client, db_session, "user3")
    raw = first.rsplit("/", 1)[1]
    assert db_session.query(PasswordResetToken).filter_by(token_hash=raw).first() is None  # never stored raw

    second = _issue_reset_link(client, db_session, "user3")
    assert client.get(first).status_code == 400
    assert client.get(second).status_code == 200


def test_expired_reset_link_is_rejected(client, db_session):
    path = _issue_reset_link(client, db_session, "user3")
    token = db_session.query(PasswordResetToken).one()
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert client.get(path).status_code == 400


def test_reset_audit_never_contains_the_token(client, db_session):
    path = _issue_reset_link(client, db_session, "user3")
    raw = path.rsplit("/", 1)[1]
    for entry in db_session.query(AuditLog).all():
        assert raw not in (entry.details or "") and raw not in entry.target


# --- Multi-level hierarchy ------------------------------------------------------

def test_director_sees_skip_level_reports(client):
    login(client, "director1")
    r = client.get("/manager/reports")
    assert r.status_code == 200
    page = r.text.lower()  # names display as "User1"; compare case-insensitively
    assert "manager1" in page and "direct" in page
    for name in ["user1", "user2", "user3", "user4"]:
        assert name in page
    assert "skip-level (2)" in r.text


def test_manager_does_not_see_their_own_manager(client):
    login(client, "manager1")
    r = client.get("/manager/reports")
    assert "director1" not in r.text.split("<tbody>")[1].lower()  # not listed as a report


def test_reporting_loop_is_rejected(client, db_session):
    login(client, "hr_admin", "admin123")
    director = _user(db_session, "director1")
    manager1 = _user(db_session, "manager1")
    # director1 -> reports to manager1, who already reports to director1
    r = client.post(f"/admin/users/{director.id}", data={"role": "manager", "manager_id": str(manager1.id)})
    assert r.status_code == 400
    assert "loop" in r.text.lower()
    db_session.refresh(director)
    assert director.manager_id is None


def test_cannot_demote_manager_with_reports_or_change_own_role(client, db_session):
    login(client, "hr_admin", "admin123")
    manager1 = _user(db_session, "manager1")
    assert client.post(f"/admin/users/{manager1.id}", data={"role": "employee"}).status_code == 400
    admin = _user(db_session, "hr_admin")
    assert client.post(f"/admin/users/{admin.id}", data={"role": "employee"}).status_code == 400


def test_hr_can_reassign_manager(client, db_session):
    login(client, "hr_admin", "admin123")
    user1 = _user(db_session, "user1")
    director = _user(db_session, "director1")
    r = client.post(f"/admin/users/{user1.id}", data={"role": "employee", "manager_id": str(director.id)})
    assert r.status_code == 200
    db_session.refresh(user1)
    assert user1.manager_id == director.id
    assert db_session.query(AuditLog).filter_by(action="update_user").count() == 1


# --- Pagination -----------------------------------------------------------------

def test_users_list_paginates(client, db_session):
    for i in range(25):
        db_session.add(User(username=f"bulk{i:02d}", password_hash="argon2id$x", role=Role.EMPLOYEE.value))
    db_session.commit()  # 7 seeded + 25 = 32 users -> 2 pages of 20

    login(client, "hr_admin", "admin123")
    first = client.get("/admin/users")
    second = client.get("/admin/users?page=2")
    assert "Page 1 of 2" in first.text and "Page 2 of 2" in second.text
    assert "bulk00" in first.text and "bulk00" not in second.text
    assert "user4" in second.text  # alphabetically last, so on page 2

    assert client.get("/admin/users?page=junk").status_code == 200
    assert "Page 2 of 2" in client.get("/admin/users?page=999").text  # clamped to last page


def test_admin_dashboard_totals_cover_all_pages(client, db_session):
    cycle = _active_cycle(db_session)
    people = [User(username=f"p{i:02d}", password_hash="argon2id$x", role=Role.EMPLOYEE.value) for i in range(26)]
    db_session.add_all(people)
    db_session.flush()
    for a, b in zip(people, people[1:]):
        db_session.add(ReviewerAssignment(cycle_id=cycle.id, reviewee_id=a.id, reviewer_id=b.id))
    db_session.commit()  # 25 assignments -> 2 pages

    login(client, "hr_admin", "admin123")
    r = client.get("/admin/dashboard?page=2")
    assert "Peer reviews: 0 of 25 reviews submitted" in r.text  # the ring's accessible label
    assert "0/25</span> peer reviews submitted" in r.text
    assert "Page 2 of 2" in r.text


def test_dashboard_shows_name_and_position(client):
    login(client, "director1")
    body = client.get("/my/dashboard").text
    assert "Welcome, Director1" in body and "<strong>Director</strong>" in body
    assert "Your team" in body and "1 direct report" in body
    logout(client)
    login(client, "manager1")
    body = client.get("/my/dashboard").text
    assert "<strong>Manager</strong>" in body and "reports to Director1" in body and "4 direct reports" in body
    logout(client)
    login(client, "user1")
    body = client.get("/my/dashboard").text
    assert "<strong>Employee</strong>" in body and "Your team" not in body


def test_retract_button_hidden_once_final_review_released(client, db_session):
    from app.models import FinalReview, ReviewCycle, ReviewerAssignment

    nominate_and_approve(client)
    a = db_session.query(ReviewerAssignment).first()
    login(client, "user1")
    form = {"additional_notes": "ok"}
    for area in SKILL_AREAS:
        form[f"{area}_text"], form[f"{area}_rating"] = "good", "4"
    client.post(f"/reviews/write/{a.id}", data=form)
    logout(client)

    login(client, "hr_admin", "admin123")
    assert "/retract" in client.get("/admin/dashboard").text
    db_session.add(FinalReview(cycle_id=db_session.query(ReviewCycle).one().id, employee_id=a.reviewee_id,
                               manager_id=a.reviewee.manager_id, status="released"))
    db_session.commit()
    body = client.get("/admin/dashboard").text
    assert "/retract" not in body and "final review released" in body
