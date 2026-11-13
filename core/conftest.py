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


def pytest_addoption(parser):
    parser.addoption(
        "--run-voice-evals",
        action="store_true",
        help="Run billed Bedrock voice behavior evaluations (no phone calls)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "voice_eval: opt-in live voice model evaluation")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-voice-evals"):
        return
    skip = pytest.mark.skip(reason="Use --run-voice-evals to run billed model checks")
    for item in items:
        if item.get_closest_marker("voice_eval"):
            item.add_marker(skip)


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
