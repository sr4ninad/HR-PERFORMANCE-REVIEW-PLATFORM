from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.audit import log_action
from app.config import COOKIE_SECURE
from app.csrf import rotate_csrf_token
from app.database import get_db
from app.flash import flash
from app.deps import get_current_user, require_role
from app.models import Role, TokenPurpose, User
from app.org import manager_problem
from app.security import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    burn_password_check,
    clear_failed_logins,
    INVITE_TOKEN_TTL,
    create_session_token,
    issue_reset_token,
    login_lockout_seconds,
    record_failed_login,
    unusable_password,
    verify_password,
)
from app.services import username_problem
from app.template_utils import templates

router = APIRouter()

LOGIN_NOTICES = {
    "reset": "Password updated. Log in with your new password.",
    "welcome": "Your account is ready. Log in with the password you just chose.",
}


def set_session_cookie(response, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(user),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, notice: str | None = None, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"message": LOGIN_NOTICES.get(notice or "")})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    ip = _client_ip(request)

    wait = login_lockout_seconds(db, username, ip)
    if wait:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": f"Too many failed attempts. Try again in {wait} seconds."},
            status_code=429,
        )

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        burn_password_check(password)  # same cost as a real check -- no username enumeration by timing
        authenticated = False
    else:
        authenticated = verify_password(db, user, password)

    if not authenticated:
        record_failed_login(db, username, ip)
        log_action(db, None, "login_failure", f"user:{username[:64]}")
        db.commit()
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password."},
            status_code=401,
        )

    clear_failed_logins(db, username)
    log_action(db, user, "login_success", f"user:{user.username}")
    db.commit()

    response = RedirectResponse("/", status_code=303)
    flash(response, f"Welcome back, {user.display_name.split()[0]}.", "info")
    set_session_cookie(response, user)
    rotate_csrf_token(request, response)
    return response


@router.post("/logout")
def logout(request: Request, user: User | None = Depends(get_current_user), db: Session = Depends(get_db)):
    # POST (with the app-wide CSRF check) rather than GET, so a third-party
    # page can't log people out with an <img src="/logout">.
    if user is not None:
        log_action(db, user, "logout", f"user:{user.username}")
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    if user is not None:
        flash(response, "You've been signed out.", "info")
    return response


def _register_context(db: Session, **extra) -> dict:
    managers = db.query(User).filter(User.role == Role.MANAGER.value).order_by(User.username).all()
    return {"managers": managers, "roles": [r.value for r in Role], **extra}


@router.get("/register")
def register_form(
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    return templates.TemplateResponse(request, "register.html", _register_context(db))


@router.post("/register")
def register_submit(
    request: Request,
    username: str = Form(...),
    role: str = Form(...),
    manager_id: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    """Create an account *without* a password. HR gets a one-time invite
    link to send to the new person, who chooses their own password -- so
    HR never knows it. Until then the account can't be logged into."""
    username = username.strip()
    parsed_manager_id, error = None, None
    if role not in [r.value for r in Role]:
        error = "Invalid role."
    else:
        error = username_problem(username)
    if error is None and db.query(User).filter(User.username == username).first() is not None:
        error = f"Username '{username}' already exists."
    if error is None:
        parsed_manager_id, error = manager_problem(db, None, manager_id)

    if error:
        return templates.TemplateResponse(
            request, "register.html", _register_context(db, error=error), status_code=400
        )

    new_user = User(
        username=username,
        password_hash=unusable_password(),
        role=role,
        manager_id=parsed_manager_id,
    )
    db.add(new_user)
    db.flush()
    log_action(db, admin, "create_user", f"user:{new_user.username}", {"role": role, "manager_id": parsed_manager_id})
    raw_token = issue_reset_token(db, new_user, issued_by=admin, purpose=TokenPurpose.INVITE.value)
    # The token itself is never written to the audit log -- only that one was issued.
    log_action(db, admin, "issue_invite", f"user:{new_user.username}")
    db.commit()

    response = templates.TemplateResponse(
        request,
        "register.html",
        _register_context(
            db,
            created=new_user,
            invite_url=str(request.url_for("reset_password_form", token=raw_token)),
            invite_days=INVITE_TOKEN_TTL.days,
        ),
    )
    response.headers["Cache-Control"] = "no-store"  # the link is a credential; keep it out of caches
    return response
