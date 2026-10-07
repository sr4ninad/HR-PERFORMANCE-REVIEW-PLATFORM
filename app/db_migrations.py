"""Run Alembic migrations programmatically at startup.

`uvicorn app.main:app` still needs zero setup: on boot the schema is
brought to the latest revision. Three starting states are handled:

  * empty database            -> every migration runs from scratch
  * pre-Alembic database       -> built by the old Base.metadata.create_all()
    (tables exist, no             bootstrap, which matches revision 0001
     alembic_version table)       exactly, so it's stamped at 0001 and
                                  upgraded from there -- nothing re-created,
                                  no data lost
  * already-migrated database  -> only newer revisions (if any) run
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.config import BASE_DIR

BASELINE_REVISION = "0001"


def alembic_config() -> Config:
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    # Keep Alembic's fileConfig() from reconfiguring the running server's logging.
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade_database(engine: Engine) -> None:
    cfg = alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        tables = set(inspect(connection).get_table_names())
        if "users" in tables and "alembic_version" not in tables:
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")
