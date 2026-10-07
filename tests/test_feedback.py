"""Reviewer approval, anonymity, and the manager's merged final review."""

import json
import re

import pytest

from app.models import (
    AssignmentSource,
    AssignmentStatus,
    AuditLog,
    CycleStatus,
    FinalReview,
    ReviewCycle,
    ReviewerAssignment,
    SKILL_AREAS,
    User,
)
from tests.conftest import login, logout, nominate_and_approve

REVIEWERS = ("user1", "user3", "user4")


def _user(db, username) -> User:
    return db.query(User).filter_by(username=username).one()


def _assignment(db, reviewee, reviewer) -> ReviewerAssignment:
    return (
        db.query(ReviewerAssignment)
        .filter_by(reviewee_id=_user(db, reviewee).id, reviewer_id=_user(db, reviewer).id)
        .one()
    )


def _submit(client, db, reviewer, reviewee="user2", rating="4", comment=None):
    a = _assignment(db, reviewee, reviewer)
    form = {"additional_notes": f"note from {reviewer}-secret"}
    for area in SKILL_AREAS:
        form[f"{area}_text"] = comment or f"{area} comment by {reviewer}-secret"
        form[f"{area}_rating"] = rating
    login(client, reviewer)
    r = client.post(f"/reviews/write/{a.id}", data=form)
    logout(client)
    return r


def _release(client, reviewee="user2", cycle_id="1", **overrides):
    data = {"cycle_id": cycle_id, "action": "release", "summary": "Strong quarter overall.",
            "strengths": "Reliable", "improvements": "Delegate more", "final_rating": "4"}
    data.update(overrides)
    return client.post(f"/team/reviews/{reviewee}", data=data)


# --- Nomination & approval ------------------------------------------------------

def test_nominations_start_pending_and_cannot_be_written(client, db_session):
    login(client, "user2")
    assert client.post("/assignments/select", data={"reviewers": list(REVIEWERS)}).status_code == 303
    logout(client)

    a = _assignment(db_session, "user2", "user1")
    assert (a.status, a.source) == (AssignmentStatus.PENDING.value, AssignmentSource.NOMINATED.value)

    login(client, "user1")
    assert f"/reviews/write/{a.id}" not in client.get("/reviews/pick").text
    assert client.get(f"/reviews/write/{a.id}").status_code == 403


def test_approval_makes_nomination_writable_and_is_audited(client, db_session):
    nominate_and_approve(client)
    a = _assignment(db_session, "user2", "user1")
    assert a.status == AssignmentStatus.APPROVED.value
    assert db_session.query(AuditLog).filter_by(action="approve_reviewer").count() == 3

    login(client, "user1")
    assert f"/reviews/write/{a.id}" in client.get("/reviews/pick").text


@pytest.mark.parametrize("names", [["user1", "user3"], ["user1", "user3", "user4", "manager1", "director1", "extra"]])
def test_nomination_count_must_be_three_to_five(client, names):
    login(client, "user2")
    assert client.post("/assignments/select", data={"reviewers": names}).status_code == 400


def test_five_nominations_allowed_and_only_once_per_cycle(client):
    login(client, "user2")
    five = ["user1", "user3", "user4", "manager1", "director1"]
    assert client.post("/assignments/select", data={"reviewers": five}).status_code == 303
    assert client.post("/assignments/select", data={"reviewers": ["user1", "user3", "user4"]}).status_code == 400


def test_only_direct_manager_or_hr_can_decide(client, db_session):
    login(client, "user2")
    client.post("/assignments/select", data={"reviewers": list(REVIEWERS)})
    logout(client)
    a = _assignment(db_session, "user2", "user1")

    login(client, "director1")  # skip-level manager
    assert client.post(f"/team/approvals/{a.id}/approve").status_code == 403
    logout(client)
    login(client, "user3")  # an employee
    assert client.post(f"/team/approvals/{a.id}/approve").status_code == 403
    logout(client)
    login(client, "hr_admin", "admin123")  # HR can override
    assert client.post(f"/team/approvals/{a.id}/approve").status_code == 303


def test_hr_approves_for_people_without_a_manager(client, db_session):
    login(client, "director1")  # top of the org: no manager
    client.post("/assignments/select", data={"reviewers": ["manager1", "user1", "user2"]})
    logout(client)
    a = _assignment(db_session, "director1", "manager1")

    login(client, "manager1")
    assert client.post(f"/team/approvals/{a.id}/approve").status_code == 403
    logout(client)
    login(client, "hr_admin", "admin123")
    assert client.post(f"/team/approvals/{a.id}/approve").status_code == 303


