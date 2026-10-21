import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from market_map.reference import REGIONS
from market_map.samples import generate


def _price(text: str) -> float:
    return float(text.replace("₽", "").replace("руб.", "").replace(" ", "").replace(",", "."))


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    generate(out)
    return out


@pytest.fixture(scope="module")
def prepared(samples):
    conn = sqlite3.connect(samples / "prepared" / "prepared.sqlite")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def onec(samples):
    return {
        f.stem: json.loads(f.read_text(encoding="utf-8"))["value"]
        for f in (samples / "onec").glob("*.json")
    }


def test_generation_is_deterministic(samples, tmp_path):
    generate(tmp_path)
    for f in (samples / "onec").glob("*.json"):
        assert f.read_bytes() == (tmp_path / "onec" / f.name).read_bytes()
    dump = [
        list(sqlite3.connect(d / "prepared" / "prepared.sqlite").iterdump())
        for d in (samples, tmp_path)
    ]
    assert dump[0] == dump[1]


def test_committed_samples_are_up_to_date(samples):
    committed = Path(__file__).parents[1] / "data" / "samples"
    for f in (samples / "onec").glob("*.json"):
        assert f.read_bytes() == (committed / "onec" / f.name).read_bytes(), (
            f"{f.name} устарел — выполните `make samples`"
        )
    dump = [
        list(sqlite3.connect(d / "prepared" / "prepared.sqlite").iterdump())
        for d in (samples, committed)
    ]
    assert dump[0] == dump[1], "prepared.sqlite устарел — выполните `make samples`"


def test_region_texts_are_known_variants(prepared):
    known = {a for r in REGIONS for a in (r.name, *r.aliases)}
    texts = {row[0] for row in prepared.execute("select region from prices")}
    texts |= {row[0] for row in prepared.execute("select region from competitor_regions")}
    assert texts <= known


def test_onec_references_are_consistent(onec):
    clients = {c["Ref_Key"] for c in onec["Catalog_Контрагенты"]}
    items = {n["Ref_Key"] for n in onec["Catalog_Номенклатура"] if not n["IsFolder"]}
    for row in onec["AccumulationRegister_Продажи_Turnovers"]:
        assert row["Контрагент_Key"] in clients
        assert row["Номенклатура_Key"] in items
        assert row["КоличествоTurnover"] > 0


def _nvs_mix_margin(onec) -> dict[str, tuple[float, float]]:
    """Факт и план маржи (%) по сухим смесям у дистрибьюторов Новосибирска по месяцам."""
    nom = {n["Ref_Key"]: n for n in onec["Catalog_Номенклатура"]}
    clients = {
        c["Ref_Key"]
        for c in onec["Catalog_Контрагенты"]
        if c["Регион"] == "Новосибирская область" and c["КаналСбыта"] == "Дистрибуция"
    }
    group = next(n["Ref_Key"] for n in nom.values() if n["Description"] == "Сухие смеси")
    fact = defaultdict(lambda: [0.0, 0.0])
    for r in onec["AccumulationRegister_Продажи_Turnovers"]:
        if r["Контрагент_Key"] in clients and nom[r["Номенклатура_Key"]]["Parent_Key"] == group:
            acc = fact[r["Period"][:7]]
            acc[0] += r["СтоимостьTurnover"]
            acc[1] += r["СебестоимостьTurnover"]
    plan = {
        r["Period"][:7]: r["ПлановаяМаржаПроцент"]
        for r in onec["InformationRegister_ПланМаржи"]
        if r["Контрагент_Key"] in clients and r["ГруппаНоменклатуры_Key"] == group
    }
    return {m: ((rev - cost) / rev * 100, plan[m]) for m, (rev, cost) in fact.items()}


def test_s1_new_competitor_25_percent_cheaper(prepared, onec):
    first_seen = prepared.execute(
        "select min(observed_at) from prices p join offers o on o.id = p.offer_id "
        "where o.competitor_id = 'C-13'"
    ).fetchone()[0]
    assert first_seen == "2026-07-06"

    # Цена конкурента на ПрофМикс PM-101 против нашей цены дистрибьюторам Новосибирска в июне
    competitor = [
        _price(p)
        for (p,) in prepared.execute(
            "select price from prices p join offers o on o.id = p.offer_id "
            "where o.competitor_id = 'C-13' and o.title like '%Штукатурка гипсовая ПрофМикс%' "
            "and p.observed_at = '2026-07-06'"
        )
    ]
    assert competitor
    nom = {n["Ref_Key"]: n for n in onec["Catalog_Номенклатура"]}
    nvs = {
        c["Ref_Key"]
        for c in onec["Catalog_Контрагенты"]
        if c["Регион"] == "Новосибирская область" and c["КаналСбыта"] == "Дистрибуция"
    }
    rows = [
        r
        for r in onec["AccumulationRegister_Продажи_Turnovers"]
        if r["Контрагент_Key"] in nvs
        and nom[r["Номенклатура_Key"]]["Артикул"] == "PM-101"
        and r["Period"].startswith("2026-06")
    ]
    own = sum(r["СтоимостьTurnover"] for r in rows) / sum(r["КоличествоTurnover"] for r in rows)
    gap = 1 - min(competitor) / own
    assert 0.20 < gap < 0.30


def test_s1_margin_falls_below_plan(onec):
    margin = _nvs_mix_margin(onec)
    fact_jun, plan_jun = margin["2026-06"]
    fact_sep, plan_sep = margin["2026-09"]
    assert abs(fact_jun - plan_jun) < 2
    assert plan_sep - fact_sep > 5


def test_s3_substitutes_appear(prepared):
    rows = prepared.execute(
        "select o.sku, min(p.observed_at) from offers o join prices p on p.offer_id = o.id "
        "where o.competitor_id = 'C-14' and o.brand = 'АкваБарьер' group by o.sku"
    ).fetchall()
    assert dict(rows) == {"АК-302": "2026-08-17", "АК-305": "2026-08-17"}


def test_s5_competitor_exits(prepared):
    last = prepared.execute(
        "select max(observed_at) from prices p join offers o on o.id = p.offer_id "
        "where o.competitor_id = 'C-11'"
    ).fetchone()[0]
    assert date.fromisoformat(last) <= date(2026, 6, 29)


def test_s6_enters_samara(prepared):
    first, last = prepared.execute(
        "select first_seen, last_seen from competitor_regions "
        "where competitor_id = 'C-15' and region like '%Самар%'"
    ).fetchone()
    assert (first, last) == ("2026-09-14", None)
