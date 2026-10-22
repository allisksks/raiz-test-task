import random
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from market_map.integrations.onec import FileSource, load_onec
from market_map.integrations.prepared import load_prepared
from market_map.samples import generator

pytestmark = pytest.mark.db

SAMPLES = Path(__file__).parents[1] / "data" / "samples"
PREPARED_URL = f"sqlite:///{SAMPLES / 'prepared' / 'prepared.sqlite'}"


def _scalar(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


@pytest.fixture(scope="module")
def loaded(test_engine):
    load_onec(test_engine, FileSource(SAMPLES / "onec"))
    return load_prepared(test_engine, PREPARED_URL)


def test_full_load(loaded):
    assert loaded.stats == {
        "competitors": 15,
        "competitor_regions": 27,
        "offers": 234,
        "matched_identical": 155,
        "matched_similar": 79,
        "unmatched": 0,
        "prices": 11753,
        "price_duplicates": 0,
        "rejected": 0,
    }


def test_matching_agrees_with_generator(loaded, test_engine):
    rng = random.Random(generator.DEFAULT_SEED)
    generator.build_onec(rng)
    truth = {
        str(o.id): (o.own.sku, "identical" if o.identical else "similar")
        for o in generator._build_offers(rng)
    }
    with test_engine.connect() as conn:
        rows = conn.execute(
            text(
                "select cp.source_key, op.sku, pm.match_type from core.product_match pm "
                "join core.product cp on cp.id = pm.competitor_product_id "
                "join core.product op on op.id = pm.own_product_id"
            )
        ).all()
    assert {key: (sku, match_type) for key, sku, match_type in rows} == truth


def test_s1_new_competitor_in_core(loaded, test_engine):
    with test_engine.connect() as conn:
        row = conn.execute(
            text(
                "select c.registered_at, c.has_regional_warehouse, c.channel_code, "
                "cr.region_code, cr.valid_from, min(po.observed_at) "
                "from core.company c join core.company_region cr on cr.company_id = c.id "
                "join core.price_observation po on po.company_id = c.id "
                "where c.name = 'ООО «СибСтройОпт»' group by 1, 2, 3, 4, 5"
            )
        ).one()
    assert [str(v) for v in row] == [
        "2026-05-14", "True", "distribution", "RU-NVS", "2026-07-06", "2026-07-06"
    ]  # fmt: skip


def test_s5_competitor_exit_closes_presence(loaded, test_engine):
    assert (
        str(
            _scalar(
                test_engine,
                "select cr.valid_to from core.company_region cr join core.company c "
                "on c.id = cr.company_id where c.name = 'ООО «СтройДвор Москва»'",
            )
        )
        == "2026-06-29"
    )


def test_observation_sources_keep_reliability(loaded, test_engine):
    assert (
        _scalar(
            test_engine,
            "select count(*) from core.price_observation po join core.data_source s "
            "on s.id = po.source_id where s.reliability = 'C'",
        )
        == 343
    )


def test_reload_is_idempotent_and_keeps_manual_matches(loaded, test_engine):
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "update core.product_match set method = 'manual', match_type = 'similar' "
                "where competitor_product_id = (select min(competitor_product_id) "
                "from core.product_match where method = 'sku')"
            )
        )
    load_prepared(test_engine, PREPARED_URL)
    assert _scalar(test_engine, "select count(*) from core.price_observation") == 11753
    assert _scalar(test_engine, "select count(*) from core.company where kind = 'competitor'") == 15
    assert _scalar(test_engine, "select count(*) from core.product_match") == 234
    assert (
        _scalar(test_engine, "select count(*) from core.product_match where method = 'manual'") == 1
    )


def test_bad_rows_are_rejected(loaded, test_engine, tmp_path):
    broken = tmp_path / "prepared.sqlite"
    shutil.copy(SAMPLES / "prepared" / "prepared.sqlite", broken)
    with sqlite3.connect(broken) as conn:
        conn.execute("update prices set price = 'по запросу' where id = 1")
        conn.execute("update prices set region = 'Атлантида' where id = 2")
    conn.close()

    result = load_prepared(test_engine, f"sqlite:///{broken}")
    reasons = sorted(reason for _, reason, _ in result.rejected)
    assert reasons == ["не распознан регион: 'Атлантида'", "не распознана цена: 'по запросу'"]
    assert result.stats["prices"] == 11751