def test_reject_then_manager_adds_replacement(client, db_session):
    login(client, "user2")
    client.post("/assignments/select", data={"reviewers": list(REVIEWERS)})
    logout(client)
    rejected = _assignment(db_session, "user2", "user4")

    login(client, "manager1")
    assert client.post(f"/team/approvals/{rejected.id}/reject").status_code == 303
    assert client.post(f"/team/approvals/{rejected.id}/approve").status_code == 400  # already decided
    assert client.post("/team/reviewers/add", data={"reviewee": "user2", "reviewer": "director1"}).status_code == 303
    logout(client)

    added = _assignment(db_session, "user2", "director1")
    assert (added.status, added.source) == (AssignmentStatus.APPROVED.value, AssignmentSource.MANAGER.value)
    db_session.refresh(rejected)
    assert rejected.status == AssignmentStatus.REJECTED.value

    login(client, "user4")
    assert client.get(f"/reviews/write/{rejected.id}").status_code == 403


def test_manager_cannot_add_self_review_or_hr(client):
    login(client, "manager1")
    assert client.post("/team/reviewers/add", data={"reviewee": "user2", "reviewer": "user2"}).status_code == 400
    assert client.post("/team/reviewers/add", data={"reviewee": "user2", "reviewer": "hr_admin"}).status_code == 400
    # not my report
    assert client.post("/team/reviewers/add", data={"reviewee": "manager1", "reviewer": "user1"}).status_code == 403


def test_hr_assignment_does_not_block_self_nomination(client, db_session):
    login(client, "hr_admin", "admin123")
    client.post("/admin/assignments", data={"reviewee": "user2", "reviewer": "director1"})
    logout(client)
    hr_added = _assignment(db_session, "user2", "director1")
    assert (hr_added.status, hr_added.source) == (AssignmentStatus.APPROVED.value, AssignmentSource.HR.value)

    login(client, "user2")
    assert client.post("/assignments/select", data={"reviewers": list(REVIEWERS)}).status_code == 303


def test_employee_never_learns_approval_outcomes_or_manager_additions(client):
    login(client, "user2")
    client.post("/assignments/select", data={"reviewers": list(REVIEWERS)})
    logout(client)
    login(client, "manager1")
    page = client.get("/team/approvals").text
    ids = re.findall(r"/team/approvals/(\d+)/(?:approve)", page)
    client.post(f"/team/approvals/{ids[0]}/reject")
    for i in ids[1:]:
        client.post(f"/team/approvals/{i}/approve")
    client.post("/team/reviewers/add", data={"reviewee": "user2", "reviewer": "director1"})
    logout(client)

    login(client, "user2")
    body = client.get("/assignments/select").text.split("<main>")[1]
    assert ">rejected<" not in body and ">approved<" not in body  # no per-nominee outcome badges
    assert "finalized" in body  # only the overall state
    assert "director1" not in body.lower()  # the manager's addition stays hidden (case-insensitive: names display as "Director1")


# --- Anonymity ---------------------------------------------------------------------

def test_reviewee_cannot_read_individual_peer_reviews(client, db_session):
    nominate_and_approve(client)
    for r in REVIEWERS:
        _submit(client, db_session, r)

    login(client, "user2")
    assert client.get("/reviews/view/user2").status_code == 403
    body = client.get("/my/review").text
    assert "No review yet" in body
    assert "secret" not in body


def test_manager_merge_page_is_anonymous_and_averages_correctly(client, db_session):
    nominate_and_approve(client)
    for reviewer, rating in zip(REVIEWERS, ["3", "4", "5"]):
        _submit(client, db_session, reviewer, rating=rating)

    login(client, "manager1")
    body = client.get("/team/reviews/user2").text.split("<main>")[1]
    for reviewer in REVIEWERS:
        assert reviewer + "-secret" in body  # every comment is merged in...
        assert f">{reviewer}<" not in body.lower() and f" {reviewer} " not in body.lower()  # ...but never attributed
    assert "Submitted" in body and "3<small>/3</small>" in body
    assert "4.0<span" in body  # per-skill average of 3, 4, 5
    assert "suggested from peer average: 4" in body


def test_only_direct_manager_sees_merged_feedback(client):
    login(client, "director1")  # skip-level
    assert client.get("/team/reviews/user2").status_code == 403
    logout(client)
    login(client, "user1")
    assert client.get("/team/reviews/user2").status_code == 403
    logout(client)
    login(client, "hr_admin", "admin123")
    assert client.get("/team/reviews/user2").status_code == 403


# --- Final review release -----------------------------------------------------------

def test_release_needs_minimum_responses(client, db_session):
    nominate_and_approve(client)
    _submit(client, db_session, "user1")
    _submit(client, db_session, "user3")

    login(client, "manager1")
    r = _release(client)
    assert r.status_code == 400
    assert "at least 3" in r.text.lower()
    assert db_session.query(FinalReview).count() == 0


