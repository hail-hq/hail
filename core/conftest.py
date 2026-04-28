import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hailhq.core.testing.fixtures import database_url  # noqa: F401


@pytest.fixture()
def session(database_url):  # noqa: F811 (re-used as a fixture parameter name)
    from hailhq.core.models import Base

    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
