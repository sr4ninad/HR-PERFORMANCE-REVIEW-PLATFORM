"""One-time notification messages ("toasts") that survive a redirect.

A successful POST redirects (POST/redirect/GET), so its confirmation has to
travel to the next page. It rides in a short-lived cookie, signed so it
can't be forged into a fake message; the page that displays it clears it.
"""

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.requests import Request

from app.config import COOKIE_SECURE, load_secret_key

FLASH_COOKIE = "flash"
KINDS = ("success", "info", "warning")

_signer = URLSafeSerializer(load_secret_key(), salt="hr-review-flash")


def flash(response, message: str, kind: str = "success"):
    """Attach a toast to a (redirect) response; shown on the next page."""
    if kind not in KINDS:
        kind = "info"
    response.set_cookie(
        FLASH_COOKIE,
        _signer.dumps([kind, message]),
        max_age=60,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )
    return response


def read_flashes(request: Request) -> list[tuple[str, str]]:
    """The pending toast, if any. Marks it consumed so the response clears it."""
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return []
    try:
        kind, message = _signer.loads(raw)
    except (BadSignature, ValueError, TypeError):
        return []
    request.state.flash_consumed = True
    return [(kind if kind in KINDS else "info", str(message))]


async def flash_middleware(request: Request, call_next):
    """Clear a flash cookie once a page has displayed it -- unless this
    response sets a new one (e.g. a redirect carrying its own toast)."""
    response = await call_next(request)
    if getattr(request.state, "flash_consumed", False):
        sets_new = any(
            name == b"set-cookie" and value.startswith(FLASH_COOKIE.encode() + b"=")
            for name, value in response.raw_headers
        )
        if not sets_new:
            response.delete_cookie(FLASH_COOKIE)
    return response
