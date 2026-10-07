"""Manager-side review workflow: approving nominated reviewers, and writing
the final review from an automatic merge of a report's peer reviews."""

from collections import defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.audit import log_action
from app.database import get_db
from app.flash import flash
from app.deps import get_page, require_role
from app.feedback import (
    MIN_RESPONSES_FOR_RELEASE,
    can_decide_reviewers,
    can_write_final_review,
    final_review_released,
    get_final_review,
    merge_peer_reviews,
    release_final_review,
)
from app.models import (
    AssignmentSource,
    AssignmentStatus,
    CycleStatus,
    FinalReview,
    ReviewCycle,
    ReviewerAssignment,
    Role,
    User,
    utcnow,
)
from app.pagination import paginate_list
from app.routers.assignments import eligible_reviewers
from app.routers.cycles import get_active_cycle
from app.routers.reviews import SKILL_LABELS
from app.services import find_banned_language, validate_rating
from app.template_utils import templates

router = APIRouter(prefix="/team")

MANAGER_OR_HR = require_role(Role.MANAGER.value, Role.HR_ADMIN.value)


# --- Reviewer approvals ----------------------------------------------------------

def _people_i_approve_for(db: Session, actor: User) -> list[User]:
    """Managers decide for their direct reports; HR for everyone (it's the
    fallback for people without a manager, and can override)."""
    query = db.query(User).filter(User.role != Role.HR_ADMIN.value)
    if actor.role != Role.HR_ADMIN.value:
        query = query.filter(User.manager_id == actor.id)
    return query.order_by(User.username).all()


def _approvals_context(db: Session, actor: User, page: int, **extra) -> dict:
    cycle = get_active_cycle(db)
    rows = []
    if cycle is not None:
        people = _people_i_approve_for(db, actor)
        by_reviewee: dict[int, list[ReviewerAssignment]] = defaultdict(list)
        for a in (
            db.query(ReviewerAssignment)
            .options(joinedload(ReviewerAssignment.reviewer))
            .filter(
                ReviewerAssignment.cycle_id == cycle.id,
                ReviewerAssignment.reviewee_id.in_([p.id for p in people]),
            )
            .order_by(ReviewerAssignment.id)
        ):
            by_reviewee[a.reviewee_id].append(a)
        for person in people:
            assignments = by_reviewee[person.id]
            rows.append(
                {
                    "person": person,
                    "assignments": assignments,
                    "pending": sum(1 for a in assignments if a.status == AssignmentStatus.PENDING.value),
                    "approved": sum(1 for a in assignments if a.status == AssignmentStatus.APPROVED.value),
                    "candidates": eligible_reviewers(db, person).all(),
                    "finalized": final_review_released(db, cycle.id, person.id),
                    # HR's only route to an unmanaged person's final review.
                    "writes_final_review": actor.role == Role.HR_ADMIN.value
                    and can_write_final_review(actor, person),
                }
            )
        # People with something waiting on me first.
        rows.sort(key=lambda r: (r["pending"] == 0, r["person"].username))
    return {
        "cycle": cycle,
        "rows_page": paginate_list(rows, page),
        "min_responses": MIN_RESPONSES_FOR_RELEASE,
        **extra,
    }


@router.get("/approvals")
def approvals(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    actor: User = Depends(MANAGER_OR_HR),
):
    return templates.TemplateResponse(request, "team_approvals.html", _approvals_context(db, actor, page))


