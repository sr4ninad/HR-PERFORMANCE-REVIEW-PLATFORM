from collections import defaultdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.deps import get_page, require_login, require_role
from app.feedback import snapshot_of
from app.models import (
    AssignmentSource,
    AssignmentStatus,
    FinalReview,
    FinalReviewStatus,
    Review,
    ReviewerAssignment,
    ReviewCycle,
    Role,
    User,
)
from app.org import position_title, reporting_tree
from app.progress import cycle_overview, my_progress
from app.pagination import paginate, paginate_list
from app.routers.cycles import get_active_cycle
from app.routers.reviews import SKILL_LABELS
from app.template_utils import templates

router = APIRouter()


@router.get("/")
def index(user: User = Depends(require_login)):
    if user.role == Role.HR_ADMIN.value:
        return RedirectResponse("/admin/dashboard", status_code=303)
    return RedirectResponse("/my/dashboard", status_code=303)


@router.get("/my/dashboard")
def my_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_login),
):
    cycle = get_active_cycle(db)
    context = {"cycle": cycle, "user": user, "position": position_title(user)}
    if cycle is not None and user.role != Role.HR_ADMIN.value:
        context["my_steps"] = my_progress(db, cycle, user)

    # Managers and directors get a "Your team" summary.
    if user.role == Role.MANAGER.value:
        direct = list(user.direct_reports)
        context["team"] = {
            "direct": len(direct),
            "reporting_line": len(reporting_tree(db, user)),
            "released": (
                db.query(FinalReview)
                .filter(
                    FinalReview.cycle_id == cycle.id,
                    FinalReview.employee_id.in_([r.id for r in direct]),
                    FinalReview.status == FinalReviewStatus.RELEASED.value,
                )
                .count()
                if cycle is not None and direct
                else 0
            ),
        }

    if cycle is not None:
        my_reviewer_assignments = (
            db.query(ReviewerAssignment)
            .filter(
                ReviewerAssignment.cycle_id == cycle.id,
                ReviewerAssignment.reviewer_id == user.id,
                ReviewerAssignment.status == AssignmentStatus.APPROVED.value,
            )
            .all()
        )
        context["pending_to_write"] = [
            a for a in my_reviewer_assignments if a.review is None or not a.review.is_locked
        ]

        my_nominations = (
            db.query(ReviewerAssignment)
            .filter(
                ReviewerAssignment.cycle_id == cycle.id,
                ReviewerAssignment.reviewee_id == user.id,
                ReviewerAssignment.source == AssignmentSource.NOMINATED.value,
            )
            .all()
        )
        context["has_selected_reviewers"] = len(my_nominations) > 0
        context["awaiting_approval"] = any(a.status == AssignmentStatus.PENDING.value for a in my_nominations)

        if user.role in (Role.MANAGER.value, Role.HR_ADMIN.value):
            pending_decisions = (
                db.query(ReviewerAssignment)
                .join(User, User.id == ReviewerAssignment.reviewee_id)
                .filter(
                    ReviewerAssignment.cycle_id == cycle.id,
                    ReviewerAssignment.status == AssignmentStatus.PENDING.value,
                )
            )
            if user.role == Role.MANAGER.value:
                pending_decisions = pending_decisions.filter(User.manager_id == user.id)
            context["approvals_waiting"] = pending_decisions.count()

    # Only the manager's released final reviews count -- never raw peer reviews.
    context["released_reviews_count"] = (
        db.query(FinalReview)
        .filter(FinalReview.employee_id == user.id, FinalReview.status == FinalReviewStatus.RELEASED.value)
        .count()
    )

    return templates.TemplateResponse(request, "my_dashboard.html", context)


@router.get("/my/review")
def my_review(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    user: User = Depends(require_role(Role.EMPLOYEE.value, Role.MANAGER.value)),
):
    # The employee's view of their own feedback: the manager's final review
    # plus the averages frozen at release. Individual peer reviews -- and
    # anything that could identify a reviewer (names, timestamps, single
    # comments) -- are never shown here.
    finals_page = paginate(
        db.query(FinalReview)
        .filter(FinalReview.employee_id == user.id, FinalReview.status == FinalReviewStatus.RELEASED.value)
        .order_by(FinalReview.released_at.desc(), FinalReview.id.desc()),
        page,
    )
    return templates.TemplateResponse(
        request,
        "my_review.html",
        {
            "finals_page": finals_page,
            "snapshots": {f.id: snapshot_of(f) for f in finals_page.items},
            "skill_labels": SKILL_LABELS,
        },
    )


