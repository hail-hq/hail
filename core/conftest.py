import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture(scope="session")
def database_url():
    """Resolve the test database URL.

    If the ``DATABASE_URL`` environment variable is set (e.g. CI's Postgres
    service container), use it directly and skip spinning up a testcontainer.
    Otherwise fall back to a session-scoped ``testcontainers`` Postgres so
    local dev works with no extra setup.
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return

    # Lazy import so test runs against an external DATABASE_URL don't need
    # the testcontainers/docker stack installed or running.
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "psycopg")


@pytest.fixture()
def session(database_url):
    from hailhq.core.models import Base

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
