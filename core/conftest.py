import pytest
from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_schemas,
    database_url,
    db,
    prepare_database,
    session_factory,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture()
def session(database_url, database_schemas, request):  # noqa: F811
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            prepare_database(
                conn,
                database_url,
                database_schemas,
                recreate=request.node.get_closest_marker("recreate_schema") is not None,
            )
        with Session(engine) as s:
            yield s
    finally:
        engine.dispose()
