from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.deps import require_login
from app.models import TokenPurpose, User, utcnow
from app.routers.auth import set_session_cookie
from app.security import find_valid_reset_token, set_password, verify_password
from app.services import password_problem
from app.template_utils import templates

router = APIRouter()


@router.get("/account/password")
def change_password_form(request: Request, user: User = Depends(require_login)):
    return templates.TemplateResponse(request, "change_password.html", {})


@router.post("/account/password")
def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    error = None
    if not verify_password(db, user, current_password):
        error = "Current password is incorrect."
    elif new_password != confirm_password:
        error = "New passwords don't match."
    elif new_password == current_password:
        error = "New password must be different from the current one."
    else:
        error = password_problem(new_password, user.username)

    if error:
        return templates.TemplateResponse(request, "change_password.html", {"error": error}, status_code=400)

    set_password(db, user, new_password)  # bumps session_version: every other device is logged out
    log_action(db, user, "change_password", f"user:{user.username}")
    db.commit()

    # Re-issue this browser's cookie at the new session_version so the
    # person who just changed their password stays logged in here.
    response = templates.TemplateResponse(
        request, "change_password.html", {"message": "Password changed. Other sessions have been signed out."}
    )
    set_session_cookie(response, user)
    return response


@router.get("/reset-password/{token}")
def reset_password_form(request: Request, token: str, db: Session = Depends(get_db)):
    record = find_valid_reset_token(db, token)
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {
            "token": token,
            "valid": record is not None,
            "username": record.user.display_name if record else None,
            "invite": record is not None and record.purpose == TokenPurpose.INVITE.value,
        },
        status_code=200 if record else 400,
    )


@router.post("/reset-password/{token}")
def reset_password_submit(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    record = find_valid_reset_token(db, token)
    if record is None:
        return templates.TemplateResponse(
            request, "reset_password.html", {"token": token, "valid": False}, status_code=400
        )

    error = (
        "Passwords don't match."
        if new_password != confirm_password
        else password_problem(new_password, record.user.username)
    )
    if error:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {
                "token": token,
                "valid": True,
                "username": record.user.display_name,
                "invite": record.purpose == TokenPurpose.INVITE.value,
                "error": error,
            },
            status_code=400,
        )

    user = record.user
    invite = record.purpose == TokenPurpose.INVITE.value
    set_password(db, user, new_password)  # also clears that username's login lockout
    record.used_at = utcnow()
    log_action(db, user, "accept_invite" if invite else "password_reset", f"user:{user.username}", {"token_id": record.id})
    db.commit()
    return RedirectResponse("/login?notice=welcome" if invite else "/login?notice=reset", status_code=303)
