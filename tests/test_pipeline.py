from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from market_map.config import Settings
from market_map.integrations.onec import FileSource, HttpSource
from market_map.pipeline import next_run, onec_source, run_pipeline

MSK = ZoneInfo("Europe/Moscow")


def test_next_run_today_or_tomorrow():
    assert next_run(datetime(2026, 10, 22, 1, 0, tzinfo=MSK), "03:00") == datetime(
        2026, 10, 22, 3, 0, tzinfo=MSK
    )
    assert next_run(datetime(2026, 10, 22, 3, 0, tzinfo=MSK), "03:00") == datetime(
        2026, 10, 23, 3, 0, tzinfo=MSK
    )


def test_onec_source_files_by_default():
    source, start, end = onec_source(Settings(_env_file=None))
    assert isinstance(source, FileSource)
    assert (start, end) == (None, None)


def test_onec_source_live_reloads_last_three_months():
    settings = Settings(_env_file=None, onec_odata_url="http://1c.example/odata/standard.odata")
    source, start, end = onec_source(settings, today=date(2026, 10, 22))
    assert isinstance(source, HttpSource)
    assert (start, end) == (date(2026, 8, 1), date(2026, 10, 1))


@pytest.mark.db
def test_run_pipeline_loads_everything_and_releases_lock(test_engine):
    settings = Settings(_env_file=None)
    first = run_pipeline(test_engine, settings)
    assert first["1c"].stats["sales_rows"] == 6168
    assert first["prepared_db"].stats["prices"] == 11753
    # второй запуск подряд: блокировка освобождена, данные не задваиваются
    run_pipeline(test_engine, settings)
    with test_engine.connect() as conn:
        assert conn.execute(text("select count(*) from core.price_observation")).scalar() == 11753
        assert conn.execute(text("select count(*) from core.signal")).scalar() > 0
