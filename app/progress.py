"""Progress rings and steppers for the dashboards.

Pure data: each ring is one ratio (a single-value meter) and each stepper
step is done / current / todo. Templates in _ui.html draw them.
"""

import math
from dataclasses import dataclass

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.models import (
    AssignmentSource,
    AssignmentStatus,
    FinalReview,
    FinalReviewStatus,
    Review,
    ReviewCycle,
    ReviewerAssignment,
    Role,
    User,
)

RING_RADIUS = 40
RING_CIRCUMFERENCE = round(2 * math.pi * RING_RADIUS, 2)


@dataclass
class Ring:
    label: str
    caption: str
    value: int
    total: int

    @property
    def ratio(self) -> float:
        return min(1.0, self.value / self.total) if self.total else 0.0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.value >= self.total

    @property
    def circumference(self) -> float:
        return RING_CIRCUMFERENCE

    @property
    def offset(self) -> float:
        """stroke-dashoffset: the unfilled length of the circle."""
        return round(RING_CIRCUMFERENCE * (1 - self.ratio), 2)

    @property
    def description(self) -> str:
        return f"{self.label}: {self.value} of {self.total} {self.caption}"


@dataclass
class Step:
    name: str
    note: str
    state: str  # "done", "current" or "todo"


def _step(name: str, note: str, done: bool, started: bool) -> Step:
    return Step(name, note, "done" if done else "current" if started else "todo")


def cycle_overview(db: Session, cycle: ReviewCycle) -> dict:
    """Org-wide progress for HR: four rings and the six-stage stepper.

    Stages overlap in real life (some people are still nominating while
    others' reviews are in), so each stage is judged on its own: done when
    complete, current while under way, todo before it starts.
    """
    in_cycle = db.query(ReviewerAssignment).filter(ReviewerAssignment.cycle_id == cycle.id)
    people = db.query(User).filter(User.role != Role.HR_ADMIN.value).count()
    with_nominations = (
        db.query(func.count(distinct(ReviewerAssignment.reviewee_id)))
        .filter(ReviewerAssignment.cycle_id == cycle.id)
        .scalar()
    )
    assignments = in_cycle.count()
    decided = in_cycle.filter(ReviewerAssignment.status != AssignmentStatus.PENDING.value).count()
    approved = in_cycle.filter(ReviewerAssignment.status == AssignmentStatus.APPROVED.value).count()
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
    reviewees = (
        db.query(func.count(distinct(ReviewerAssignment.reviewee_id)))
        .filter(ReviewerAssignment.cycle_id == cycle.id, ReviewerAssignment.status == AssignmentStatus.APPROVED.value)
        .scalar()
    )
    released = (
        db.query(FinalReview)
        .filter(FinalReview.cycle_id == cycle.id, FinalReview.status == FinalReviewStatus.RELEASED.value)
        .count()
    )

    rings = [
        Ring("Nominated", "people have reviewers", with_nominations, people),
        Ring("Approvals", "nominations decided", decided, assignments),
        Ring("Peer reviews", "reviews submitted", submitted, approved),
        Ring("Final reviews", "released to employees", released, reviewees),
    ]
    pending = assignments - decided
    steps = [
        Step("Open", cycle.name, "done"),
        _step("Nominate", f"{with_nominations} of {people} people", people > 0 and with_nominations >= people, with_nominations > 0),
        _step(
            "Approve",
            "no nominations yet" if not assignments else f"{pending} pending" if pending else "all decided",
            assignments > 0 and pending == 0,
            assignments > 0,
        ),
        _step("Review", f"{submitted} of {approved} in" if approved else "no reviewers yet",
              approved > 0 and submitted >= approved, approved > 0),
        _step("Finalize", f"{released} of {reviewees} released" if reviewees else "nothing to release yet",
              reviewees > 0 and released >= reviewees, released > 0 or submitted > 0),
        Step("Close", "HR closes the cycle", "todo"),
    ]
    return {"rings": rings, "steps": steps}


def my_progress(db: Session, cycle: ReviewCycle, user: User) -> list[Step]:
    """One employee's own review, as four steps.

    Deliberately shows no counts of submitted peer reviews: seeing "2 of 3"
    tick up would let someone time when each colleague submitted, which
    undermines anonymity.
    """
    mine = (
        db.query(ReviewerAssignment)
        .filter(ReviewerAssignment.cycle_id == cycle.id, ReviewerAssignment.reviewee_id == user.id)
        .all()
    )
    nominated = any(a.source == AssignmentSource.NOMINATED.value for a in mine)
    awaiting = any(a.status == AssignmentStatus.PENDING.value for a in mine)
    has_reviewers = any(a.status == AssignmentStatus.APPROVED.value for a in mine)
    released = (
        db.query(FinalReview)
        .filter(
            FinalReview.cycle_id == cycle.id,
            FinalReview.employee_id == user.id,
            FinalReview.status == FinalReviewStatus.RELEASED.value,
        )
        .first()
        is not None
    )
    decider = user.manager.display_name if user.manager else "HR"
    return [
        # Nominating is always the employee's first move, so it's "current" until done.
        _step("Nominate", "Pick 3–5 colleagues", nominated or has_reviewers, True),
        _step("Approval", f"{decider} decides", (nominated or has_reviewers) and not awaiting, awaiting),
        _step("Peer reviews", "Written anonymously", released, has_reviewers and not awaiting),
        _step("Final review", f"From {decider}", released, False),
    ]
