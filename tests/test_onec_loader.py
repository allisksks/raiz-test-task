import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from market_map.integrations.onec import FileSource, load_onec
from market_map.integrations.onec import loader as loader_module
from market_map.integrations.onec.source import CONTRACTORS, SALES_FILE

pytestmark = pytest.mark.db

SAMPLES = Path(__file__).parents[1] / "data" / "samples" / "onec"


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _json_revenue() -> float:
    rows = json.loads((SAMPLES / f"{SALES_FILE}.json").read_text(encoding="utf-8"))["value"]
    return round(sum(r["СтоимостьTurnover"] for r in rows), 2)


@pytest.fixture(scope="module")
def loaded(test_engine):
    return load_onec(test_engine, FileSource(SAMPLES))


def test_full_load(loaded, test_engine):
    assert loaded.stats == {
        "product_groups": 5,
        "products": 25,
        "clients": 33,
        "sales_rows": 6168,
        "margin_plan_rows": 1956,
        "rejected": 0,
    }
    assert float(_scalar(test_engine, "select sum(revenue) from core.sales_fact")) == pytest.approx(
        _json_revenue()
    )
    status = _scalar(
        test_engine, "select status from staging.load_run where id = :i", i=loaded.run_id
    )
    assert status == "success"


def test_clients_regions_and_channels(loaded, test_engine):
    assert (
        _scalar(
            test_engine,
            "select c.channel_code || ' ' || r.region_code from core.company c "
            "join core.company_region r on r.company_id = c.id "
            "where c.name = 'ООО «СибДистрибуция»'",
        )
        == "distribution RU-NVS"
    )
    assert _scalar(test_engine, "select count(*) from core.company_region") == 33


def test_reload_is_idempotent(loaded, test_engine):
    load_onec(test_engine, FileSource(SAMPLES))
    assert _scalar(test_engine, "select count(*) from core.sales_fact") == 6168
    assert _scalar(test_engine, "select count(*) from core.margin_plan") == 1956
    assert _scalar(test_engine, "select count(*) from core.company") == 33
    assert _scalar(test_engine, "select count(*) from core.company_region") == 33


def test_partial_reload_replaces_only_its_months(loaded, test_engine):
    before = _scalar(
        test_engine, "select sum(revenue) from core.sales_fact where period < '2026-09-01'"
    )
    result = load_onec(test_engine, FileSource(SAMPLES), date(2026, 9, 1), date(2026, 9, 1))
    assert {r for r in result.stats if r.endswith("_rows")} == {"sales_rows", "margin_plan_rows"}
    assert (
        _scalar(test_engine, "select sum(revenue) from core.sales_fact where period < '2026-09-01'")
        == before
    )
    assert _scalar(test_engine, "select count(*) from core.sales_fact") == 6168


class BrokenRegionSource(FileSource):
    """Один клиент с нераспознаваемым регионом."""

    def catalog(self, entity):
        rows = super().catalog(entity)
        if entity == CONTRACTORS:
            rows = [dict(r) for r in rows]
            rows[0]["Регион"] = "Атлантида"
        return rows


def test_unrecognized_region_is_rejected_not_fatal(loaded, test_engine):
    result = load_onec(test_engine, BrokenRegionSource(SAMPLES), date(2026, 9, 1), date(2026, 9, 1))
    reasons = {reason for _, reason, _ in result.rejected}
    assert "не распознан регион: 'Атлантида'" in reasons
    assert result.stats["clients"] == 32
    assert _scalar(
        test_engine, "select count(*) from staging.load_reject where run_id = :i", i=result.run_id
    ) == len(result.rejected)


def test_failure_rolls_back_core_and_marks_run(loaded, test_engine, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("сбой загрузки плана")

    load_onec(test_engine, FileSource(SAMPLES))  # восстановить полный набор после прошлых тестов
    before = _scalar(test_engine, "select count(*) from core.sales_fact")
    monkeypatch.setattr(loader_module, "_load_margin_plan", boom)
    with pytest.raises(RuntimeError):
        load_onec(test_engine, FileSource(SAMPLES), date(2026, 9, 1), date(2026, 9, 1))

    assert _scalar(test_engine, "select count(*) from core.sales_fact") == before
    run = _scalar(
        test_engine, "select status || ': ' || error from staging.load_run order by id desc limit 1"
    )
    assert run.startswith("failed: RuntimeError")
    # Сырые данные неудачной загрузки сохранены для разбора
    assert (
        _scalar(
            test_engine,
            "select count(*) from staging.onec_raw where run_id = "
            "(select max(id) from staging.load_run)",
        )
        > 0
    )
