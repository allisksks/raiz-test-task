import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from market_map.config import get_settings
from market_map.db import SCHEMAS
from market_map.migrate import migrate


@pytest.fixture(scope="session")
def test_engine():
    """Отдельная база `<POSTGRES_DB>_test` с применёнными миграциями.

    Создаётся заново на каждый прогон, рабочая база не затрагивается.
    """
    url = make_url(get_settings().database_url)
    test_db = f"{url.database}_test"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'drop database if exists "{test_db}" with (force)'))
            conn.execute(text(f'create database "{test_db}"'))
    except OperationalError:
        pytest.skip("PostgreSQL недоступен")
    finally:
        admin.dispose()

    engine = create_engine(url.set(database=test_db))
    with engine.begin() as conn:
        for schema in SCHEMAS:
            conn.execute(text(f"create schema {schema}"))
    migrate(engine)
    yield engine
    engine.dispose()
