import base64
import binascii
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.audit import log_action
from app.config import load_secret_key
from app.models import LoginAttempt, PasswordResetToken, TokenPurpose, User, as_utc, utcnow

# --- Password hashing -------------------------------------------------
# Two algorithms are supported side by side, distinguished by a prefix on
# the stored value:
#   "pbkdf2$<base64 of salt+hash>"   -- legacy, from the original prototype
#   "argon2id$<argon2-cffi encoded>" -- current, used for every new hash
#
# PBKDF2-HMAC-SHA256 (100k iterations, random 32-byte salt) is kept only so
# existing accounts keep working; it is never used to hash a *new*
# password. Argon2id (memory-hard, resistant to GPU/ASIC cracking in a way
# PBKDF2 isn't) is what every new hash uses, via argon2-cffi's defaults,
# which are already tuned to a sensible time/memory tradeoff.
#
# Old accounts are upgraded lazily: verify_password() re-hashes with
# Argon2id the moment a pbkdf2$ account successfully logs in (the plaintext
# password is only ever available right there, at that request), instead
# of needing a bulk offline migration or forcing everyone to reset their
# password.

PBKDF2_ITERATIONS = 100_000
SALT_BYTES = 32
PBKDF2_DIGEST_BYTES = 32

PBKDF2_PREFIX = "pbkdf2$"
ARGON2_PREFIX = "argon2id$"
# A new account has no password until its owner sets one through an invite
# link, so HR never knows it. The stored value matches no algorithm, so
# verify_password() rejects every attempt.
UNSET_PREFIX = "unset$"

_argon2_hasher = PasswordHasher()


def hash_password(password: str, salt: bytes | None = None) -> bytes:
    """Raw PBKDF2-HMAC-SHA256 hash (salt + digest), legacy algorithm only.

    Not used to create new accounts -- kept for the migration script and
    for tests that need to construct a legacy `pbkdf2$` hash.
    """
    if salt is None:
        salt = os.urandom(SALT_BYTES)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt + hashed


def hash_password_argon2(password: str) -> str:
    """The hash format used for every new or re-provisioned account."""
    return ARGON2_PREFIX + _argon2_hasher.hash(password)


def unusable_password() -> str:
    """Placeholder for an account whose owner hasn't chosen a password yet."""
    return UNSET_PREFIX + secrets.token_urlsafe(16)


def has_usable_password(user: User) -> bool:
    return not user.password_hash.startswith(UNSET_PREFIX)


