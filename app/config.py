"""Environment-driven settings, read once at import.

Every knob the app reads from the environment lives here so there is one
place to look when deploying:

    HR_REVIEW_ENV            "development" (default) or "production"
    HR_REVIEW_DATABASE_URL   SQLAlchemy URL; defaults to ./hr_review.db
    HR_REVIEW_SECRET_KEY     session-signing key; required in production
    HR_REVIEW_SECRET_KEY_FILE  where the dev key is persisted (default ./.secret_key)
    HR_REVIEW_COOKIE_SECURE  "1"/"0"; defaults to on in production
    HR_REVIEW_SEED_DEMO      "1"/"0"; demo accounts are seeded by default only outside production
"""

import os
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENV = os.environ.get("HR_REVIEW_ENV", "development").strip().lower()
IS_PRODUCTION = ENV == "production"

DATABASE_URL = os.environ.get("HR_REVIEW_DATABASE_URL", f"sqlite:///{BASE_DIR / 'hr_review.db'}")

# Secure cookies are only sent over HTTPS, so they're on by default in
# production and off for plain-http local development.
COOKIE_SECURE = _env_flag("HR_REVIEW_COOKIE_SECURE", IS_PRODUCTION)

SEED_DEMO_DATA = _env_flag("HR_REVIEW_SEED_DEMO", not IS_PRODUCTION)

MIN_SECRET_KEY_LENGTH = 32
SECRET_KEY_FILE = Path(os.environ.get("HR_REVIEW_SECRET_KEY_FILE", BASE_DIR / ".secret_key"))


def load_secret_key() -> str:
    """Return the session-signing key.

    Production: HR_REVIEW_SECRET_KEY must be set (and long enough), or the
    app refuses to start -- there is no hardcoded fallback to leak.
    Development: a random key is generated on first run and persisted to
    SECRET_KEY_FILE (gitignored), so `uvicorn app.main:app` still works with
    zero setup, sessions survive restarts, and every worker process on the
    machine shares the same key.
    """
    key = os.environ.get("HR_REVIEW_SECRET_KEY", "").strip()
    if key:
        if IS_PRODUCTION and len(key) < MIN_SECRET_KEY_LENGTH:
            raise RuntimeError(
                f"HR_REVIEW_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters in production."
            )
        return key

    if IS_PRODUCTION:
        raise RuntimeError("HR_REVIEW_SECRET_KEY must be set when HR_REVIEW_ENV=production.")

    try:
        # O_EXCL: if two workers race on first start, exactly one creates the
        # file and the other falls through to reading it.
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        raise RuntimeError(f"{SECRET_KEY_FILE} exists but is empty; delete it to regenerate.")

    generated = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(generated)
    return generated
