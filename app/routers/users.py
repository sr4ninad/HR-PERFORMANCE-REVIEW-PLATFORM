from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.deps import get_page, require_role
from app.models import Role, TokenPurpose, User
from app.org import manager_problem
from app.pagination import paginate
from app.security import TOKEN_TTLS, has_usable_password, issue_reset_token
from app.template_utils import templates

router = APIRouter(prefix="/admin/users")


def _get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def _ttl_text(ttl) -> str:
    hours = int(ttl.total_seconds() // 3600)
    return f"{hours // 24} days" if hours >= 48 else f"{int(ttl.total_seconds() // 60)} minutes"


def _edit_context(db: Session, target: User, **extra) -> dict:
    managers = (
        db.query(User)
        .filter(User.role == Role.MANAGER.value, User.id != target.id)
        .order_by(User.username)
        .all()
    )
    return {
        "target": target,
        "managers": managers,
        "roles": [r.value for r in Role],
        "invite_pending": not has_usable_password(target),
        **extra,
    }


@router.get("")
def list_users(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    users_page = paginate(db.query(User).order_by(User.username), page)
    pending = {u.id for u in users_page.items if not has_usable_password(u)}
    return templates.TemplateResponse(request, "admin_users.html", {"users_page": users_page, "pending_ids": pending})


@router.get("/{user_id}")
def edit_user_form(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    return templates.TemplateResponse(request, "admin_user_edit.html", _edit_context(db, _get_user_or_404(db, user_id)))


@router.post("/{user_id}")
def edit_user_submit(
    request: Request,
    user_id: int,
    role: str = Form(...),
    manager_id: str = Form(""),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    target = _get_user_or_404(db, user_id)

    error = None
    parsed_manager_id = None
    if role not in [r.value for r in Role]:
        error = "Invalid role."
    elif target.id == admin.id and role != target.role:
        error = "You can't change your own role (ask another HR admin)."
    elif target.role == Role.MANAGER.value and role != Role.MANAGER.value and target.direct_reports:
        error = f"{target.username} still has direct reports; reassign them before changing this role."
    else:
        parsed_manager_id, error = manager_problem(db, target, manager_id)

    if error:
        return templates.TemplateResponse(
            request, "admin_user_edit.html", _edit_context(db, target, error=error), status_code=400
        )

    changes = {}
    if role != target.role:
        changes["role"] = [target.role, role]
        target.role = role
    if parsed_manager_id != target.manager_id:
        changes["manager_id"] = [target.manager_id, parsed_manager_id]
        target.manager_id = parsed_manager_id
    if changes:
        log_action(db, admin, "update_user", f"user:{target.username}", changes)
        db.commit()

    return templates.TemplateResponse(
        request,
        "admin_user_edit.html",
        _edit_context(db, target, message="Saved." if changes else "No changes."),
    )


@router.post("/{user_id}/reset-link")
def issue_reset_link(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    target = _get_user_or_404(db, user_id)
    # Someone who never accepted their invite gets a fresh invite (3 days),
    # not a 1-hour reset. Either way they choose the password, not HR.
    purpose = TokenPurpose.RESET.value if has_usable_password(target) else TokenPurpose.INVITE.value
    raw_token = issue_reset_token(db, target, issued_by=admin, purpose=purpose)
    # The token itself is never written to the audit log -- only that one was issued.
    log_action(db, admin, "issue_invite" if purpose == TokenPurpose.INVITE.value else "issue_password_reset",
               f"user:{target.username}")
    db.commit()

    reset_url = str(request.url_for("reset_password_form", token=raw_token))
    response = templates.TemplateResponse(
        request,
        "admin_user_edit.html",
        _edit_context(
            db,
            target,
            reset_url=reset_url,
            reset_ttl=_ttl_text(TOKEN_TTLS[purpose]),
        ),
    )
    response.headers["Cache-Control"] = "no-store"  # the link is a credential; keep it out of caches
    return response

