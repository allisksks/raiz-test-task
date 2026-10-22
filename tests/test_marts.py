from pathlib import Path

import pytest
from sqlalchemy import text

from market_map.integrations.onec import FileSource, load_onec
from market_map.integrations.prepared import load_prepared

pytestmark = pytest.mark.db

SAMPLES = Path(__file__).parents[1] / "data" / "samples"


@pytest.fixture(scope="module")
def db(test_engine):
    # Витрины пересчитываются загрузчиками сами — отдельный refresh не нужен
    load_onec(test_engine, FileSource(SAMPLES / "onec"))
    load_prepared(test_engine, f"sqlite:///{SAMPLES / 'prepared' / 'prepared.sqlite'}")
    with test_engine.connect() as conn:
        yield conn


def _all(db, sql: str, **params):
    return db.execute(text(sql), params).all()


def _one(db, sql: str, **params):
    return db.execute(text(sql), params).scalar()


def test_region_summary_covers_all_regions(db):
    assert _one(db, "select count(*) from marts.region_summary") == 85
    assert _one(db, "select count(*) from marts.region_summary where clients > 0") == 10
    new = dict(_all(db, "select region_code, new_competitors from marts.region_summary"))
    assert {code for code, n in new.items() if n} == {"RU-NVS", "RU-SAM"}


def test_s1_competitor_25_percent_cheaper_in_same_channel(db):
    gap = _one(
        db,
        "select avg(price_gap) from marts.price_comparison "
        "where competitor = 'ООО «СибСтройОпт»' and own_price_basis = 'канал конкурента'",
    )
    assert -0.30 < gap < -0.20


def test_s1_margin_below_plan(db):
    deviation = _one(
        db,
        "select sum(margin) / sum(revenue) - sum(plan_revenue * plan_margin_pct) / "
        "sum(plan_revenue) from marts.margin_plan_fact where region_code = 'RU-NVS' "
        "and product_group = 'Сухие смеси' and channel = 'Дистрибуция' and month = '2026-09-01'",
    )
    assert deviation < -0.05


def _monthly_gap(db, competitor: str, region: str, group: str) -> dict[str, float]:
    rows = _all(
        db,
        "select to_char(month, 'YYYY-MM'), avg(price_gap) from marts.price_comparison "
        "where competitor = :c and region_code = :r and product_group = :g and comparable "
        "group by 1",
        c=competitor,
        r=region,
        g=group,
    )
    return {month: float(gap) for month, gap in rows}


def test_s2_competitor_price_increase(db):
    gap = _monthly_gap(db, "ООО «ЮгСтройСнаб»", "RU-KDA", "Гидроизоляция")
    assert gap["2026-10"] - gap["2026-08"] > 0.08


def test_s4_temporary_promo(db):
    weekly = dict(
        _all(
            db,
            "select to_char(observed_at, 'MM-DD'), avg(competitor_price / own_price) "
            "from marts.price_comparison where competitor = 'ООО «КазаньСтройТорг»' "
            "and product_group = 'Утеплители' and comparable group by 1",
        )
    )
    assert weekly["08-10"] / weekly["07-27"] < 0.85
    assert weekly["09-07"] / weekly["07-27"] > 0.95


def test_s5_exited_competitor_inactive(db):
    assert _all(
        db, "select is_active, is_new from marts.presence where company = 'ООО «СтройДвор Москва»'"
    ) == [(False, False)]


def test_market_share_is_normalized_and_estimated(db):
    sums = _all(
        db,
        "select month, region_code, product_group, sum(share) from marts.market_share "
        "group by 1, 2, 3",
    )
    assert sums
    assert all(abs(float(total) - 1) < 0.001 for *_, total in sums)
    assert _all(db, "select distinct provenance::text from marts.market_share") == [("estimate",)]


def test_new_competitor_takes_share_only_after_entry(db):
    shares = dict(
        _all(
            db,
            "select to_char(month, 'YYYY-MM'), share from marts.market_share "
            "where player = 'ООО «СибСтройОпт»' and product_group = 'Сухие смеси'",
        )
    )
    assert min(shares) == "2026-07"
    own = dict(
        _all(
            db,
            "select to_char(month, 'YYYY-MM'), share from marts.market_share "
            "where player_kind = 'own' and region_code = 'RU-NVS' "
            "and product_group = 'Сухие смеси'",
        )
    )
    assert own["2026-07"] < own["2026-06"]


def test_source_freshness(db):
    rows = {
        code: (reliability, status, records)
        for code, reliability, status, records in _all(
            db, "select code, reliability, status, records from marts.source_freshness"
        )
    }
    assert rows["1c"] == ("A", "актуален", 6168)
    assert rows["prepared_db"][1:] == ("актуален", 11753)
    # у подготовленной базы — сумма по источникам наблюдений
    children = sum(r for code, (_, _, r) in rows.items() if code.startswith("prepared_db:"))
    assert children == rows["prepared_db"][2]
    assert rows["prepared_db:4"][:2] == ("C", "по мере поступления")
