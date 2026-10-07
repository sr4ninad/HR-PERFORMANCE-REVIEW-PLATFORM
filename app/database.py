import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)


# Registered on the Engine class rather than on `engine` so every SQLite
# engine gets it -- including the per-test engines in tests/conftest.py and
# the one Alembic uses -- not just the app's default one.
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    # WAL lets readers and a writer proceed concurrently instead of SQLite's
    # default of locking the whole file for the duration of a write. Still
    # single-writer-at-a-time; see information.md for when Postgres is the
    # right next step.
    cursor.execute("PRAGMA journal_mode=WAL")
    # SQLite ships with foreign-key enforcement OFF for backwards
    # compatibility; without this, a users.manager_id pointing at a
    # nonexistent row would be stored silently.
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
