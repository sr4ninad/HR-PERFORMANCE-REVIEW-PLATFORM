import base64
from datetime import timedelta

from app import security
from app.models import AuditLog, LoginAttempt, User, Role


def _make_pbkdf2_user(db_session, username: str, password: str) -> User:
    """Build a User row with a legacy pbkdf2$ hash, bypassing hash_password_argon2
    entirely, to exercise the legacy-authentication and lazy-rehash paths."""
    raw = security.hash_password(password)
    stored = security.PBKDF2_PREFIX + base64.b64encode(raw).decode("ascii")
    user = User(username=username, password_hash=stored, role=Role.EMPLOYEE.value)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_argon2_hash_and_verify_round_trip(db_session):
    user = User(
        username="argon2-user",
        password_hash=security.hash_password_argon2("correct-horse"),
        role=Role.EMPLOYEE.value,
    )
    db_session.add(user)
    db_session.commit()

    assert user.password_hash.startswith(security.ARGON2_PREFIX)
    assert security.verify_password(db_session, user, "correct-horse") is True


def test_argon2_verify_rejects_wrong_password(db_session):
    user = User(
        username="argon2-wrong",
        password_hash=security.hash_password_argon2("correct-horse"),
        role=Role.EMPLOYEE.value,
    )
    db_session.add(user)
    db_session.commit()

    assert security.verify_password(db_session, user, "wrong-password") is False


def test_same_password_hashes_differently_each_time():
    # random salt per call -> ciphertext differs even for identical input
    a = security.hash_password_argon2("same-password")
    b = security.hash_password_argon2("same-password")
    assert a != b


def test_pbkdf2_legacy_hash_still_authenticates(db_session):
    user = _make_pbkdf2_user(db_session, "legacy-user", "legacy-pw")
    assert user.password_hash.startswith(security.PBKDF2_PREFIX)

    assert security.verify_password(db_session, user, "legacy-pw") is True


def test_pbkdf2_legacy_hash_rejects_wrong_password(db_session):
    user = _make_pbkdf2_user(db_session, "legacy-user-2", "legacy-pw")
    assert security.verify_password(db_session, user, "wrong-password") is False
    # a failed attempt must not rehash or mutate the stored hash
    assert user.password_hash.startswith(security.PBKDF2_PREFIX)


def test_successful_pbkdf2_login_rehashes_to_argon2id(db_session):
    user = _make_pbkdf2_user(db_session, "legacy-user-3", "legacy-pw")

    assert security.verify_password(db_session, user, "legacy-pw") is True

    assert user.password_hash.startswith(security.ARGON2_PREFIX)
    # the new hash must still authenticate the same password
    assert security.verify_password(db_session, user, "legacy-pw") is True

    rehash_events = (
        db_session.query(AuditLog)
        .filter(AuditLog.actor_id == user.id, AuditLog.action == "password_rehashed")
        .all()
    )
    assert len(rehash_events) == 1
    assert rehash_events[0].target == f"user:{user.username}"


def test_fixture_org_users_are_hashed_with_argon2id(db_session):
    # The demo seed's hashing is checked separately in test_demo_seed.py.
    for username in ["hr_admin", "director1", "manager1", "user1", "user2", "user3", "user4"]:
        user = db_session.query(User).filter_by(username=username).first()
        assert user.password_hash.startswith(security.ARGON2_PREFIX), username


def test_corrupt_pbkdf2_hash_fails_closed_instead_of_crashing(db_session):
    user = User(username="corrupt-hash", password_hash="pbkdf2$not base64!!", role=Role.EMPLOYEE.value)
    db_session.add(user)
    db_session.commit()
    assert security.verify_password(db_session, user, "anything") is False


def test_session_token_round_trip(db_session):
    user = db_session.query(User).filter_by(username="user1").one()
    token = security.create_session_token(user)
    assert security.read_session_token(token) == (user.id, user.session_version)


def test_session_token_rejects_garbage():
    assert security.read_session_token("not-a-real-token") is None


def test_rate_limiter_locks_out_after_max_attempts(db_session):
    assert security.login_lockout_seconds(db_session, "user1", "10.0.0.1") == 0

    for _ in range(security.MAX_FAILED_ATTEMPTS):
        security.record_failed_login(db_session, "user1", "10.0.0.1")
    db_session.commit()

    assert security.login_lockout_seconds(db_session, "user1", "10.0.0.1") > 0
    # the per-username lock applies from any IP
    assert security.login_lockout_seconds(db_session, "user1", "10.9.9.9") > 0


def test_rate_limiter_clears_username_on_success_but_not_ip(db_session):
    for i in range(security.MAX_FAILED_ATTEMPTS_PER_IP):
        security.record_failed_login(db_session, f"sprayed-{i}", "10.0.0.2")
    db_session.commit()
    assert security.login_lockout_seconds(db_session, "user2", "10.0.0.2") > 0  # IP is blocked

    security.clear_failed_logins(db_session, "sprayed-0")
    db_session.commit()
    # clearing one username must not reset the IP budget
    assert security.login_lockout_seconds(db_session, "user2", "10.0.0.2") > 0
    assert security.login_lockout_seconds(db_session, "user2", "10.0.0.3") == 0


def test_rate_limiter_is_shared_state_not_process_memory(db_session):
    # The lockout lives in the DB: a fresh session (standing in for another
    # worker process, or the same one after a restart) sees it.
    for _ in range(security.MAX_FAILED_ATTEMPTS):
        security.record_failed_login(db_session, "user3", "10.0.0.4")
    db_session.commit()

    other_session = type(db_session)(bind=db_session.get_bind())
    try:
        assert security.login_lockout_seconds(other_session, "user3", "10.0.0.5") > 0
    finally:
        other_session.close()


def test_rate_limiter_prunes_expired_attempts(db_session):
    stale = security.utcnow() - timedelta(seconds=security.LOCKOUT_SECONDS + 60)
    db_session.add_all([LoginAttempt(key=f"user:stale-{i}", attempted_at=stale) for i in range(50)])
    db_session.commit()

    security.record_failed_login(db_session, "fresh", "10.0.0.6")
    db_session.commit()

    assert db_session.query(LoginAttempt).filter(LoginAttempt.key.like("user:stale-%")).count() == 0
