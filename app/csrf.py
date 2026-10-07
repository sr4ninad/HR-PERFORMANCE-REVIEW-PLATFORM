"""CSRF protection: double-submit token, checked on every unsafe request.

Every visitor gets a random `csrf_token` cookie (httponly -- the page never
needs to read it with JS, because the server renders the same value into a
hidden field in every form). On POST/PUT/PATCH/DELETE the submitted
`csrf_token` form field must match the cookie. A cross-site page can make
the browser *send* the cookie, but it can't *read* it, so it can't produce
the matching field.

The check is an app-wide FastAPI dependency (see main.py), not a per-route
one, so a new POST route can't forget it. SameSite=Lax on the session
cookie is kept as a second layer, not the only one.
"""

import hmac
import re
import secrets

from fastapi import HTTPException, Request, status

from app.config import COOKIE_SECURE

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME, token, httponly=True, samesite="lax", secure=COOKIE_SECURE
    )


async def csrf_cookie_middleware(request: Request, call_next):
    """Make sure every visitor has a token, and expose it to templates via
    request.state before the route renders."""
    token = request.cookies.get(CSRF_COOKIE_NAME)
    is_new = token is None or not _TOKEN_SHAPE.match(token)
    if is_new:
        token = new_csrf_token()
    request.state.csrf_token = token

    response = await call_next(request)
    # A route (login) may have rotated the token itself; don't clobber it.
    if is_new and not getattr(request.state, "csrf_rotated", False):
        set_csrf_cookie(response, token)
    return response


def rotate_csrf_token(request: Request, response) -> None:
    """Issue a fresh token on a privilege change (login), so a token planted
    before authentication can't be reused after it."""
    token = new_csrf_token()
    request.state.csrf_token = token
    request.state.csrf_rotated = True
    set_csrf_cookie(response, token)


async def verify_csrf(request: Request) -> None:
    if request.method not in _UNSAFE_METHODS:
        return
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    form = await request.form()  # Starlette caches this; the route reads the same parsed form
    submitted = form.get(CSRF_FORM_FIELD)
    if (
        not cookie_token
        or not isinstance(submitted, str)
        or not hmac.compare_digest(cookie_token.encode(), submitted.encode())
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your form session expired or the request didn't come from this site. Reload the page and try again.",
        )
