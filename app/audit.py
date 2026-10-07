import json

from sqlalchemy.orm import Session

from app.models import AuditLog, User


def log_action(
    db: Session,
    actor: User | None,
    action: str,
    target: str,
    details: dict | None = None,
) -> AuditLog:
    """Stage an audit row in the caller's transaction.

    Deliberately does not commit: the caller commits once, so the state
    change and its audit row land atomically -- there is no window where an
    action happened but its audit entry didn't (or vice versa).
    """
    entry = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        target=target,
        details=json.dumps(details) if details else None,
    )
    db.add(entry)
    return entry
