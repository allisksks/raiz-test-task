import pytest
from sqlalchemy.exc import OperationalError

from market_map.cli import main
from market_map.db import get_engine, missing_schemas

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine():
    engine = get_engine()
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("PostgreSQL недоступен")
    return engine


def test_schemas_created_by_init_scripts(engine):
    assert missing_schemas(engine) == []


def test_cli_check(engine):
    assert main(["check"]) == 0


def test_migrate_is_idempotent_and_seeds_reference(engine):
    from sqlalchemy import text

    from market_map.migrate import migrate

    migrate(engine)
    assert migrate(engine) == []
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from core.region")).scalar() == 85
        assert conn.execute(text("select count(*) from core.channel")).scalar() == 6
