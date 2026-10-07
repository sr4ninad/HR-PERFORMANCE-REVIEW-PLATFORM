import enum
import re
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands DateTime(timezone=True) columns back as naive datetimes;
    every value here is written in UTC, so reattach that before comparing
    against an aware `utcnow()`."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Role(str, enum.Enum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    HR_ADMIN = "hr_admin"


class CycleStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


class AssignmentStatus(str, enum.Enum):
    """Nominations start PENDING until the reviewee's manager decides;
    only APPROVED assignments can be written."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssignmentSource(str, enum.Enum):
    NOMINATED = "nominated"  # chosen by the reviewee, needs approval
    MANAGER = "manager"  # added by the reviewee's manager, approved on creation
    HR = "hr"  # added by HR, approved on creation


class FinalReviewStatus(str, enum.Enum):
    DRAFT = "draft"
    RELEASED = "released"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Algorithm-tagged string, e.g. "pbkdf2$<base64 salt+hash>" (legacy) or
    # "argon2id$<argon2-cffi encoded hash>" (current). See security.py.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=Role.EMPLOYEE.value)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Embedded in every session cookie; bumping it (password change/reset)
    # invalidates every session issued before, on every device.
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    manager: Mapped["User | None"] = relationship(remote_side=[id], back_populates="direct_reports")
    direct_reports: Mapped[list["User"]] = relationship(back_populates="manager")

    @property
    def display_name(self) -> str:
        """"priya.sharma" -> "Priya Sharma". Usernames are first.last by
        convention, so no separate name column is needed."""
        return " ".join(part.capitalize() for part in re.split(r"[._-]+", self.username) if part)

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"


class ReviewCycle(Base):
    __tablename__ = "review_cycles"
    __table_args__ = (
        # "Only one active cycle" is enforced by the database, not just by
        # activate_cycle(): a partial unique index over the rows where
        # status = 'active' (supported by both SQLite and Postgres).
        Index(
            "uq_one_active_cycle",
            "status",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CycleStatus.DRAFT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assignments: Mapped[list["ReviewerAssignment"]] = relationship(back_populates="cycle")


class ReviewerAssignment(Base):
    __tablename__ = "reviewer_assignments"
    __table_args__ = (
        UniqueConstraint("cycle_id", "reviewee_id", "reviewer_id", name="uq_assignment_triplet"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("review_cycles.id"), nullable=False)
    reviewee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AssignmentStatus.APPROVED.value, server_default="approved"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AssignmentSource.NOMINATED.value, server_default="nominated"
    )
    # Who decided is recorded in the audit log (approve_reviewer / reject_reviewer).
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    cycle: Mapped["ReviewCycle"] = relationship(back_populates="assignments")
    reviewee: Mapped["User"] = relationship(foreign_keys=[reviewee_id])
    reviewer: Mapped["User"] = relationship(foreign_keys=[reviewer_id])
    review: Mapped["Review | None"] = relationship(back_populates="assignment", uselist=False)


SKILL_AREAS = [
    "work_quality",
    "productivity",
    "communication",
    "collaboration",
    "initiative",
    "punctuality",
]


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(
        ForeignKey("reviewer_assignments.id"), unique=True, nullable=False
    )

    work_quality_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_quality_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    productivity_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    productivity_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    communication_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    communication_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collaboration_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    collaboration_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    initiative_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    initiative_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    punctuality_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    punctuality_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)

    additional_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    overall_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assignment: Mapped["ReviewerAssignment"] = relationship(back_populates="review")

    def ratings(self) -> list[int]:
        return [getattr(self, f"{area}_rating") for area in SKILL_AREAS if getattr(self, f"{area}_rating") is not None]


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    actor: Mapped["User | None"] = relationship()


class LoginAttempt(Base):
    """One failed login, keyed by "user:<name>" or "ip:<addr>".

    Lives in the database rather than process memory so the lockout is
    shared by every worker process and survives restarts.
    """

    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_key_time", "key", "attempted_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class TokenPurpose(str, enum.Enum):
    INVITE = "invite"  # a new account choosing its first password
    RESET = "reset"  # an existing account getting a new password


class PasswordResetToken(Base):
    """A single-use, expiring link that lets someone set their own password:
    either an invite for a brand-new account or a reset for an existing one.
    HR issues it but never learns the password chosen with it.

    Only a SHA-256 digest of the token is stored, so a database leak doesn't
    hand out working links.
    """

    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TokenPurpose.RESET.value, server_default="reset"
    )

    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class FinalReview(Base):
    """The manager's consolidated review of one employee for one cycle.

    The manager writes it from an automatic merge of every submitted peer
    review (per-skill averages + anonymised comments). It's the only review
    content the employee ever sees: individual peer reviews are never shown
    to the person reviewed, which is what makes them anonymous. On release,
    the merged numbers are frozen into `stats_snapshot` and the row locks.
    """

    __tablename__ = "final_reviews"
    __table_args__ = (UniqueConstraint("cycle_id", "employee_id", name="uq_final_review_cycle_employee"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("review_cycles.id"), nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvements: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=FinalReviewStatus.DRAFT.value)
    stats_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, written on release
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cycle: Mapped["ReviewCycle"] = relationship()
    employee: Mapped["User"] = relationship(foreign_keys=[employee_id])
    manager: Mapped["User"] = relationship(foreign_keys=[manager_id])

    @property
    def is_released(self) -> bool:
        return self.status == FinalReviewStatus.RELEASED.value
