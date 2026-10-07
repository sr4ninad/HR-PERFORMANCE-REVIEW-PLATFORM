"""Frontend plumbing: toasts, theme + htmx wiring, rings, steppers, and the
audit-log timeline."""

import json
import re

import pytest

from app.flash import FLASH_COOKIE
from app.models import AuditLog, ReviewCycle, User
from app.progress import RING_CIRCUMFERENCE, Ring, cycle_overview, my_progress
from tests.conftest import login, logout, nominate_and_approve


def _user(db, username) -> User:
    return db.query(User).filter_by(username=username).one()


# --- Toasts --------------------------------------------------------------------------

def test_toast_shows_once_after_a_redirect_then_clears(client):
    r = login(client, "user2")
    assert FLASH_COOKIE in r.headers.get("set-cookie", "")
    page = client.get("/my/dashboard")
    assert "Welcome back, User2." in page.text
    assert client.get("/my/dashboard").text.count("Welcome back") == 0  # shown once only


def test_action_toast_names_the_people_involved(client):
    login(client, "user2")
    client.get("/my/dashboard")  # consume the welcome toast
    client.post("/assignments/select", data={"reviewers": ["user1", "user3", "user4"]})
    assert "Nominations sent to Manager1 for approval." in client.get("/my/dashboard").text
    logout(client)

    login(client, "manager1")
    client.get("/my/dashboard")
    page = client.get("/team/approvals").text
    assignment_id = re.search(r"/team/approvals/(\d+)/approve", page).group(1)
    client.post(f"/team/approvals/{assignment_id}/approve")
    assert re.search(r"Approved User\d as a reviewer for User2\.", client.get("/team/approvals").text)


def test_forged_flash_cookie_is_ignored(client):
    login(client, "user1")
    client.get("/my/dashboard")
    client.cookies.set(FLASH_COOKIE, "WyJzdWNjZXNzIiwiWW91IHdvbiEiXQ")  # unsigned ["success","You won!"]
    assert "You won!" not in client.get("/my/dashboard").text


def test_toasts_escape_their_text(client):
    # Even a correctly signed toast is rendered as text, never as HTML.
    from app.flash import _signer

    login(client, "user1")
    client.get("/my/dashboard")
    client.cookies.set(FLASH_COOKIE, _signer.dumps(["success", "<b onmouseover=x>hi</b>"]))
    toasts = client.get("/my/dashboard").text.split('id="toasts"')[1]
    assert "<b onmouseover" not in toasts and "&lt;b onmouseover=x&gt;hi&lt;/b&gt;" in toasts


# --- Theme, htmx, transitions wiring ----------------------------------------------------

def test_base_page_wires_theme_htmx_and_toasts(client):
    body = client.get("/login").text
    assert 'data-theme-toggle' in body
    assert 'localStorage.getItem("theme")' in body  # applied before first paint
    assert '<body hx-boost="true">' in body
    assert '/static/vendor/htmx.min.js' in body and '/static/app.js' in body
    config = json.loads(re.search(r"name=\"htmx-config\" content='([^']+)'", body).group(1))
    assert config["historyCacheSize"] == 0  # never snapshot pages (with review content) into localStorage
    assert config["globalViewTransitions"] is True
    assert any(rule["code"] == "[2345].." and rule["swap"] for rule in config["responseHandling"])
    assert 'id="toasts"' in body and 'aria-live="polite"' in body


def test_static_assets_are_served_locally(client):
    htmx = client.get("/static/vendor/htmx.min.js")
    assert htmx.status_code == 200 and "htmx" in htmx.text[:200]
    assert client.get("/static/app.js").status_code == 200
    css = client.get("/static/style.css").text
    assert ':root[data-theme="light"]' in css
    assert "@media (prefers-color-scheme: light)" in css
    assert "@view-transition" in css
    assert "prefers-reduced-motion" in css


def test_retract_confirm_lives_on_the_button(client):
    # onclick-confirm on the button works with and without htmx; an onsubmit
    # handler could be bypassed by htmx's own submit listener.
    login(client, "hr_admin", "admin123")
    body = client.get("/admin/dashboard").text
    assert "onsubmit" not in body


# --- Rings and steppers --------------------------------------------------------------------

def test_ring_geometry():
    assert Ring("x", "y", 0, 4).offset == RING_CIRCUMFERENCE
    assert Ring("x", "y", 2, 4).offset == round(RING_CIRCUMFERENCE / 2, 2)
    assert Ring("x", "y", 4, 4).offset == 0 and Ring("x", "y", 4, 4).complete
    assert Ring("x", "y", 0, 0).ratio == 0 and not Ring("x", "y", 0, 0).complete  # nothing to do isn't "complete"
    assert Ring("x", "y", 9, 4).ratio == 1  # capped


