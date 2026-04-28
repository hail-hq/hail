"""Shared pytest fixtures for the Hail workspace.

Import the fixtures you need from here in each package's ``conftest.py``::

    from hailhq.core.testing.fixtures import database_url  # noqa: F401
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Resolve the test database URL.

    Honors a pre-set ``DATABASE_URL`` (e.g. CI's Postgres service
    container); otherwise spins up a session-scoped testcontainers
    Postgres so local dev works with no extra setup.
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "psycopg")