def test_release_needs_summary_and_rating(client, db_session):
    nominate_and_approve(client)
    for r in REVIEWERS:
        _submit(client, db_session, r)
    login(client, "manager1")
    assert _release(client, summary="").status_code == 400
    assert _release(client, final_rating="").status_code == 400
    assert _release(client, final_rating="9").status_code == 400


def test_draft_is_invisible_to_employee(client, db_session):
    nominate_and_approve(client)
    for r in REVIEWERS:
        _submit(client, db_session, r)
    login(client, "manager1")
    assert _release(client, action="save").status_code == 303
    logout(client)

    login(client, "user2")
    assert "Strong quarter overall" not in client.get("/my/review").text


def test_released_review_shows_summary_and_frozen_averages_only(client, db_session):
    nominate_and_approve(client)
    for reviewer, rating in zip(REVIEWERS, ["3", "4", "5"]):
        _submit(client, db_session, reviewer, rating=rating)
    login(client, "manager1")
    assert _release(client).status_code == 303
    logout(client)

    final = db_session.query(FinalReview).one()
    assert final.is_released
    snapshot = json.loads(final.stats_snapshot)
    assert snapshot["responses"] == 3 and snapshot["skills"]["communication"] == 4.0
    assert db_session.query(AuditLog).filter_by(action="release_final_review").count() == 1

    login(client, "user2")
    body = client.get("/my/review").text
    assert "Strong quarter overall." in body and "Delegate more" in body
    assert "combined from 3 colleagues" in body
    assert "secret" not in body  # no individual comment or note leaks through
    for reviewer in REVIEWERS:
        assert reviewer not in body.split("<main>")[1].lower()


def test_release_locks_everything_downstream(client, db_session):
    nominate_and_approve(client, reviewers=("user1", "user3", "user4", "director1"))
    for r in REVIEWERS:
        _submit(client, db_session, r)
    login(client, "manager1")
    assert _release(client).status_code == 303

    # the final review itself is locked
    assert _release(client, summary="changed").status_code == 403
    # no more reviewers can be added
    assert client.post("/team/reviewers/add", data={"reviewee": "user2", "reviewer": "manager1"}).status_code == 400
    logout(client)

    # a late peer review is refused rather than silently ignored
    assert _submit(client, db_session, "director1").status_code == 403

    # HR can no longer retract a review the final one was built from
    login(client, "hr_admin", "admin123")
    review_id = _assignment(db_session, "user2", "user1").review.id
    assert client.post(f"/reviews/{review_id}/retract").status_code == 400


def test_manager_can_still_finalize_after_cycle_closes(client, db_session):
    nominate_and_approve(client)
    for r in REVIEWERS:
        _submit(client, db_session, r)
    cycle = db_session.query(ReviewCycle).one()
    cycle.status = CycleStatus.CLOSED.value
    db_session.commit()

    login(client, "manager1")
    assert _release(client).status_code == 303


def test_manager_team_page_links_direct_reports_only(client):
    login(client, "director1")
    body = client.get("/manager/reports").text
    assert "/team/reviews/manager1" in body  # direct report
    assert "/team/reviews/user1" not in body  # skip-level: status only


# --- People with no manager (top of the org) -----------------------------------

def test_hr_writes_final_review_for_someone_with_no_manager(client, db_session):
    # director1 has no manager: HR approves their reviewers *and* writes
    # their final review, so their feedback isn't stranded.
    nominate_and_approve(
        client, reviewee="director1", reviewers=("manager1", "user1", "user2"), approver=("hr_admin", "admin123")
    )
    for reviewer in ("manager1", "user1", "user2"):
        assert _submit(client, db_session, reviewer, reviewee="director1").status_code == 303

    login(client, "hr_admin", "admin123")
    assert "/team/reviews/director1" in client.get("/team/approvals").text
    assert client.get("/team/reviews/director1").status_code == 200
    assert _release(client, reviewee="director1").status_code == 303
    logout(client)

    final = db_session.query(FinalReview).one()
    assert final.manager_id == _user(db_session, "hr_admin").id  # HR is recorded as the author

    login(client, "director1")
    body = client.get("/my/review").text
    assert "Strong quarter overall." in body
    assert "secret" not in body


def test_only_hr_can_write_for_someone_with_no_manager(client):
    login(client, "manager1")  # director1's own report can't review their boss's final review
    assert client.get("/team/reviews/director1").status_code == 403
    logout(client)
    login(client, "hr_admin", "admin123")
    assert client.get("/team/reviews/hr_admin").status_code == 403  # HR admins aren't reviewed
