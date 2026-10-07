from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.flash import flash
from app.deps import get_page, require_role
from app.feedback import final_review_released
from app.models import AssignmentStatus, CycleStatus, Review, ReviewerAssignment, Role, SKILL_AREAS, User
from app.pagination import paginate
from app.routers.cycles import get_active_cycle
from app.services import compute_overall_rating, find_banned_language, validate_rating
from app.template_utils import templates

router = APIRouter()

SKILL_LABELS = {
    "work_quality": "Work Quality",
    "productivity": "Productivity",
    "communication": "Communication",
    "collaboration": "Collaboration",
    "initiative": "Initiative",
    "punctuality": "Punctuality",
}


@router.get("/reviews/pick")
def pick_reviewee(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    cycle = get_active_cycle(db)
    if cycle is None:
        return templates.TemplateResponse(
            request, "pick_reviewee.html", {"error": "There is no active review cycle right now."}
        )

    assignments = (
        db.query(ReviewerAssignment)
        .filter(
            ReviewerAssignment.cycle_id == cycle.id,
            ReviewerAssignment.reviewer_id == user.id,
            # Nominations only become writable once the reviewee's manager approves them.
            ReviewerAssignment.status == AssignmentStatus.APPROVED.value,
        )
        .all()
    )
    # Phase 6 fix: the original Pickempfront.py indexed reviewee_names[0]
    # unconditionally and crashed with IndexError when nobody had picked you
    # as a reviewer yet. Here an empty list renders a proper empty state.
    pending = [a for a in assignments if a.review is None or not a.review.is_locked]

    return templates.TemplateResponse(
        request, "pick_reviewee.html", {"cycle": cycle, "assignments": pending}
    )


def _get_owned_assignment(db: Session, assignment_id: int, user: User) -> ReviewerAssignment:
    assignment = db.get(ReviewerAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")
    if assignment.reviewer_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your assignment")
    if assignment.status != AssignmentStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This review hasn't been approved by their manager"
        )
    return assignment


def _cycle_is_open(assignment: ReviewerAssignment) -> bool:
    return assignment.cycle.status == CycleStatus.ACTIVE.value


@router.get("/reviews/write/{assignment_id}")
def write_review_form(
    request: Request,
    assignment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    assignment = _get_owned_assignment(db, assignment_id, user)
    return templates.TemplateResponse(
        request,
        "review_form.html",
        {
            "assignment": assignment,
            "skill_areas": SKILL_AREAS,
            "skill_labels": SKILL_LABELS,
            "existing": assignment.review,
            "cycle_open": _cycle_is_open(assignment),
        },
    )


@router.post("/reviews/write/{assignment_id}")
async def write_review_submit(
    request: Request,
    assignment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    assignment = _get_owned_assignment(db, assignment_id, user)

    if assignment.review is not None and assignment.review.is_locked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This review is already submitted and locked"
        )
    # Reviews can only be written while their cycle is running; once HR
    # closes a cycle its results are final.
    if not _cycle_is_open(assignment):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This review cycle is closed; reviews can no longer be submitted"
        )
    # Once the manager releases the final review, its numbers are frozen;
    # a late peer review would otherwise be silently ignored.
    if final_review_released(db, assignment.cycle_id, assignment.reviewee_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This person's review has already been finalized by their manager"
        )

    form = await request.form()
    additional_notes = (form.get("additional_notes") or "").strip()

    texts: dict[str, str] = {}
    ratings: dict[str, int | None] = {}
    rating_error = None
    for area in SKILL_AREAS:
        texts[area] = (form.get(f"{area}_text") or "").strip()
        raw_rating = form.get(f"{area}_rating")
        try:
            rating_value = int(raw_rating)
        except (TypeError, ValueError):
            rating_value = None
        if not validate_rating(rating_value):
            rating_error = f"{SKILL_LABELS[area]} rating must be between 1 and 5."
        ratings[area] = rating_value

    def rerender(error: str):
        return templates.TemplateResponse(
            request,
            "review_form.html",
            {
                "assignment": assignment,
                "skill_areas": SKILL_AREAS,
                "skill_labels": SKILL_LABELS,
                "error": error,
                "cycle_open": True,
                "posted": {**texts, **{f"{a}_rating": ratings[a] for a in SKILL_AREAS}, "additional_notes": additional_notes},
            },
            status_code=400,
        )

    if rating_error:
        return rerender(rating_error)

    # Phase 6 fix: single-pass, word-boundary profanity check instead of the
    # original's per-word loop that both mis-flagged substrings ("mad" in
    # "made") and popped one messagebox per matched word.
    banned = find_banned_language(additional_notes, *texts.values())
    if banned:
        return rerender(f"Please remove inappropriate language: {', '.join(banned)}")

    review = assignment.review
    if review is None:
        review = Review(assignment_id=assignment.id)
        db.add(review)

    for area in SKILL_AREAS:
        setattr(review, f"{area}_text", texts[area])
        setattr(review, f"{area}_rating", ratings[area])
    review.additional_notes = additional_notes

    # Phase 6 fix: overall_rating is computed here, at submission time, not
    # recomputed from scratch on every display like the original showreview.py
    # did with `sum(ratings) // 6` (integer division that silently truncated).
    review.overall_rating = compute_overall_rating(review)
    review.is_locked = True
    review.submitted_at = datetime.now(timezone.utc)

    log_action(db, user, "submit_review", f"assignment:{assignment.id}", {"overall_rating": review.overall_rating})
    db.commit()

    return flash(
        RedirectResponse("/reviews/pick", status_code=303),
        f"Your review of {assignment.reviewee.display_name} is submitted and locked.",
    )


@router.post("/reviews/{review_id}/retract")
def retract_review(
    review_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    # Phase 5: this replaces the original delete_review() hard delete. HR
    # can unlock a submitted review for rewrite, but the full cleared
    # content (every rating, comment and note) is preserved in the audit
    # row, and the assignment/review row itself survives rather than being
    # deleted outright -- nothing that was written is ever unrecoverable.
    review = db.get(Review, review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review not found")
    if not review.is_locked:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only a submitted review can be retracted")
    if not _cycle_is_open(review.assignment):
        # Unlocking a review in a closed cycle would leave it unwritable
        # forever (submission requires an active cycle).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Reviews in a closed cycle can't be retracted"
        )
    if final_review_released(db, review.assignment.cycle_id, review.assignment.reviewee_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The manager has already released a final review built from it"
        )

    previous = {
        "ratings": {area: getattr(review, f"{area}_rating") for area in SKILL_AREAS},
        "texts": {area: getattr(review, f"{area}_text") for area in SKILL_AREAS},
        "additional_notes": review.additional_notes,
        "overall_rating": review.overall_rating,
        "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
    }
    for area in SKILL_AREAS:
        setattr(review, f"{area}_text", None)
        setattr(review, f"{area}_rating", None)
    review.additional_notes = None
    review.overall_rating = None
    review.is_locked = False
    review.submitted_at = None

    log_action(db, admin, "retract_review", f"review:{review.id}", {"cleared": previous})
    db.commit()

    return flash(
        RedirectResponse("/admin/dashboard", status_code=303),
        f"Retracted {review.assignment.reviewer.display_name}'s review of {review.assignment.reviewee.display_name}. "
        "It's unlocked for rewrite; the original is kept in the audit log.",
        "info",
    )


@router.get("/reviews/view/{username}")
def view_reviews(
    request: Request,
    username: str,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    # Raw individual peer reviews: HR only, read-only, for dispute
    # resolution. The person reviewed never sees these -- only their
    # manager's final review and the aggregate numbers (/my/review) -- which
    # is what keeps peer feedback anonymous.
    target = db.query(User).filter(User.username == username).first()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    reviews_page = paginate(
        db.query(Review)
        .join(ReviewerAssignment)
        .filter(ReviewerAssignment.reviewee_id == target.id, Review.is_locked.is_(True))
        .order_by(Review.submitted_at.desc(), Review.id.desc()),
        page,
    )

    return templates.TemplateResponse(
        request,
        "show_reviews.html",
        {
            "target": target,
            "reviews_page": reviews_page,
            "skill_areas": SKILL_AREAS,
            "skill_labels": SKILL_LABELS,
            "read_only": True,
        },
    )