def _decidable_assignment(db: Session, assignment_id: int, actor: User) -> ReviewerAssignment:
    assignment = db.get(ReviewerAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nomination not found")
    if not can_decide_reviewers(actor, assignment.reviewee):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only this person's manager or HR can decide")
    if assignment.status != AssignmentStatus.PENDING.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This nomination has already been decided")
    if assignment.cycle.status != CycleStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This review cycle is closed")
    if final_review_released(db, assignment.cycle_id, assignment.reviewee_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This person's review is already final")
    return assignment


def _decide(db: Session, assignment_id: int, actor: User, new_status: str, action: str) -> RedirectResponse:
    assignment = _decidable_assignment(db, assignment_id, actor)
    assignment.status = new_status
    assignment.decided_at = utcnow()
    log_action(
        db, actor, action, f"assignment:{assignment.id}",
        {"reviewee": assignment.reviewee.username, "reviewer": assignment.reviewer.username},
    )
    db.commit()
    verb = "Approved" if new_status == AssignmentStatus.APPROVED.value else "Rejected"
    return flash(
        RedirectResponse("/team/approvals", status_code=303),
        f"{verb} {assignment.reviewer.display_name} as a reviewer for {assignment.reviewee.display_name}.",
        "success" if verb == "Approved" else "info",
    )


@router.post("/approvals/{assignment_id}/approve")
def approve_nomination(assignment_id: int, db: Session = Depends(get_db), actor: User = Depends(MANAGER_OR_HR)):
    return _decide(db, assignment_id, actor, AssignmentStatus.APPROVED.value, "approve_reviewer")


@router.post("/approvals/{assignment_id}/reject")
def reject_nomination(assignment_id: int, db: Session = Depends(get_db), actor: User = Depends(MANAGER_OR_HR)):
    return _decide(db, assignment_id, actor, AssignmentStatus.REJECTED.value, "reject_reviewer")


@router.post("/reviewers/add")
def add_reviewer(
    request: Request,
    reviewee: str = Form(...),
    reviewer: str = Form(...),
    db: Session = Depends(get_db),
    actor: User = Depends(MANAGER_OR_HR),
):
    """The manager (or HR) adds a reviewer the employee didn't nominate --
    e.g. to replace a rejected nominee. Approved immediately."""

    def rerender(error: str):
        return templates.TemplateResponse(
            request, "team_approvals.html", _approvals_context(db, actor, 1, error=error), status_code=400
        )

    cycle = get_active_cycle(db)
    if cycle is None:
        return rerender("There is no active review cycle.")
    reviewee_user = db.query(User).filter(User.username == reviewee).first()
    if reviewee_user is None or not can_decide_reviewers(actor, reviewee_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only this person's manager or HR can add reviewers")
    if final_review_released(db, cycle.id, reviewee_user.id):
        return rerender(f"{reviewee}'s review for this cycle is already final.")
    reviewer_user = eligible_reviewers(db, reviewee_user).filter(User.username == reviewer).first()
    if reviewer_user is None:
        return rerender("The reviewer must be another non-HR colleague.")

    existing = (
        db.query(ReviewerAssignment)
        .filter_by(cycle_id=cycle.id, reviewee_id=reviewee_user.id, reviewer_id=reviewer_user.id)
        .first()
    )
    if existing is not None and existing.status == AssignmentStatus.APPROVED.value:
        return rerender(f"{reviewer} is already reviewing {reviewee} this cycle.")
    if existing is not None:
        # A pending or rejected nomination of the same person: approve it
        # rather than duplicating the row.
        existing.status = AssignmentStatus.APPROVED.value
        existing.decided_at = utcnow()
    else:
        db.add(
            ReviewerAssignment(
                cycle_id=cycle.id,
                reviewee_id=reviewee_user.id,
                reviewer_id=reviewer_user.id,
                status=AssignmentStatus.APPROVED.value,
                source=(AssignmentSource.HR if actor.role == Role.HR_ADMIN.value else AssignmentSource.MANAGER).value,
                decided_at=utcnow(),
            )
        )
    log_action(db, actor, "add_reviewer", f"cycle:{cycle.id}", {"reviewee": reviewee, "reviewer": reviewer})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return rerender("That reviewer is already assigned.")
    return flash(
        RedirectResponse("/team/approvals", status_code=303),
        f"Added {reviewer_user.display_name} as a reviewer for {reviewee_user.display_name}.",
    )


# --- Merged feedback & final review --------------------------------------------

def _reviewee_or_403(db: Session, username: str, actor: User) -> User:
    employee = db.query(User).filter(User.username == username).first()
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if not can_write_final_review(actor, employee):
        # The direct manager writes the final review; HR does for people
        # with no manager. Skip-level managers keep the status-only view
        # from /manager/reports.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this person's direct manager (or HR, if they have no manager) can write their review",
        )
    return employee


def _pick_cycle(db: Session, cycle_id: str | None) -> ReviewCycle:
    if cycle_id:
        try:
            cycle = db.get(ReviewCycle, int(cycle_id))
        except ValueError:
            cycle = None
    else:
        cycle = get_active_cycle(db) or (
            db.query(ReviewCycle)
            .filter(ReviewCycle.status != CycleStatus.DRAFT.value)
            .order_by(ReviewCycle.created_at.desc(), ReviewCycle.id.desc())
            .first()
        )
    if cycle is None or cycle.status == CycleStatus.DRAFT.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No review cycle to show")
    return cycle


def _review_context(db: Session, cycle: ReviewCycle, employee: User, **extra) -> dict:
    merged = merge_peer_reviews(db, cycle, employee)
    return {
        "cycle": cycle,
        "employee": employee,
        "merged": merged,
        "final": get_final_review(db, cycle.id, employee.id),
        "skill_labels": SKILL_LABELS,
        "min_responses": MIN_RESPONSES_FOR_RELEASE,
        **extra,
    }


@router.get("/reviews/{username}")
def final_review_form(
    request: Request,
    username: str,
    cycle_id: str | None = None,
    db: Session = Depends(get_db),
    author: User = Depends(MANAGER_OR_HR),
):
    employee = _reviewee_or_403(db, username, author)
    cycle = _pick_cycle(db, cycle_id)
    return templates.TemplateResponse(request, "team_review.html", _review_context(db, cycle, employee))


@router.post("/reviews/{username}")
def final_review_submit(
    request: Request,
    username: str,
    cycle_id: str = Form(...),
    action: str = Form("save"),
    summary: str = Form(""),
    strengths: str = Form(""),
    improvements: str = Form(""),
    final_rating: str = Form(""),
    db: Session = Depends(get_db),
    author: User = Depends(MANAGER_OR_HR),
):
    employee = _reviewee_or_403(db, username, author)
    cycle = _pick_cycle(db, cycle_id)
    final = get_final_review(db, cycle.id, employee.id)
    if final is not None and final.is_released:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This review has been released and is locked")

    summary, strengths, improvements = summary.strip(), strengths.strip(), improvements.strip()
    posted = {"summary": summary, "strengths": strengths, "improvements": improvements, "final_rating": final_rating}

    def rerender(error: str):
        return templates.TemplateResponse(
            request, "team_review.html", _review_context(db, cycle, employee, error=error, posted=posted),
            status_code=400,
        )

    rating = None
    if final_rating:
        try:
            rating = int(final_rating)
        except ValueError:
            rating = None
        if not validate_rating(rating):
            return rerender("Final rating must be between 1 and 5.")

    banned = find_banned_language(summary, strengths, improvements)
    if banned:
        return rerender(f"Please remove inappropriate language: {', '.join(banned)}")

    merged = merge_peer_reviews(db, cycle, employee)
    releasing = action == "release"
    if releasing:
        if not merged.can_release:
            return rerender(
                f"At least {MIN_RESPONSES_FOR_RELEASE} peer reviews must be submitted before release, "
                f"so no single reviewer can be identified from the averages ({merged.responses} so far)."
            )
        if not summary or rating is None:
            return rerender("A summary and a final rating are required to release the review.")

    now = utcnow()
    if final is None:
        final = FinalReview(cycle_id=cycle.id, employee_id=employee.id, manager_id=author.id, created_at=now)
        db.add(final)
    final.manager_id = author.id  # whoever is allowed to write it now is the author
    final.summary, final.strengths, final.improvements = summary, strengths, improvements
    final.final_rating = rating
    final.updated_at = now

    if releasing:
        release_final_review(final, merged, now)
        log_action(
            db, author, "release_final_review", f"user:{employee.username}",
            {"cycle_id": cycle.id, "final_rating": rating, "responses": merged.responses},
        )
    else:
        log_action(db, author, "save_final_review", f"user:{employee.username}", {"cycle_id": cycle.id})
    db.commit()
    response = RedirectResponse(f"/team/reviews/{employee.username}?cycle_id={cycle.id}", status_code=303)
    if releasing:
        return flash(response, f"Released {employee.display_name}'s final review. It's now locked.")
    return flash(response, f"Draft saved. {employee.display_name} can't see it until you release it.", "info")
