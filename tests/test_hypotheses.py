from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from market_map.hypotheses import Context, build_hypotheses
from market_map.integrations.onec import FileSource, load_onec
from market_map.integrations.prepared import load_prepared

SAMPLES = Path(__file__).parents[1] / "data" / "samples"


def _context(signal_type="new_competitor", prices=(), reversed_within=None, **competitor):
    return Context(
        signal={"signal_type": signal_type, "event_date": date(2026, 7, 6), "value": -0.2},
        competitor={
            "supply_scheme": None,
            "registered_at": None,
            "entry_date": date(2026, 7, 6),
            "has_regional_warehouse": None,
            "channel_code": "retail",
            "regions": 1,
            "assortment": 5,
            **competitor,
        },
        prices=list(prices),
        regional_premium=None,
        reversed_within=reversed_within,
        days_observed_after=100,
        as_of=date(2026, 10, 19),
    )


def _price(competitor, own, cost, match_type="identical"):
    return {
        "match_type": match_type,
        "competitor_price": competitor,
        "own_price": own,
        "own_cost": cost,
        "gap": competitor / own - 1,
        "to_cost": competitor / cost - 1,
        "markup": own / cost - 1,
        "implied_cost_gap": competitor / (own / cost) / cost - 1,
    }


def _by_type(hypotheses):
    return {h.hypothesis_type: h for h in hypotheses}


def test_no_evidence_means_insufficient_data():
    hypotheses = build_hypotheses(_context(signal_type="competitor_price_change"))
    assert [h.hypothesis_type for h in hypotheses] == ["insufficient_data"]


def test_price_below_our_cost_supports_direct_purchase():
    ctx = _context(prices=[_price(80, 110, 100)], supply_scheme="прямые поставки с завода")
    direct = _by_type(build_hypotheses(ctx))["direct_purchase"]
    assert direct.score >= 0.85
    assert "ниже нашей закупочной на 20%" in direct.rationale


def test_analog_price_is_not_compared_with_our_cost_for_direct_purchase():
    ctx = _context(prices=[_price(80, 110, 100, "similar")])
    hypotheses = _by_type(build_hypotheses(ctx))
    assert "direct_purchase" not in hypotheses
    assert (
        "аналог дешевле нашей закупочной цены оригинала на 20%"
        in hypotheses["analog_cost"].rationale
    )


def test_promo_explains_low_price_instead_of_purchase_terms():
    ctx = _context(
        signal_type="competitor_price_change", prices=[_price(80, 110, 100)], reversed_within=28
    )
    hypotheses = build_hypotheses(ctx)
    assert hypotheses[0].hypothesis_type == "temporary_promo"
    # цена ниже закупочной в акцию не довод за прямые закупки — остаётся только слабый довод
    assert _by_type(hypotheses)["direct_purchase"].score < 0.35


@pytest.fixture(scope="module")
def db(test_engine):
    load_onec(test_engine, FileSource(SAMPLES / "onec"))
    load_prepared(test_engine, f"sqlite:///{SAMPLES / 'prepared' / 'prepared.sqlite'}")
    with test_engine.connect() as conn:
        yield conn


def _top(db, signal_name: str, competitor: str) -> dict[str, float]:
    rows = db.execute(
        text(
            "select hypothesis_type, score from marts.hypotheses "
            "where signal_name = :s and competitor = :c"
        ),
        {"s": signal_name, "c": competitor},
    ).all()
    return {t: float(score) for t, score in rows}


@pytest.mark.db
def test_every_price_signal_is_explained(db):
    unexplained = db.execute(
        text(
            "select s.id from core.signal s where s.signal_type in ('composite', "
            "'new_competitor', 'new_substitute') and not exists "
            "(select 1 from core.hypothesis h where h.signal_id = s.id)"
        )
    ).all()
    assert unexplained == []
    assert db.execute(text("select count(distinct provenance) from marts.hypotheses")).scalar() == 1


@pytest.mark.db
def test_s1_direct_purchase_short_chain_aggressive_entry(db):
    scores = _top(db, "Новый конкурент", "ООО «СибСтройОпт»")
    for hypothesis in ("direct_purchase", "short_chain", "aggressive_entry"):
        assert scores[hypothesis] >= 0.8, hypothesis
    assert scores["regional_warehouse"] >= 0.4


@pytest.mark.db
def test_s6_federal_chain_volume_purchase_first(db):
    scores = _top(db, "Новый конкурент", "ООО «ГиперСтрой Федерал»")
    assert max(scores, key=scores.get) == "volume_purchase"


@pytest.mark.db
def test_s4_promo_first(db):
    scores = _top(db, "Изменение цены конкурента", "ООО «КазаньСтройТорг»")
    assert max(scores, key=scores.get) == "temporary_promo"
    assert scores.get("direct_purchase", 0) < 0.35


@pytest.mark.db
def test_s3_cheap_analog_first(db):
    scores = _top(db, "Новый товар-заменитель", "ООО «УралСтройМаркет»")
    assert max(scores, key=scores.get) == "analog_cost"


@pytest.mark.db
def test_price_breakdown_separates_facts_and_estimates(db):
    rows = db.execute(
        text(
            "select component, value, provenance from marts.price_breakdown "
            "where signal_name = 'Новый конкурент' and competitor = 'ООО «СибСтройОпт»' "
            "order by step"
        )
    ).all()
    breakdown = {component: (float(value), provenance) for component, value, provenance in rows}
    assert breakdown["Цена конкурента к нашей закупочной"][0] < -0.1
    assert {p for _, p in breakdown.values()} == {"fact", "estimate"}
    assert breakdown["Закупочная цена конкурента при нашей наценке"][1] == "estimate"
