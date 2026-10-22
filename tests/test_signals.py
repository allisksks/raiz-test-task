from pathlib import Path

import pytest
from sqlalchemy import text

from market_map.integrations.onec import FileSource, load_onec
from market_map.integrations.prepared import load_prepared
from market_map.signals import Thresholds, detect_signals

SAMPLES = Path(__file__).parents[1] / "data" / "samples"


@pytest.fixture(scope="module")
def db(test_engine):
    # Сигналы пересчитываются загрузчиками сами
    load_onec(test_engine, FileSource(SAMPLES / "onec"))
    load_prepared(test_engine, f"sqlite:///{SAMPLES / 'prepared' / 'prepared.sqlite'}")
    with test_engine.connect() as conn:
        yield conn


def _signals(db, signal_type: str | None = None) -> list[dict]:
    sql = "select * from marts.signals"
    if signal_type:
        sql += " where signal_type = :t"
    return [dict(r._mapping) for r in db.execute(text(sql + " order by id"), {"t": signal_type})]


pytestmark = pytest.mark.db


def test_signals_only_where_scenarios_are(db):
    regions = {s["region_code"] for s in _signals(db)}
    assert regions == {"RU-NVS", "RU-KDA", "RU-SVE", "RU-TA", "RU-MOW", "RU-SAM"}


def test_s1_composite_signal(db):
    composite = [s for s in _signals(db, "composite") if s["region_code"] == "RU-NVS"]
    assert len(composite) == 6
    assert {s["severity"] for s in composite} == {"critical"}
    assert {s["competitor"] for s in composite} == {"ООО «СибСтройОпт»"}

    s = next(
        s for s in composite
        if s["client"] == "ООО «СибДистрибуция»" and s["product_group"] == "Сухие смеси"
    )  # fmt: skip
    assert -0.30 < s["details"]["price_gap"] < -0.15
    assert s["details"]["margin_change"] < -0.07
    assert s["title"].startswith(
        "В регионе Новосибирская область появился новый конкурент ООО «СибСтройОпт» "
        "с ценой на 21% ниже цены клиента ООО «СибДистрибуция» по «Сухие смеси». "
        "Одновременно маржа клиента снизилась на 8,3 п.п."
    )


def test_new_competitors_and_exit(db):
    new = {
        (s["region_code"], s["competitor"], str(s["event_date"]))
        for s in _signals(db, "new_competitor")
    }
    assert new == {
        ("RU-NVS", "ООО «СибСтройОпт»", "2026-07-06"),
        ("RU-SAM", "ООО «ГиперСтрой Федерал»", "2026-09-14"),
    }
    exits = [(s["competitor"], str(s["event_date"])) for s in _signals(db, "competitor_exit")]
    assert exits == [("ООО «СтройДвор Москва»", "2026-06-29")]


def test_competitor_price_changes(db):
    changes = {
        (s["competitor"], s["product_group"], str(s["event_date"]), float(s["value"]) > 0)
        for s in _signals(db, "competitor_price_change")
    }
    assert changes == {
        ("ООО «КазаньСтройТорг»", "Утеплители", "2026-08-03", False),  # S4 акция
        ("ООО «КазаньСтройТорг»", "Утеплители", "2026-08-31", True),  # конец акции
        ("ООО «ЮгСтройСнаб»", "Гидроизоляция", "2026-09-07", True),  # S2
    }


def test_s3_substitutes(db):
    substitutes = _signals(db, "new_substitute")
    assert {str(s["event_date"]) for s in substitutes} == {"2026-08-17"}
    assert {s["product"] for s in substitutes} == {
        "Гидроизоляция эластичная 2К ГидроСтоп 15 кг",
        "Гидроизоляция проникающая ГидроСтоп 25 кг",
    }
    assert all(float(s["value"]) < 0 for s in substitutes)


def test_margin_deviation_signals_are_episodes(db):
    deviations = _signals(db, "margin_plan_deviation")
    assert {s["region_code"] for s in deviations} == {"RU-NVS"}
    # одна запись на клиента и группу, хотя отклонение длится три месяца
    assert len({(s["client"], s["product_group"]) for s in deviations}) == len(deviations)


def test_market_share_signals_quarterly_and_estimated(db):
    shares = _signals(db, "market_share_change")
    assert {(s["region_code"], s["product_group"]) for s in shares} == {
        ("RU-NVS", "Сухие смеси"),
        ("RU-NVS", "Клеи и затирки"),
    }
    assert {s["provenance"] for s in shares} == {"estimate"}
    assert all("за квартал" in s["title"] for s in shares)


def test_no_assortment_exits_in_samples(db):
    assert _signals(db, "assortment_exit") == []


def test_assortment_exit_detected(db, test_engine):
    from market_map.integrations.prepared import load_prepared
    from market_map.marts import refresh_marts

    # конкурент перестал продавать позицию: последние наблюдения позиции пропали
    with test_engine.begin() as conn:
        product_id = conn.execute(
            text(
                "select p.id from core.product p join core.company c on c.id = p.company_id "
                "where c.name = 'ООО «СибирьСтрой»' order by p.id limit 1"
            )
        ).scalar()
        conn.execute(
            text(
                "delete from core.price_observation "
                "where product_id = :p and observed_at > '2026-09-14'"
            ),
            {"p": product_id},
        )
    refresh_marts(test_engine)
    detect_signals(test_engine)
    exits = _signals(db, "assortment_exit")
    assert len(exits) == 1
    assert exits[0]["competitor"] == "ООО «СибирьСтрой»"
    assert str(exits[0]["event_date"]) == "2026-09-21"
    # вернуть данные для остальных тестов
    load_prepared(test_engine, f"sqlite:///{SAMPLES / 'prepared' / 'prepared.sqlite'}")
    assert _signals(db, "assortment_exit") == []


def test_detection_is_idempotent_and_thresholds_apply(db, test_engine):
    total = len(_signals(db))
    detect_signals(test_engine)
    assert len(_signals(db)) == total

    counts = detect_signals(test_engine, Thresholds(price_gap=0.5))
    assert "composite" not in counts
    detect_signals(test_engine)
