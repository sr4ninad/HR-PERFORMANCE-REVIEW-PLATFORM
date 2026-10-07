from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import TEMPLATES_DIR
from app.database import get_db
from app.deps import resolve_session_user
from app.flash import read_flashes
from app.org import position_title
from app.security import SESSION_COOKIE_NAME
from app.services import PASSWORD_RULES_TEXT


def _inject_request_globals(request: Request) -> dict:
    """Context processor so every template can render nav links and CSRF
    fields without every route handler having to pass them explicitly.

    Goes through `request.app.dependency_overrides` (falling back to the
    real `get_db`) rather than opening its own session directly, so it
    reads through the same database the request's route handler is using
    -- notably the per-test DB the test suite swaps in via
    `dependency_overrides`.
    """
    nav_user = None
    nav_position = None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        db_dependency = request.app.dependency_overrides.get(get_db, get_db)
        db_gen = db_dependency()
        db = next(db_gen)
        try:
            nav_user = resolve_session_user(db, token)
            # Computed while the session is open: it reads direct_reports.
            nav_position = position_title(nav_user) if nav_user else None
        finally:
            db_gen.close()
    return {
        "nav_user": nav_user,
        "nav_position": nav_position,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "flashes": read_flashes(request),
    }


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_inject_request_globals])
templates.env.globals["password_rules"] = PASSWORD_RULES_TEXT
