import os
from logging.config import fileConfig

from alembic import context
from hailhq.core.db import to_sync_url
from sqlalchemy import engine_from_config, pool

config = context.config

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Export it before running alembic so the "
        "migration targets the right database — there is no implicit default."
    )
config.set_main_option("sqlalchemy.url", to_sync_url(db_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            # Migration safety guards. See docs/operations.md → Migration
            # discipline. SET LOCAL keeps these scoped to the current
            # transaction so they survive PgBouncer transaction pooling
            # (e.g. Neon's -pooler endpoint, which rejects libpq's PGOPTIONS
            # startup parameter outright).
            connection.exec_driver_sql("SET LOCAL statement_timeout = '120s'")
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
