from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.flash import flash
from app.deps import get_page, require_role
from app.models import CycleStatus, ReviewCycle, Role, User
from app.pagination import paginate
from app.template_utils import templates

router = APIRouter(prefix="/admin/cycles")

MAX_CYCLE_NAME_LENGTH = 128


def _cycles_page(db: Session, page: int):
    return paginate(db.query(ReviewCycle).order_by(ReviewCycle.created_at.desc(), ReviewCycle.id.desc()), page)


def _get_cycle_or_404(db: Session, cycle_id: int) -> ReviewCycle:
    cycle = db.get(ReviewCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cycle not found")
    return cycle


def _parse_form_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@router.get("")
def list_cycles(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    return templates.TemplateResponse(request, "admin_cycles.html", {"cycles_page": _cycles_page(db, page)})


@router.post("")
def create_cycle(
    request: Request,
    name: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    name = name.strip()
    start = _parse_form_datetime(start_date)
    end = _parse_form_datetime(end_date)

    error = None
    if not name or len(name) > MAX_CYCLE_NAME_LENGTH:
        error = f"Cycle name is required (max {MAX_CYCLE_NAME_LENGTH} characters)."
    elif start is None or end is None:
        error = "Start and end must be valid dates."
    elif end <= start:
        error = "End date must be after the start date."

    if error:
        return templates.TemplateResponse(
            request,
            "admin_cycles.html",
            {"cycles_page": _cycles_page(db, 1), "error": error},
            status_code=400,
        )

    cycle = ReviewCycle(name=name, start_date=start, end_date=end, status=CycleStatus.DRAFT.value)
    db.add(cycle)
    db.flush()
    log_action(db, admin, "create_cycle", f"cycle:{cycle.id}", {"name": name})
    db.commit()
    return flash(RedirectResponse("/admin/cycles", status_code=303), f"Created {name} as a draft. Activate it to start the cycle.")


@router.post("/{cycle_id}/activate")
def activate_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    # Look the target up *before* touching anything else: activating a
    # nonexistent cycle must 404, not silently close the current one.
    cycle = _get_cycle_or_404(db, cycle_id)
    if cycle.status == CycleStatus.ACTIVE.value:
        return RedirectResponse("/admin/cycles", status_code=303)

    # Only one cycle is active at a time (also enforced by the
    # uq_one_active_cycle partial index). Close the current one and flush
    # that UPDATE first, so the index never sees two active rows at once.
    superseded = []
    for other in db.query(ReviewCycle).filter(ReviewCycle.status == CycleStatus.ACTIVE.value):
        superseded.append(other.name)
        other.status = CycleStatus.CLOSED.value
        log_action(db, admin, "close_cycle", f"cycle:{other.id}", {"reason": "superseded"})
    db.flush()

    cycle.status = CycleStatus.ACTIVE.value
    log_action(db, admin, "activate_cycle", f"cycle:{cycle.id}")
    db.commit()
    message = f"{cycle.name} is now active."
    if superseded:
        message += f" {', '.join(superseded)} closed automatically."
    return flash(RedirectResponse("/admin/cycles", status_code=303), message)


@router.post("/{cycle_id}/close")
def close_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    cycle = _get_cycle_or_404(db, cycle_id)
    if cycle.status != CycleStatus.CLOSED.value:
        cycle.status = CycleStatus.CLOSED.value
        log_action(db, admin, "close_cycle", f"cycle:{cycle.id}")
        db.commit()
        return flash(
            RedirectResponse("/admin/cycles", status_code=303),
            f"{cycle.name} closed. No more nominations or peer reviews can be submitted.",
        )
    return RedirectResponse("/admin/cycles", status_code=303)


def get_active_cycle(db: Session) -> ReviewCycle | None:
    return db.query(ReviewCycle).filter(ReviewCycle.status == CycleStatus.ACTIVE.value).first()