def verify_password(db: Session, user: User, provided_password: str) -> bool:
    algo, separator, payload = user.password_hash.partition("$")
    if not separator:
        return False  # unrecognized stored format -- fail closed

    if algo == "unset":
        # Same cost as a real check, so timing doesn't reveal that this
        # account is still waiting for its owner to accept the invite.
        burn_password_check(provided_password)
        return False

    if algo == "argon2id":
        try:
            _argon2_hasher.verify(payload, provided_password)
        except (VerifyMismatchError, VerificationError, InvalidHash):
            return False
        return True

    if algo == "pbkdf2":
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return False  # corrupt stored hash -- fail closed, don't 500
        if len(raw) != SALT_BYTES + PBKDF2_DIGEST_BYTES:
            return False
        salt, stored_hash = raw[:SALT_BYTES], raw[SALT_BYTES:]
        candidate_hash = hashlib.pbkdf2_hmac(
            "sha256", provided_password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        if not hmac.compare_digest(stored_hash, candidate_hash):
            return False

        # Lazy rehash to Argon2id -- see module docstring above.
        user.password_hash = hash_password_argon2(provided_password)
        log_action(db, user, "password_rehashed", f"user:{user.username}")
        db.commit()
        return True

    return False


@cache
def _dummy_argon2_hash() -> str:
    return _argon2_hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(provided_password: str) -> None:
    """Spend the same time as a real Argon2id verify, for unknown usernames.

    Without this, "no such user" returns instantly while "wrong password"
    takes ~tens of ms of hashing -- a timing difference that lets an
    attacker enumerate which usernames exist.
    """
    try:
        _argon2_hasher.verify(_dummy_argon2_hash(), provided_password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        pass


# --- Session cookie -----------------------------------------------------
# Signed, httponly cookie holding a user id plus that user's
# session_version. Not a JWT: there are no external API clients to hand a
# bearer token to, so a server-verified opaque session is simpler, and
# because the version is checked against the DB on every request, a
# password change/reset kills every older session immediately.

SESSION_COOKIE_NAME = "session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60  # 8 hours

_serializer = URLSafeTimedSerializer(load_secret_key(), salt="hr-review-session")


def create_session_token(user: User) -> str:
    return _serializer.dumps({"uid": user.id, "v": user.session_version})


def read_session_token(token: str) -> tuple[int, int] | None:
    """Return (user_id, session_version), or None if the cookie is forged,
    expired, or from an older payload format."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user_id, version = data.get("uid"), data.get("v")
    if not isinstance(user_id, int) or not isinstance(version, int):
        return None
    return user_id, version


# --- Login rate limiting (database-backed) ------------------------------
# Failed attempts are rows in `login_attempts`, so the lockout is shared by
# every worker process and survives restarts. Two independent limits:
#   per username -- stops guessing one account's password
#   per client IP -- stops one client spraying many usernames
# A successful login clears only that username's counter, never the IP's,
# so an attacker can't reset their IP budget by logging into an account
# they control.

MAX_FAILED_ATTEMPTS = 5
MAX_FAILED_ATTEMPTS_PER_IP = 20
LOCKOUT_SECONDS = 5 * 60
_MAX_KEY_LENGTH = 128


def _user_key(username: str) -> str:
    return f"user:{username}"[:_MAX_KEY_LENGTH]


def _ip_key(ip: str) -> str:
    return f"ip:{ip}"[:_MAX_KEY_LENGTH]


def _window_start():
    return utcnow() - timedelta(seconds=LOCKOUT_SECONDS)


def _seconds_blocked(db: Session, key: str, limit: int) -> int:
    recent = (
        db.query(LoginAttempt.attempted_at)
        .filter(LoginAttempt.key == key, LoginAttempt.attempted_at > _window_start())
        .order_by(LoginAttempt.attempted_at.desc())
        .limit(limit)
        .all()
    )
    if len(recent) < limit:
        return 0
    # The lockout lifts once the limit-th most recent failure ages out.
    oldest_relevant = as_utc(recent[-1][0])
    remaining = LOCKOUT_SECONDS - (utcnow() - oldest_relevant).total_seconds()
    return max(1, int(remaining))


def login_lockout_seconds(db: Session, username: str, ip: str) -> int:
    """0 if a login attempt is allowed, else seconds until it will be."""
    return max(
        _seconds_blocked(db, _user_key(username), MAX_FAILED_ATTEMPTS),
        _seconds_blocked(db, _ip_key(ip), MAX_FAILED_ATTEMPTS_PER_IP),
    )


def record_failed_login(db: Session, username: str, ip: str) -> None:
    """Stage failed-attempt rows; the caller commits. Also prunes rows that
    have aged out of the window, so the table stays bounded no matter how
    many distinct usernames are tried."""
    db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at <= _window_start()))
    db.add_all([LoginAttempt(key=_user_key(username)), LoginAttempt(key=_ip_key(ip))])


def clear_failed_logins(db: Session, username: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.key == _user_key(username)))



# --- Set-password links: invites and resets -------------------------------
# HR issues a link; the person it's for chooses the password, so HR never
# knows it. The raw token appears exactly once (in the link) and only its
# SHA-256 digest is stored. Links are single-use, and issuing a new one
# voids any older unused ones for that person.
#   invite -- a new account's first password; lasts INVITE_TOKEN_TTL
#   reset  -- an existing account's new password; lasts RESET_TOKEN_TTL

RESET_TOKEN_TTL = timedelta(hours=1)
INVITE_TOKEN_TTL = timedelta(days=3)
TOKEN_TTLS = {TokenPurpose.INVITE.value: INVITE_TOKEN_TTL, TokenPurpose.RESET.value: RESET_TOKEN_TTL}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_reset_token(
    db: Session, user: User, issued_by: User | None, purpose: str = TokenPurpose.RESET.value
) -> str:
    """Stage a new invite or reset token for `user` and return the raw
    token; the caller commits."""
    now = utcnow()
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)

    raw = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_digest(raw),
            issued_by_id=issued_by.id if issued_by else None,
            created_at=now,
            expires_at=now + TOKEN_TTLS[purpose],
            purpose=purpose,
        )
    )
    return raw


def find_valid_reset_token(db: Session, raw_token: str) -> PasswordResetToken | None:
    record = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == _digest(raw_token)).first()
    if record is None or record.used_at is not None or as_utc(record.expires_at) <= utcnow():
        return None
    return record


def set_password(db: Session, user: User, new_password: str) -> None:
    """Replace a user's password and invalidate all of their existing
    sessions; the caller commits."""
    user.password_hash = hash_password_argon2(new_password)
    user.session_version += 1
    clear_failed_logins(db, user.username)
