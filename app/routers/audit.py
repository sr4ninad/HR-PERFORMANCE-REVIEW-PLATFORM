from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session, joinedload

from app.audit_view import CATEGORIES, build_timeline
from app.database import get_db
from app.deps import get_page, require_role
from app.models import AuditLog, Role, User
from app.pagination import paginate
from app.template_utils import templates

router = APIRouter(prefix="/admin/audit")

PER_PAGE = 30


@router.get("")
def audit_timeline(
    request: Request,
    category: str | None = None,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    """HR's read-only view of the audit log, newest first, as a timeline."""
    query = db.query(AuditLog).options(joinedload(AuditLog.actor))
    if category in CATEGORIES:
        query = query.filter(AuditLog.action.in_(CATEGORIES[category][1]))
    else:
        category = None
    rows_page = paginate(query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()), page, per_page=PER_PAGE)
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "groups": build_timeline(db, list(rows_page.items)),
            "rows_page": rows_page,
            "categories": [(key, label) for key, (label, _) in CATEGORIES.items()],
            "category": category,
            "pager_query": f"category={category}" if category else "",
        },
    )
