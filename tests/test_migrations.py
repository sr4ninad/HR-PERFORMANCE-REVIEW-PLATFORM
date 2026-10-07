from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401 -- populate Base.metadata
from app.database import Base
from app.db_migrations import alembic_config, upgrade_database


def _engine(tmp_path, name="migrate.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def test_migrations_produce_exactly_the_model_schema(tmp_path):
    # If someone edits app/models.py without writing a migration, this fails
    # with the diff Alembic would autogenerate.
    engine = _engine(tmp_path)
    upgrade_database(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_pre_alembic_database_is_stamped_and_upgraded_without_data_loss(tmp_path):
    engine = _engine(tmp_path)
    # Recreate what the old create_all() bootstrap left behind: the 0001
    # schema, with data in it, and no alembic_version table.
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('legacy', 'pbkdf2$abc', 'employee', '2025-01-01 00:00:00')"
            )
        )

    upgrade_database(engine)

    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        row = conn.execute(text("SELECT username, session_version FROM users")).one()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    engine.dispose()

    assert {"login_attempts", "password_reset_tokens"} <= tables
    assert tuple(row) == ("legacy", 0)
    assert version == ScriptDirectory.from_config(cfg).get_current_head()


def test_existing_assignments_are_backfilled_as_approved(tmp_path):
    # Assignments created before the approval step existed must keep working.
    engine = _engine(tmp_path)
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0002")
        conn.execute(text(
            "INSERT INTO users (id, username, password_hash, role, created_at) VALUES "
            "(1, 'a', 'x', 'employee', '2026-01-01'), (2, 'b', 'x', 'employee', '2026-01-01')"
        ))
        conn.execute(text(
            "INSERT INTO review_cycles (id, name, start_date, end_date, status, created_at) "
            "VALUES (1, 'c', '2026-01-01', '2026-02-01', 'active', '2026-01-01')"
        ))
        conn.execute(text(
            "INSERT INTO reviewer_assignments (cycle_id, reviewee_id, reviewer_id, created_at) "
            "VALUES (1, 1, 2, '2026-01-01')"
        ))
        command.upgrade(cfg, "head")
        row = conn.execute(text("SELECT status, source FROM reviewer_assignments")).one()
    engine.dispose()
    assert tuple(row) == ("approved", "nominated")


def test_upgrade_repairs_duplicate_active_cycles_before_adding_unique_index(tmp_path):
    engine = _engine(tmp_path)
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        for name in ["old", "new"]:
            conn.execute(
                text(
                    "INSERT INTO review_cycles (name, start_date, end_date, status, created_at) "
                    "VALUES (:n, '2026-01-01', '2026-02-01', 'active', '2026-01-01')"
                ),
                {"n": name},
            )

    upgrade_database(engine)

    with engine.connect() as conn:
        statuses = dict(conn.execute(text("SELECT name, status FROM review_cycles")).all())
    engine.dispose()
    assert statuses == {"old": "closed", "new": "active"}


def test_downgrade_to_base_and_back(tmp_path):
    engine = _engine(tmp_path)
    upgrade_database(engine)
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
        assert set(inspect(conn).get_table_names()) == {"alembic_version"}
        command.upgrade(cfg, "head")
        assert "users" in inspect(conn).get_table_names()
    engine.dispose()


def test_existing_reset_links_are_backfilled_as_resets(tmp_path):
    engine = _engine(tmp_path)
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0003")
        conn.execute(text("INSERT INTO users (id, username, password_hash, role, created_at, session_version) "
                          "VALUES (1, 'a', 'x', 'employee', '2026-01-01', 0)"))
        conn.execute(text("INSERT INTO password_reset_tokens (user_id, token_hash, created_at, expires_at) "
                          "VALUES (1, 'h', '2026-01-01', '2026-01-02')"))
        command.upgrade(cfg, "head")
        purpose = conn.execute(text("SELECT purpose FROM password_reset_tokens")).scalar()
    engine.dispose()
    assert purpose == "reset"
