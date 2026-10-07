"""Turns raw audit_log rows into readable timeline entries for HR.

    approve_reviewer  assignment:12  {"reviewee": "sneha.kulkarni", ...}
 -> "Priya Sharma approved Arjun Nair as a reviewer for Sneha Kulkarni"

All lookups for a page are batched (one query per kind of thing referenced),
and anything that can't be resolved -- a deleted cycle, a renamed user --
falls back to what the row itself recorded, so history always renders.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from markupsafe import Markup
from sqlalchemy.orm import Session, joinedload

from app.models import AuditLog, Review, ReviewCycle, ReviewerAssignment, User, as_utc

# action -> category. Categories drive the filter chips and the icon color.
CATEGORIES = {
    "reviews": ("Reviews", {
        "nominate_reviewers", "approve_reviewer", "reject_reviewer", "add_reviewer", "assign_reviewer",
        "submit_review", "retract_review", "save_final_review", "release_final_review",
    }),
    "cycles": ("Cycles", {"create_cycle", "activate_cycle", "close_cycle"}),
    "people": ("People", {"create_user", "update_user", "issue_invite", "issue_password_reset"}),
    "access": ("Sign-ins & passwords", {
        "login_success", "login_failure", "logout", "change_password", "password_reset", "password_rehashed",
        "accept_invite",
    }),
}
ACTION_CATEGORY = {action: key for key, (_, actions) in CATEGORIES.items() for action in actions}


@dataclass
class TimelineEntry:
    when: datetime
    category: str  # reviews / cycles / people / access / alert
    category_label: str
    icon: str
    sentence: Markup
    details: str | None = None  # pretty JSON for the "Recorded details" disclosure
    extra: list[str] = field(default_factory=list)


def _b(text) -> Markup:
    return Markup("<strong>%s</strong>") % text


class _Lookups:
    """Batch-load every user, cycle, assignment and review a page refers to."""

    def __init__(self, db: Session, rows: list[AuditLog]):
        usernames, cycle_ids, assignment_ids, review_ids = set(), set(), set(), set()
        for row in rows:
            kind, _, ref = row.target.partition(":")
            if kind == "user":
                usernames.add(ref)
            elif kind == "cycle" and ref.isdigit():
                cycle_ids.add(int(ref))
            elif kind == "assignment" and ref.isdigit():
                assignment_ids.add(int(ref))
            elif kind == "review" and ref.isdigit():
                review_ids.add(int(ref))
            d = _details(row)
            nominees = d.get("nominees") if isinstance(d.get("nominees"), list) else []
            for name in nominees + [d.get("reviewee"), d.get("reviewer")]:
                if isinstance(name, str):
                    usernames.add(name)
            cycle_id = d.get("cycle_id")
            if isinstance(cycle_id, int):
                cycle_ids.add(cycle_id)

        self.users = {u.username: u for u in db.query(User).filter(User.username.in_(usernames))} if usernames else {}
        self.users_by_id = {}
        self.cycles = {c.id: c for c in db.query(ReviewCycle).filter(ReviewCycle.id.in_(cycle_ids))} if cycle_ids else {}
        self.assignments = {}
        if assignment_ids:
            for a in db.query(ReviewerAssignment).options(
                joinedload(ReviewerAssignment.reviewee), joinedload(ReviewerAssignment.reviewer)
            ).filter(ReviewerAssignment.id.in_(assignment_ids)):
                self.assignments[a.id] = a
        self.reviews = {}
        if review_ids:
            for r in db.query(Review).options(
                joinedload(Review.assignment).joinedload(ReviewerAssignment.reviewee),
                joinedload(Review.assignment).joinedload(ReviewerAssignment.reviewer),
            ).filter(Review.id.in_(review_ids)):
                self.reviews[r.id] = r
        manager_ids = set()
        for row in rows:
            for key in ("manager_id",):
                value = _details(row).get(key)
                if isinstance(value, int):
                    manager_ids.add(value)
                if isinstance(value, list):
                    manager_ids.update(v for v in value if isinstance(v, int))
        if manager_ids:
            self.users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(manager_ids))}

    def person(self, username: str | None) -> str:
        if not username:
            return "someone"
        user = self.users.get(username)
        return user.display_name if user else username

    def person_by_id(self, user_id) -> str:
        if user_id is None:
            return "nobody"
        user = self.users_by_id.get(user_id)
        return user.display_name if user else f"user #{user_id}"

    def cycle(self, ref) -> str:
        cycle_id = _int(ref)
        cycle = self.cycles.get(cycle_id) if cycle_id is not None else None
        return cycle.name if cycle else f"cycle #{ref}"


def _int(value) -> int | None:
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _details(row: AuditLog) -> dict:
    if not row.details:
        return {}
    try:
        data = json.loads(row.details)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _ref(row: AuditLog) -> tuple[str, str]:
    kind, _, ref = row.target.partition(":")
    return kind, ref


def describe(row: AuditLog, look: _Lookups) -> TimelineEntry:
    d = _details(row)
    kind, ref = _ref(row)
    actor = _b(row.actor.display_name) if row.actor else _b("System")
    category = ACTION_CATEGORY.get(row.action, "access")
    icon = {"reviews": "review", "cycles": "cycle", "people": "person", "access": "key"}[category]
    extra: list[str] = []
    a = row.action

    def assignment_people():
        if kind == "assignment" and ref.isdigit() and int(ref) in look.assignments:
            asg = look.assignments[int(ref)]
            return asg.reviewer.display_name, asg.reviewee.display_name
        return look.person(d.get("reviewer")), look.person(d.get("reviewee"))

    if a == "login_success":
        sentence = actor + Markup(" signed in")
    elif a == "logout":
        sentence = actor + Markup(" signed out")
    elif a == "login_failure":
        category, icon = "alert", "alert"
        sentence = Markup("Failed sign-in attempt for ") + _b(look.person(ref) if kind == "user" else ref)
    elif a == "password_rehashed":
        sentence = actor + Markup("’s password hash was upgraded to Argon2id")
    elif a == "change_password":
        sentence = actor + Markup(" changed their password")
    elif a == "password_reset":
        sentence = actor + Markup(" set a new password using a reset link")
    elif a == "issue_invite":
        sentence = actor + Markup(" created an invite for ") + _b(look.person(ref))
        extra.append("they choose their own password")
    elif a == "accept_invite":
        sentence = actor + Markup(" accepted their invite and chose a password")
        extra.append("account activated")
    elif a == "issue_password_reset":
        sentence = actor + Markup(" created a password-reset link for ") + _b(look.person(ref))
    elif a == "create_user":
        role = str(d.get("role") or "user").replace("_", " ")
        sentence = actor + Markup(" added ") + _b(look.person(ref)) + Markup(" as %s") % (("an " if role[0] in "aeiou" else "a ") + role)
        if isinstance(d.get("manager_id"), int):
            sentence += Markup(", reporting to ") + _b(look.person_by_id(d["manager_id"]))
        if d.get("via"):
            extra.append(f"via {d['via']}")
    elif a == "update_user":
        sentence = actor + Markup(" updated ") + _b(look.person(ref))
        if isinstance(d.get("role"), list) and len(d["role"]) == 2:
            extra.append(f"role: {d['role'][0].replace('_', ' ')} → {d['role'][1].replace('_', ' ')}")
        if isinstance(d.get("manager_id"), list) and len(d["manager_id"]) == 2:
            extra.append(f"reports to: {look.person_by_id(d['manager_id'][0])} → {look.person_by_id(d['manager_id'][1])}")
    elif a == "create_cycle":
        sentence = actor + Markup(" created the ") + _b(d.get("name") or look.cycle(ref)) + Markup(" cycle as a draft")
    elif a == "activate_cycle":
        sentence = actor + Markup(" activated ") + _b(look.cycle(ref))
    elif a == "close_cycle":
        sentence = actor + Markup(" closed ") + _b(look.cycle(ref))
        if d.get("reason") == "superseded":
            extra.append("closed automatically when another cycle was activated")
    elif a == "nominate_reviewers":
        names = [look.person(n) for n in d.get("nominees", []) if isinstance(n, str)]
        sentence = actor + Markup(" nominated %d reviewers") % len(names) + Markup(" for ") + _b(look.cycle(ref))
        if names:
            extra.append(", ".join(names))
    elif a in ("approve_reviewer", "reject_reviewer"):
        reviewer, reviewee = assignment_people()
        verb = "approved" if a == "approve_reviewer" else "rejected"
        sentence = actor + Markup(" %s ") % verb + _b(reviewer) + Markup(" as a reviewer for ") + _b(reviewee)
    elif a in ("add_reviewer", "assign_reviewer"):
        sentence = actor + Markup(" added ") + _b(look.person(d.get("reviewer"))) + Markup(" as a reviewer for ") + _b(look.person(d.get("reviewee")))
    elif a == "submit_review":
        _, reviewee = assignment_people()
        sentence = actor + Markup(" submitted a peer review of ") + _b(reviewee)
        if d.get("overall_rating") is not None:
            extra.append(f"overall {d['overall_rating']}/5")
    elif a == "retract_review":
        review = look.reviews.get(_int(ref))
        if review:
            sentence = actor + Markup(" retracted ") + _b(review.assignment.reviewer.display_name) + Markup("’s review of ") + _b(review.assignment.reviewee.display_name)
        else:
            sentence = actor + Markup(" retracted a peer review")
        extra.append("original content preserved below")
    elif a == "save_final_review":
        sentence = actor + Markup(" saved a draft final review for ") + _b(look.person(ref))
    elif a == "release_final_review":
        sentence = actor + Markup(" released ") + _b(look.person(ref)) + Markup("’s final review")
        if d.get("final_rating") is not None:
            extra.append(f"rating {d['final_rating']}/5 from {d.get('responses', '?')} peer reviews")
    else:
        sentence = actor + Markup(" — %s on %s") % (a, row.target)

    if isinstance(d.get("cycle_id"), int) and a in ("save_final_review", "release_final_review"):
        extra.append(look.cycle(d["cycle_id"]))

    label = "Failed sign-in" if category == "alert" else CATEGORIES[category][0]
    show_details = a in ("retract_review",) and bool(d)
    return TimelineEntry(
        when=as_utc(row.timestamp),
        category=category,
        category_label=label,
        icon=icon,
        sentence=sentence,
        details=json.dumps(d, indent=2, ensure_ascii=False) if show_details else None,
        extra=extra,
    )


def build_timeline(db: Session, rows: list[AuditLog]) -> list[tuple[str, list[TimelineEntry]]]:
    """Entries grouped by day, newest first: [("Wed 7 Oct 2026", [...]), ...]."""
    look = _Lookups(db, rows)
    groups: list[tuple[str, list[TimelineEntry]]] = []
    for row in rows:
        entry = describe(row, look)
        day = entry.when.strftime("%a %d %b %Y")
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append(entry)
    return groups


__all__ = ["CATEGORIES", "build_timeline", "describe"]
