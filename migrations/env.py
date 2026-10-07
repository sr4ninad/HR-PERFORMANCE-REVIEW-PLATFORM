from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401 -- registers every model on Base.metadata
from app.config import DATABASE_URL
from app.database import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can't ALTER most things in place; batch mode lets future
        # migrations rebuild a table instead. No-op on Postgres.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # app/db_migrations.py passes in the app's own connection; the `alembic`
    # CLI doesn't, so build one from the same URL the app uses.
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        _configure(conn)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
