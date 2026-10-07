"""Reviewer approval rules and the manager's automatic merge of peer reviews.

How anonymity works: the person reviewed nominates their own reviewers, so
they always know the *pool*. Hiding names alone can't make feedback
anonymous. Instead, individual peer reviews are never shown to the person
reviewed at all. They only ever see their manager's final review plus
per-skill averages, and only once at least MIN_RESPONSES_FOR_RELEASE reviews
have been submitted, so no single review can be picked out of the average.
"""

import json
import random
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, selectinload

from app.models import (
    SKILL_AREAS,
    AssignmentStatus,
    FinalReview,
    FinalReviewStatus,
    ReviewCycle,
    ReviewerAssignment,
    Role,
    User,
)

MIN_NOMINATIONS = 3
MAX_NOMINATIONS = 5
# Fewer responses than this and an average could reveal one person's ratings.
MIN_RESPONSES_FOR_RELEASE = 3


def can_decide_reviewers(actor: User, reviewee: User) -> bool:
    """Who approves a reviewee's nominations: their direct manager, or HR.
    HR is the fallback for people with no manager (e.g. the top of the org)."""
    return actor.role == Role.HR_ADMIN.value or (
        reviewee.manager_id is not None and reviewee.manager_id == actor.id
    )


def is_direct_manager(actor: User, employee: User) -> bool:
    return employee.manager_id is not None and employee.manager_id == actor.id


def can_write_final_review(actor: User, employee: User) -> bool:
    """The direct manager writes the final review. For someone with no
    manager (the top of the org), HR does -- the same fallback as reviewer
    approval, so nobody's feedback is left without an author. HR admins
    aren't reviewed, so they never get one."""
    if employee.role == Role.HR_ADMIN.value:
        return False
    if employee.manager_id is None:
        return actor.role == Role.HR_ADMIN.value
    return is_direct_manager(actor, employee)


def get_final_review(db: Session, cycle_id: int, employee_id: int) -> FinalReview | None:
    return (
        db.query(FinalReview)
        .filter(FinalReview.cycle_id == cycle_id, FinalReview.employee_id == employee_id)
        .first()
    )


def final_review_released(db: Session, cycle_id: int, employee_id: int) -> bool:
    final = get_final_review(db, cycle_id, employee_id)
    return final is not None and final.is_released


@dataclass
class SkillSummary:
    area: str
    average: float | None
    comments: list[str] = field(default_factory=list)


@dataclass
class MergedFeedback:
    approved: int  # approved reviewers in this cycle
    responses: int  # of those, how many have submitted
    pending_approval: int
    skills: list[SkillSummary]
    overall_average: float | None
    notes: list[str]

    @property
    def can_release(self) -> bool:
        return self.responses >= MIN_RESPONSES_FOR_RELEASE

    @property
    def suggested_rating(self) -> int | None:
        """The merged average rounded to a whole 1-5 rating: a starting point
        the manager can accept or override."""
        if self.overall_average is None:
            return None
        return max(1, min(5, int(self.overall_average + 0.5)))

    def snapshot(self) -> dict:
        """What gets frozen into FinalReview.stats_snapshot on release: numbers
        only, never comments, so nothing traceable to one reviewer is shown
        to the employee."""
        return {
            "responses": self.responses,
            "overall_average": self.overall_average,
            "skills": {s.area: s.average for s in self.skills},
        }


def merge_peer_reviews(db: Session, cycle: ReviewCycle, employee: User) -> MergedFeedback:
    """Combine every submitted, approved peer review of `employee` in `cycle`.

    Comments are pooled per skill area and shuffled, so their order can't be
    matched to submission order. No reviewer identity or timestamp is
    included anywhere in the result.
    """
    assignments = (
        db.query(ReviewerAssignment)
        .options(selectinload(ReviewerAssignment.review))
        .filter(ReviewerAssignment.cycle_id == cycle.id, ReviewerAssignment.reviewee_id == employee.id)
        .all()
    )
    approved = [a for a in assignments if a.status == AssignmentStatus.APPROVED.value]
    pending = sum(1 for a in assignments if a.status == AssignmentStatus.PENDING.value)
    reviews = [a.review for a in approved if a.review is not None and a.review.is_locked]

    skills = []
    for area in SKILL_AREAS:
        ratings = [getattr(r, f"{area}_rating") for r in reviews if getattr(r, f"{area}_rating") is not None]
        comments = [getattr(r, f"{area}_text") for r in reviews if getattr(r, f"{area}_text")]
        random.shuffle(comments)
        skills.append(
            SkillSummary(
                area=area,
                average=round(sum(ratings) / len(ratings), 2) if ratings else None,
                comments=comments,
            )
        )

    overall = [r.overall_rating for r in reviews if r.overall_rating is not None]
    notes = [r.additional_notes for r in reviews if r.additional_notes]
    random.shuffle(notes)

    return MergedFeedback(
        approved=len(approved),
        responses=len(reviews),
        pending_approval=pending,
        skills=skills,
        overall_average=round(sum(overall) / len(overall), 2) if overall else None,
        notes=notes,
    )


def release_final_review(final: FinalReview, merged: MergedFeedback, now) -> None:
    """Lock the final review and freeze the merged numbers into it; the
    caller commits. Peer reviews submitted after this don't change what the
    employee sees (and new submissions are blocked, see reviews.py)."""
    final.status = FinalReviewStatus.RELEASED.value
    final.stats_snapshot = json.dumps(merged.snapshot())
    final.released_at = now
    final.updated_at = now


def snapshot_of(final: FinalReview) -> dict | None:
    return json.loads(final.stats_snapshot) if final.stats_snapshot else None
