import re

from app.models import SKILL_AREAS
from tests.conftest import login, logout, nominate_and_approve


def _extract_assignment_id(html: str) -> str:
    match = re.search(r"/reviews/write/(\d+)", html)
    assert match, html
    return match.group(1)


def _review_form_data(notes: str, rating: str = "4") -> dict:
    form = {"additional_notes": notes}
    for area in SKILL_AREAS:
        form[f"{area}_text"] = "solid work"
        form[f"{area}_rating"] = rating
    return form


def test_login_wrong_password_shows_error(client):
    r = login(client, "user1", "wrong-password")
    assert r.status_code == 401
    assert "invalid" in r.text.lower()


def test_login_success_redirects_home(client):
    r = login(client, "user1")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_full_review_workflow_locks_and_computes_overall_rating(client):
    nominate_and_approve(client)

    login(client, "user1")
    r = client.get("/reviews/pick")
    assignment_id = _extract_assignment_id(r.text)

    r = client.post(f"/reviews/write/{assignment_id}", data=_review_form_data("Great teammate."))
    assert r.status_code == 303

    # locked: GET shows read-only, POST is rejected
    r = client.get(f"/reviews/write/{assignment_id}")
    assert "locked" in r.text.lower()

    r = client.post(f"/reviews/write/{assignment_id}", data=_review_form_data("trying again"))
    assert r.status_code == 403
    logout(client)

    # The reviewee can't read individual peer reviews (that's what keeps
    # them anonymous); HR can, read-only.
    login(client, "user2")
    assert client.get("/reviews/view/user2").status_code == 403
    logout(client)
    login(client, "hr_admin", "admin123")
    r = client.get("/reviews/view/user2")
    assert r.status_code == 200
    assert "Great teammate" in r.text
    assert "4.0" in r.text  # overall_rating computed server-side from all 4-star ratings


def test_reviewer_cannot_write_for_someone_elses_assignment(client):
    nominate_and_approve(client)

    login(client, "user1")
    r = client.get("/reviews/pick")
    assignment_id = _extract_assignment_id(r.text)
    logout(client)

    login(client, "user3")  # not the reviewer on this assignment
    r = client.get(f"/reviews/write/{assignment_id}")
    assert r.status_code == 403


def test_profanity_filter_blocks_submission_and_does_not_lock(client):
    nominate_and_approve(client)

    login(client, "user1")
    r = client.get("/reviews/pick")
    assignment_id = _extract_assignment_id(r.text)

    r = client.post(f"/reviews/write/{assignment_id}", data=_review_form_data("You are so dumb sometimes."))
    assert r.status_code == 400
    assert "inappropriate language" in r.text.lower()

    # word-boundary matching: "made" should NOT trip the "mad" filter
    r = client.post(f"/reviews/write/{assignment_id}", data=_review_form_data("They made great progress this quarter."))
    assert r.status_code == 303  # accepted, not flagged


def test_reviewee_with_no_reviewers_selected_sees_empty_state_not_error(client):
    login(client, "manager1")  # nobody has selected manager1 as a reviewer in this fresh DB
    r = client.get("/reviews/pick")
    assert r.status_code == 200
    assert "nothing" in r.text.lower() or "reviewer yet" in r.text.lower()


def test_cannot_select_the_same_reviewer_twice(client):
    login(client, "user1")
    r = client.post(
        "/assignments/select",
        data={"reviewers": ["user2", "user2", "user3"]},
    )
    assert r.status_code == 400
    assert "different" in r.text.lower()


def test_hr_admin_retract_unlocks_review_and_is_audited(client, db_session):
    nominate_and_approve(client)

    login(client, "user1")
    r = client.get("/reviews/pick")
    assignment_id = _extract_assignment_id(r.text)
    client.post(f"/reviews/write/{assignment_id}", data=_review_form_data("Solid quarter."))
    logout(client)

    login(client, "hr_admin", "admin123")
    r = client.get("/admin/dashboard")
    review_id = re.search(r"/reviews/(\d+)/retract", r.text).group(1)
    r = client.post(f"/reviews/{review_id}/retract")
    assert r.status_code == 303
    logout(client)

    login(client, "user1")
    r = client.get(f"/reviews/write/{assignment_id}")
    assert "locked" not in r.text.lower()

    from app.models import AuditLog

    actions = [a.action for a in db_session.query(AuditLog).all()]
    assert "retract_review" in actions
    assert "submit_review" in actions
