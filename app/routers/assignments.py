from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.flash import flash
from app.deps import require_role
from app.feedback import MAX_NOMINATIONS, MIN_NOMINATIONS
from app.models import AssignmentSource, AssignmentStatus, ReviewerAssignment, Role, User, utcnow
from app.routers.cycles import get_active_cycle
from app.template_utils import templates

router = APIRouter()


def eligible_reviewers(db: Session, reviewee: User):
    """Who can review `reviewee`: anyone but themself and HR admins. Used to
    build every reviewer dropdown *and* to validate every POST -- the
    dropdown alone is not a security boundary, since anyone can hand-craft
    the form."""
    return (
        db.query(User)
        .filter(User.id != reviewee.id, User.role != Role.HR_ADMIN.value)
        .order_by(User.username)
    )


def _reviewable_people(db: Session):
    return db.query(User).filter(User.role != Role.HR_ADMIN.value).order_by(User.username).all()


def _my_nominations(db: Session, cycle_id: int, user: User) -> list[ReviewerAssignment]:
    # Only the reviewee's own nominations count here: reviewers added by HR
    # or the manager don't use up (or block) the employee's own choice.
    return (
        db.query(ReviewerAssignment)
        .filter(
            ReviewerAssignment.cycle_id == cycle_id,
            ReviewerAssignment.reviewee_id == user.id,
            ReviewerAssignment.source == AssignmentSource.NOMINATED.value,
        )
        .all()
    )


def _select_context(db: Session, cycle, user: User, **extra) -> dict:
    return {
        "cycle": cycle,
        "candidates": eligible_reviewers(db, user).all(),
        "min_nominations": MIN_NOMINATIONS,
        "max_nominations": MAX_NOMINATIONS,
        "approver": user.manager.display_name if user.manager else "HR",
        **extra,
    }


@router.get("/assignments/select")
def select_reviewers_form(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    cycle = get_active_cycle(db)
    if cycle is None:
        return templates.TemplateResponse(
            request, "select_reviewers.html", {"error": "There is no active review cycle right now."}
        )

    nominations = _my_nominations(db, cycle.id, user)
    if nominations:
        # Show who *you* nominated (you already know that) and whether the
        # manager has finished deciding -- but never which nominees were
        # approved or rejected, or who else the manager added.
        awaiting = any(a.status == AssignmentStatus.PENDING.value for a in nominations)
        return templates.TemplateResponse(
            request,
            "select_reviewers.html",
            _select_context(
                db, cycle, user,
                nominated=sorted(a.reviewer.display_name for a in nominations),
                awaiting_approval=awaiting,
            ),
        )

    return templates.TemplateResponse(request, "select_reviewers.html", _select_context(db, cycle, user))


@router.post("/assignments/select")
def select_reviewers_submit(
    request: Request,
    reviewers: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    cycle = get_active_cycle(db)
    if cycle is None:
        return templates.TemplateResponse(
            request, "select_reviewers.html", {"error": "There is no active review cycle right now."}
        )

    def rerender(error: str):
        return templates.TemplateResponse(
            request, "select_reviewers.html", _select_context(db, cycle, user, error=error), status_code=400
        )

    names = [n.strip() for n in reviewers if n.strip()]
    if len(set(names)) != len(names):
        return rerender("Please nominate different people.")
    if not MIN_NOMINATIONS <= len(names) <= MAX_NOMINATIONS:
        return rerender(f"Please nominate between {MIN_NOMINATIONS} and {MAX_NOMINATIONS} different people.")

    chosen = eligible_reviewers(db, user).filter(User.username.in_(names)).all()
    if len(chosen) != len(names):
        return rerender("Reviewers must be other colleagues (not yourself or an HR admin).")

    if _my_nominations(db, cycle.id, user):
        return rerender("You've already nominated reviewers for this cycle.")

    for reviewer in chosen:
        db.add(
            ReviewerAssignment(
                cycle_id=cycle.id,
                reviewee_id=user.id,
                reviewer_id=reviewer.id,
                status=AssignmentStatus.PENDING.value,
                source=AssignmentSource.NOMINATED.value,
            )
        )
    log_action(db, user, "nominate_reviewers", f"cycle:{cycle.id}", {"nominees": sorted(names)})
    try:
        db.commit()
    except IntegrityError:
        # The manager or HR already added one of these people directly.
        db.rollback()
        return rerender("One of those people is already reviewing you this cycle; pick someone else.")

    approver = user.manager.display_name if user.manager else "HR"
    return flash(RedirectResponse("/my/dashboard", status_code=303), f"Nominations sent to {approver} for approval.")


@router.get("/admin/assignments")
def admin_assign_form(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    cycle = get_active_cycle(db)
    return templates.TemplateResponse(
        request, "admin_assignments.html", {"cycle": cycle, "people": _reviewable_people(db)}
    )


@router.post("/admin/assignments")
def admin_assign_submit(
    request: Request,
    reviewee: str = Form(...),
    reviewer: str = Form(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    cycle = get_active_cycle(db)
    people = _reviewable_people(db)

    def rerender(error: str):
        return templates.TemplateResponse(
            request, "admin_assignments.html", {"cycle": cycle, "people": people, "error": error}, status_code=400
        )

    if cycle is None:
        return rerender("There is no active review cycle.")
    if reviewee == reviewer:
        return rerender("A reviewee cannot review themself.")

    by_name = {p.username: p for p in people}  # HR admins are excluded, so they can't be picked on either side
    reviewee_user, reviewer_user = by_name.get(reviewee), by_name.get(reviewer)
    if reviewee_user is None or reviewer_user is None:
        return rerender("Both people must be existing, non-HR users.")

    db.add(
        ReviewerAssignment(
            cycle_id=cycle.id,
            reviewee_id=reviewee_user.id,
            reviewer_id=reviewer_user.id,
            status=AssignmentStatus.APPROVED.value,  # HR's own choice needs no further approval
            source=AssignmentSource.HR.value,
            decided_at=utcnow(),
        )
    )
    log_action(db, admin, "assign_reviewer", f"cycle:{cycle.id}", {"reviewee": reviewee, "reviewer": reviewer})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return rerender("That assignment already exists for this cycle.")

    return templates.TemplateResponse(
        request,
        "admin_assignments.html",
        {"cycle": cycle, "people": people, "message": f"Assigned {reviewer} to review {reviewee}."},
    )
