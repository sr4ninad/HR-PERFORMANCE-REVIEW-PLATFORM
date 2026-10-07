"""Reporting-line helpers for a multi-level org chart.

`users.manager_id` is a self-referencing FK, so the hierarchy can be any
depth (employee -> manager -> director -> ...). These helpers walk it
level by level -- one query per level, not per person -- and guard
against cycles so a bad edit can't loop forever.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Role, User

MAX_DEPTH = 32  # far deeper than any real org; a backstop, not a limit anyone should hit


@dataclass
class ReportLine:
    user: User
    depth: int  # 1 = direct report, 2 = skip-level, ...


def reporting_tree(db: Session, manager: User) -> list[ReportLine]:
    """Everyone who reports to `manager`, directly or transitively,
    ordered by depth then username."""
    lines: list[ReportLine] = []
    seen = {manager.id}
    frontier = [manager.id]
    depth = 0
    while frontier and depth < MAX_DEPTH:
        depth += 1
        level = (
            db.query(User)
            .filter(User.manager_id.in_(frontier), User.id.notin_(seen))
            .order_by(User.username)
            .all()
        )
        lines.extend(ReportLine(user=u, depth=depth) for u in level)
        seen.update(u.id for u in level)
        frontier = [u.id for u in level]
    return lines


def would_create_cycle(db: Session, user: User, new_manager_id: int | None) -> bool:
    """True if making `new_manager_id` the manager of `user` would make
    `user` (indirectly) their own manager."""
    current = new_manager_id
    for _ in range(MAX_DEPTH):
        if current is None:
            return False
        if current == user.id:
            return True
        manager = db.get(User, current)
        current = manager.manager_id if manager else None
    return True  # a chain this deep is itself malformed


def manager_problem(db: Session, user: User | None, manager_id_raw: str) -> tuple[int | None, str | None]:
    """Validate a submitted manager_id form value.

    Returns (manager_id, None) when valid, or (None, error message).
    `user` is None when creating a brand-new account (which can't be part
    of a cycle yet).
    """
    if not manager_id_raw:
        return None, None
    try:
        manager_id = int(manager_id_raw)
    except ValueError:
        return None, "Invalid manager."
    manager = db.get(User, manager_id)
    if manager is None or manager.role != Role.MANAGER.value:
        return None, "The selected manager doesn't exist or isn't a manager."
    if user is not None and would_create_cycle(db, user, manager_id):
        return None, "That would create a reporting loop (someone would end up managing themself)."
    return manager_id, None


def position_title(user: User) -> str:
    """The person's position, derived from role plus the org chart rather
    than stored, so it can't drift out of sync with reporting lines: a
    manager who manages other managers is a Director."""
    if user.role == Role.HR_ADMIN.value:
        return "HR Admin"
    if user.role == Role.MANAGER.value:
        if any(report.role == Role.MANAGER.value for report in user.direct_reports):
            return "Director"
        return "Manager"
    return "Employee"
