import re

from app.language_filter import BANNED_WORDS, find_banned_language
from app.models import SKILL_AREAS, Review


def compute_overall_rating(review: Review) -> float | None:
    ratings = review.ratings()
    if not ratings:
        return None
    return round(sum(ratings) / len(ratings), 2)


def validate_rating(value: int) -> bool:
    return isinstance(value, int) and 1 <= value <= 5


MIN_PASSWORD_LENGTH = 8
# Argon2 handles long inputs fine, but an unbounded field lets a client make
# the server hash megabytes per request.
MAX_PASSWORD_LENGTH = 256
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")

# Shown next to every "new password" field, so the rules are visible up front.
PASSWORD_RULES_TEXT = (
    f"At least {MIN_PASSWORD_LENGTH} characters, with an uppercase letter, a lowercase letter, "
    "a number and a special character (e.g. ! @ # $ %). Can't contain your username."
)


def password_problem(password: str, username: str | None = None) -> str | None:
    """None if acceptable, else one user-facing reason listing everything
    that's missing, so the user can fix it in one go.

    Applies whenever a password is *set* (registration, change, reset, the
    bootstrap script). Existing passwords keep working until next changed.
    """
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."

    missing = []
    if len(password) < MIN_PASSWORD_LENGTH:
        missing.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if not any(c.isupper() for c in password):
        missing.append("an uppercase letter")
    if not any(c.islower() for c in password):
        missing.append("a lowercase letter")
    if not any(c.isdigit() for c in password):
        missing.append("a number")
    if not any(not c.isalnum() and not c.isspace() for c in password):
        missing.append("a special character (e.g. ! @ # $ %)")
    if missing:
        return "Password needs " + _join(missing) + "."

    if username and len(username) >= 3 and username.lower() in password.lower():
        return "Password can't contain your username."
    return None


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def username_problem(username: str) -> str | None:
    if not _USERNAME_PATTERN.match(username):
        return "Username must be 3-64 characters: letters, digits, '.', '_' or '-'."
    return None


__all__ = [
    "BANNED_WORDS",
    "SKILL_AREAS",
    "find_banned_language",
    "compute_overall_rating",
    "validate_rating",
    "password_problem",
    "username_problem",
    "PASSWORD_RULES_TEXT",
]
