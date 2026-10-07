from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.security import SESSION_COOKIE_NAME, read_session_token


def resolve_session_user(db: Session, token: str | None) -> User | None:
    """Cookie -> User, re-checked against the DB on every request: the user
    must still exist and the cookie's session_version must match theirs
    (a password change/reset bumps it, killing older sessions)."""
    if not token:
        return None
    session = read_session_token(token)
    if session is None:
        return None
    user_id, version = session
    user = db.get(User, user_id)
    if user is None or user.session_version != version:
        return None
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return resolve_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))


def require_login(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_role(*roles: str):
    """Dependency factory: 403s any request from a user whose role isn't in `roles`."""

    def dependency(user: User = Depends(require_login)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return dependency


def get_page(page: str | None = None) -> int:
    """`?page=N` parsed leniently -- junk or out-of-range falls back to 1
    rather than a JSON 422 on an HTML page."""
    try:
        value = int(page) if page is not None else 1
    except ValueError:
        return 1
    return max(1, value)