@router.get("/manager/reports")
def manager_reports(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    manager: User = Depends(require_role(Role.MANAGER.value)),
):
    cycle = get_active_cycle(db)
    # Whole reporting subtree, not just direct reports: a director sees
    # their managers' teams too (depth 2+ = skip-level).
    lines_page = paginate_list(reporting_tree(db, manager), page)

    assignments_by_reviewee: dict[int, list[ReviewerAssignment]] = defaultdict(list)
    if cycle is not None and lines_page.items:
        # One query for the whole page instead of one per person.
        for a in (
            db.query(ReviewerAssignment)
            .options(selectinload(ReviewerAssignment.review))
            .filter(
                ReviewerAssignment.cycle_id == cycle.id,
                ReviewerAssignment.reviewee_id.in_([line.user.id for line in lines_page.items]),
            )
        ):
            assignments_by_reviewee[a.reviewee_id].append(a)

    finals: dict[int, FinalReview] = {}
    if cycle is not None and lines_page.items:
        finals = {
            f.employee_id: f
            for f in db.query(FinalReview).filter(
                FinalReview.cycle_id == cycle.id,
                FinalReview.employee_id.in_([line.user.id for line in lines_page.items]),
            )
        }

    report_status = []
    for line in lines_page.items:
        row = {
            "user": line.user,
            "depth": line.depth,
            "state": "no_cycle",
            "submitted": 0,
            "total": 0,
            "pending_approval": 0,
            "final": None,
        }
        if cycle is not None:
            assignments = assignments_by_reviewee[line.user.id]
            approved = [a for a in assignments if a.status == AssignmentStatus.APPROVED.value]
            row["total"] = len(approved)
            row["submitted"] = sum(1 for a in approved if a.review is not None and a.review.is_locked)
            row["pending_approval"] = sum(1 for a in assignments if a.status == AssignmentStatus.PENDING.value)
            row["state"] = "in_progress" if assignments else "not_selected"
            row["final"] = finals.get(line.user.id)
        # Status only on this page. Review content is shown only to the
        # *direct* manager, merged and anonymised, on /team/reviews/{username};
        # skip-level managers never see content.
        report_status.append(row)

    return templates.TemplateResponse(
        request,
        "manager_reports.html",
        {"cycle": cycle, "report_status": report_status, "lines_page": lines_page},
    )


@router.get("/admin/dashboard")
def admin_dashboard(
    request: Request,
    page: int = Depends(get_page),
    db: Session = Depends(get_db),
    admin: User = Depends(require_role(Role.HR_ADMIN.value)),
):
    cycle = get_active_cycle(db)
    recent_cycles = db.query(ReviewCycle).order_by(ReviewCycle.created_at.desc(), ReviewCycle.id.desc()).limit(5).all()

    rows_page = None
    total = submitted = 0
    released_ids: set[int] = set()
    if cycle is not None:
        # Retract is refused once a final review is released from a review,
        # so the table doesn't offer it there.
        released_ids = {
            employee_id
            for (employee_id,) in db.query(FinalReview.employee_id).filter(
                FinalReview.cycle_id == cycle.id, FinalReview.status == FinalReviewStatus.RELEASED.value
            )
        }
        in_cycle = db.query(ReviewerAssignment).filter(ReviewerAssignment.cycle_id == cycle.id)
        # Totals come from COUNT queries over the whole cycle, so they stay
        # correct no matter which page of the table is being shown. Only
        # approved assignments count towards completion.
        total = in_cycle.filter(ReviewerAssignment.status == AssignmentStatus.APPROVED.value).count()
        submitted = (
            db.query(func.count(Review.id))
            .join(ReviewerAssignment)
            .filter(
                ReviewerAssignment.cycle_id == cycle.id,
                ReviewerAssignment.status == AssignmentStatus.APPROVED.value,
                Review.is_locked.is_(True),
            )
            .scalar()
        )
        rows_page = paginate(
            in_cycle.options(
                joinedload(ReviewerAssignment.reviewee),
                joinedload(ReviewerAssignment.reviewer),
                joinedload(ReviewerAssignment.review),
            ).order_by(ReviewerAssignment.id),
            page,
        )

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "cycle": cycle,
            "recent_cycles": recent_cycles,
            "rows_page": rows_page,
            "total": total,
            "submitted": submitted,
            "released_ids": released_ids,
            "overview": cycle_overview(db, cycle) if cycle is not None else None,
        },
    )