def test_cycle_overview_tracks_the_pipeline(client, db_session):
    cycle = db_session.query(ReviewCycle).one()
    steps = {s.name: s.state for s in cycle_overview(db_session, cycle)["steps"]}
    assert steps == {"Open": "done", "Nominate": "todo", "Approve": "todo", "Review": "todo",
                     "Finalize": "todo", "Close": "todo"}

    login(client, "user2")
    client.post("/assignments/select", data={"reviewers": ["user1", "user3", "user4"]})
    logout(client)
    overview = cycle_overview(db_session, cycle)
    steps = {s.name: s.state for s in overview["steps"]}
    assert steps["Nominate"] == "current" and steps["Approve"] == "current"
    rings = {r.label: (r.value, r.total) for r in overview["rings"]}
    assert rings["Nominated"] == (1, 6)  # 6 non-HR people in the fixture org
    assert rings["Approvals"] == (0, 3)


def test_admin_dashboard_shows_rings_and_stepper(client):
    nominate_and_approve(client)
    login(client, "hr_admin", "admin123")
    body = client.get("/admin/dashboard").text
    assert 'aria-label="Cycle progress"' in body
    assert "Approvals: 3 of 3 nominations decided" in body
    assert body.count('class="ring') >= 4
    assert "Complete" in body  # the approvals ring is full


def test_my_progress_hides_review_counts(client, db_session):
    cycle = db_session.query(ReviewCycle).one()
    user2 = _user(db_session, "user2")
    assert [s.state for s in my_progress(db_session, cycle, user2)] == ["current", "todo", "todo", "todo"]
    nominate_and_approve(client)
    steps = my_progress(db_session, cycle, user2)
    assert [s.state for s in steps] == ["done", "done", "current", "todo"]
    assert all(not re.search(r"\d+ of \d+", s.note) for s in steps)  # no "2 of 3" timing leak

    login(client, "user2")
    body = client.get("/my/dashboard").text
    assert 'aria-label="Your review progress"' in body and "Written anonymously" in body


# --- Audit timeline --------------------------------------------------------------------------

def test_audit_timeline_is_hr_only(client):
    assert client.get("/admin/audit").status_code == 303  # anonymous -> login
    for username in ["user1", "manager1"]:
        login(client, username)
        assert client.get("/admin/audit").status_code == 403
        logout(client)
    login(client, "hr_admin", "admin123")
    assert client.get("/admin/audit").status_code == 200


def test_audit_timeline_reads_like_sentences(client):
    nominate_and_approve(client)
    login(client, "hr_admin", "admin123")
    text = re.sub(r"<[^>]+>", "", client.get("/admin/audit").text)
    text = re.sub(r"\s+", " ", text)
    assert "User2 nominated 3 reviewers for Q1 2026" in text
    assert re.search(r"Manager1 approved User\d as a reviewer for User2", text)
    assert "Manager1 signed in" in text


def test_audit_timeline_escapes_attacker_supplied_usernames(client):
    client.post("/login", data={"username": "<script>alert(1)</script>", "password": "x"})
    login(client, "hr_admin", "admin123")
    body = client.get("/admin/audit").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "Failed sign-in" in body


def test_audit_timeline_filters_by_category_and_keeps_it_across_pages(client, db_session):
    for i in range(35):
        db_session.add(AuditLog(actor_id=None, action="create_cycle", target=f"cycle:{i + 100}", details=json.dumps({"name": f"C{i}"})))
    db_session.commit()
    login(client, "hr_admin", "admin123")
    body = client.get("/admin/audit?category=cycles").text
    assert "signed in" not in body  # sign-ins filtered out
    assert 'href="/admin/audit?category=cycles&amp;page=2"' in body
    assert client.get("/admin/audit?category=nonsense").status_code == 200  # unknown filter = all


def test_retract_entry_offers_the_preserved_content(client, db_session):
    from app.models import SKILL_AREAS, ReviewerAssignment

    nominate_and_approve(client)
    a = db_session.query(ReviewerAssignment).first()
    login(client, a.reviewer.username)
    form = {"additional_notes": "kept for the record"}
    for area in SKILL_AREAS:
        form[f"{area}_text"], form[f"{area}_rating"] = "fine", "4"
    client.post(f"/reviews/write/{a.id}", data=form)
    logout(client)
    login(client, "hr_admin", "admin123")
    db_session.refresh(a)
    client.post(f"/reviews/{a.review.id}/retract")
    body = client.get("/admin/audit?category=reviews").text
    assert "Recorded details" in body and "kept for the record" in body


def test_favicon_is_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 301 and r.headers["location"] == "/static/favicon.svg"
    assert client.get("/static/favicon.svg").status_code == 200


def test_empty_cycle_stepper_says_nothing_has_happened(client, db_session):
    notes = {s.name: s.note for s in cycle_overview(db_session, db_session.query(ReviewCycle).one())["steps"]}
    assert notes["Approve"] == "no nominations yet"  # not "all decided"
    assert notes["Review"] == "no reviewers yet" and notes["Finalize"] == "nothing to release yet"
