"""Accounts are created without a password: the new person chooses it from
an invite link, so HR never knows it."""

import re
from datetime import datetime, timedelta, timezone

from app import security
from app.models import AuditLog, PasswordResetToken, TokenPurpose, User
from tests.conftest import login, logout

INVITE_PATH = re.compile(r"/reset-password/[A-Za-z0-9_-]+")


def _user(db, username) -> User:
    return db.query(User).filter_by(username=username).one()


def _create(client, username="ananya.joshi", role="employee"):
    login(client, "hr_admin", "admin123")
    r = client.post("/register", data={"username": username, "role": role})
    logout(client)
    return r


def test_add_user_form_has_no_password_field(client):
    login(client, "hr_admin", "admin123")
    body = client.get("/register").text
    assert 'name="password"' not in body and 'type="password"' not in body.split("<main>")[1]


def test_creating_a_user_gives_hr_an_invite_link_not_a_password(client, db_session):
    r = _create(client)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert "Send Ananya Joshi this invite link" in r.text and "expires in 3 days" in r.text
    assert INVITE_PATH.search(r.text)

    user = _user(db_session, "ananya.joshi")
    assert user.password_hash.startswith(security.UNSET_PREFIX)
    assert not security.has_usable_password(user)
    token = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert token.purpose == TokenPurpose.INVITE.value
    lifetime = token.expires_at - token.created_at
    assert timedelta(days=3) - timedelta(minutes=1) < lifetime <= timedelta(days=3)
    actions = [a.action for a in db_session.query(AuditLog).filter(AuditLog.target == "user:ananya.joshi")]
    assert actions == ["create_user", "issue_invite"]


def test_new_account_cannot_log_in_before_accepting(client, db_session):
    _create(client)
    user = _user(db_session, "ananya.joshi")
    for guess in ["", "password123", user.password_hash, user.password_hash.partition("$")[2]]:
        r = login(client, "ananya.joshi", guess)
        assert r.status_code in (400, 401) and "session=" not in r.headers.get("set-cookie", ""), guess  # refused, no session


def test_pending_account_check_costs_as_much_as_a_real_one(client, db_session, monkeypatch):
    # Otherwise response timing would reveal which accounts are still pending.
    _create(client)
    calls = []
    monkeypatch.setattr(security, "burn_password_check", lambda pw: calls.append(pw))
    assert security.verify_password(db_session, _user(db_session, "ananya.joshi"), "anything") is False
    assert calls == ["anything"]


def test_accepting_the_invite(client, db_session):
    path = INVITE_PATH.search(_create(client).text).group(0)

    page = client.get(path).text
    assert "Set up your account" in page and "Welcome, Ananya Joshi" in page and "only you will know it" in page
    assert "an uppercase letter, a lowercase letter" in page  # the password rules are shown

    r = client.post(path, data={"new_password": "ananya2026", "confirm_password": "ananya2026"})
    assert r.status_code == 400 and "Password needs" in r.text
    r = client.post(path, data={"new_password": "Monsoon-Kite-55!", "confirm_password": "Monsoon-Kite-55!"})
    assert r.status_code == 303 and r.headers["location"] == "/login?notice=welcome"
    assert "Your account is ready" in client.get("/login?notice=welcome").text

    assert login(client, "ananya.joshi", "Monsoon-Kite-55!").status_code == 303
    assert security.has_usable_password(_user(db_session, "ananya.joshi"))
    assert db_session.query(AuditLog).filter_by(action="accept_invite").count() == 1
    logout(client)
    assert client.get(path).status_code == 400  # single use


def test_invite_expires_after_three_days(client, db_session):
    path = INVITE_PATH.search(_create(client).text).group(0)
    token = db_session.query(PasswordResetToken).one()
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    r = client.get(path)
    assert r.status_code == 400 and "invalid, has already been used, or has expired" in r.text


def test_users_page_marks_pending_invites_and_reissues_an_invite(client, db_session):
    first = INVITE_PATH.search(_create(client).text).group(0)
    login(client, "hr_admin", "admin123")
    assert "invite pending" in client.get("/admin/users").text

    user = _user(db_session, "ananya.joshi")
    edit_page = client.get(f"/admin/users/{user.id}").text
    assert "Create new invite link" in edit_page and "hasn't set a password yet" in edit_page

    r = client.post(f"/admin/users/{user.id}/reset-link")
    assert "Invite link created" in r.text and "expires in 3 days" in r.text
    newest = db_session.query(PasswordResetToken).filter_by(user_id=user.id).order_by(PasswordResetToken.id.desc()).first()
    assert newest.purpose == TokenPurpose.INVITE.value
    assert db_session.query(AuditLog).filter_by(action="issue_invite").count() == 2
    logout(client)
    assert client.get(first).status_code == 400  # the old invite stopped working


def test_active_users_still_get_a_one_hour_reset_link(client, db_session):
    login(client, "hr_admin", "admin123")
    user = _user(db_session, "user1")
    assert "Create reset link" in client.get(f"/admin/users/{user.id}").text
    r = client.post(f"/admin/users/{user.id}/reset-link")
    assert "Reset link created" in r.text and "expires in 60 minutes" in r.text
    token = db_session.query(PasswordResetToken).filter_by(user_id=user.id).one()
    assert token.purpose == TokenPurpose.RESET.value
    logout(client)
    page = client.get(INVITE_PATH.search(r.text).group(0)).text
    assert "Reset password" in page and "Set up your account" not in page


def test_invite_events_read_well_in_the_audit_log(client):
    path = INVITE_PATH.search(_create(client).text).group(0)
    client.post(path, data={"new_password": "Monsoon-Kite-55!", "confirm_password": "Monsoon-Kite-55!"})
    login(client, "hr_admin", "admin123")
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", client.get("/admin/audit").text))
    assert "Hr Admin created an invite for Ananya Joshi" in text
    assert "Ananya Joshi accepted their invite and chose a password" in text


def test_dead_link_page_is_neutral(client):
    path = INVITE_PATH.search(_create(client).text).group(0)
    client.post(path, data={"new_password": "Monsoon-Kite-55!", "confirm_password": "Monsoon-Kite-55!"})
    page = client.get(path).text
    assert "This link can" in page and "Reset password" not in page
