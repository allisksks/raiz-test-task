"""Генератор тестовых данных.

Выход:
- `<out>/onec/*.json` — выгрузка в формате OData 1С (справочники, обороты продаж, план маржи);
- `<out>/prepared/prepared.sqlite` — подготовленная база цен и конкурентов в «сыром» виде:
  регионы и цены записаны по-разному, артикул есть не всегда — как после парсинга.
"""

import json
import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from market_map.reference import CHANNELS_BY_CODE, REGIONS_BY_CODE
from market_map.samples.catalog import (
    ANALOG_BRANDS,
    CHANNEL_PRICE_FACTOR,
    CHANNEL_VOLUME,
    CLIENTS,
    COMPETITORS,
    GROUPS,
    PRODUCTS,
    PRODUCTS_BY_SKU,
    REGION_PRICE_FACTOR,
    Competitor,
    Product,
)
from market_map.samples.scenarios import SCENARIOS, Scenario

DEFAULT_SEED = 20261021

# Продажи из 1С: 12 полных месяцев
SALES_MONTHS = [
    date(2025 + (9 + i) // 12, (9 + i) % 12 + 1, 1) for i in range(12)
]  # 10.2025–09.2026
# Мониторинг цен: еженедельно по понедельникам
PRICE_WEEKS = [date(2026, 4, 6) + timedelta(weeks=i) for i in range(29)]

SEASONALITY = {
    1: 0.55, 2: 0.65, 3: 0.85, 4: 1.00, 5: 1.15, 6: 1.20,
    7: 1.20, 8: 1.15, 9: 1.05, 10: 0.95, 11: 0.80, 12: 0.65,
}  # fmt: skip
PRICE_INFLATION_MONTHLY = 0.004
COST_INFLATION_MONTHLY = 0.005
PLAN_GROWTH = 1.05

ONEC_NAMESPACE = uuid.UUID("6f1d3c4e-2b7a-4c1e-9d55-1c0000000001")
ODATA_BASE = "http://1c.example/trade/odata/standard.odata/$metadata"

SOURCES = (
    (1, "Сайты конкурентов (парсинг)", "site", "B"),
    (2, "Прайс-листы конкурентов (рассылка)", "price_list", "B"),
    (3, "Маркетплейсы (витрина)", "marketplace", "B"),
    (4, "Полевой мониторинг торговых представителей", "field", "C"),
)
SOURCE_BY_CHANNEL = {
    "marketplace": 3,
    "diy": 1,
    "retail": 1,
    "wholesale": 2,
    "distribution": 2,
    "construction": 2,
}


def _ref_key(*parts: str) -> str:
    return str(uuid.uuid5(ONEC_NAMESPACE, "/".join(parts)))


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _months_since_start(d: date) -> int:
    start = SALES_MONTHS[0]
    return (d.year - start.year) * 12 + d.month - start.month


def _volume_weight(product: Product) -> float:
    """Дешёвые товары продаются в больших количествах."""
    return (500 / product.price) ** 0.7


def _sales_multipliers(region: str, channel: str, group: str, month: date) -> tuple[float, float]:
    price_mult = qty_mult = 1.0
    for scenario in SCENARIOS:
        for effect in scenario.sales_effects:
            if effect.region != region or group not in effect.groups:
                continue
            if effect.channels is not None and channel not in effect.channels:
                continue
            p, q = effect.by_month.get(_month_key(month), (1.0, 1.0))
            price_mult *= p
            qty_mult *= q
    return price_mult, qty_mult


# --- 1С ---------------------------------------------------------------------------------


def _odata(entity: str, rows: list[dict]) -> dict:
    return {"odata.metadata": f"{ODATA_BASE}#{entity}", "value": rows}


def build_onec(rng: random.Random) -> dict[str, dict]:
    """Возвращает {имя файла: содержимое} для выгрузки 1С."""
    contractors = [
        {
            "Ref_Key": _ref_key("client", c.key),
            "DeletionMark": False,
            "Code": c.key,
            "Description": c.name,
            "ИНН": c.inn,
            "КаналСбыта": CHANNELS_BY_CODE[c.channel].name,
            "Регион": REGIONS_BY_CODE[c.region].name,
        }
        for c in CLIENTS
    ]

    nomenclature = [
        {
            "Ref_Key": _ref_key("group", g),
            "DeletionMark": False,
            "IsFolder": True,
            "Parent_Key": "00000000-0000-0000-0000-000000000000",
            "Code": f"Г-{i:02d}",
            "Description": g,
        }
        for i, g in enumerate(GROUPS, start=1)
    ] + [
        {
            "Ref_Key": _ref_key("product", p.sku),
            "DeletionMark": False,
            "IsFolder": False,
            "Parent_Key": _ref_key("group", p.group),
            "Code": p.sku,
            "Description": p.name,
            "Артикул": p.sku,
            "Бренд": p.brand,
            "ЕдиницаИзмерения": p.unit,
        }
        for p in PRODUCTS
    ]

    turnovers = []
    plan: dict[tuple[str, str, str], list[float]] = {}  # (клиент, группа, месяц) → [выручка, маржа]
    for client in CLIENTS:
        share = 0.85 if client.channel in ("distribution", "wholesale") else 0.55
        assortment = [p for p in PRODUCTS if rng.random() < share]
        client_discount = rng.uniform(0.98, 1.02)
        size = CHANNEL_VOLUME[client.channel] * rng.uniform(0.7, 1.3)
        for month in SALES_MONTHS:
            n = _months_since_start(month)
            for p in assortment:
                base_price = (
                    p.price
                    * CHANNEL_PRICE_FACTOR[client.channel]
                    * REGION_PRICE_FACTOR[client.region]
                    * client_discount
                    * (1 + PRICE_INFLATION_MONTHLY) ** n
                )
                unit_cost = p.cost * (1 + COST_INFLATION_MONTHLY) ** n
                base_qty = size * _volume_weight(p) * SEASONALITY[month.month] / 10

                key = (client.key, p.group, _month_key(month))
                acc = plan.setdefault(key, [0.0, 0.0])
                acc[0] += base_qty * base_price
                acc[1] += base_qty * (base_price - unit_cost)

                price_mult, qty_mult = _sales_multipliers(
                    client.region, client.channel, p.group, month
                )
                qty = max(1, round(base_qty * qty_mult * rng.uniform(0.85, 1.15)))
                price = base_price * price_mult
                turnovers.append(
                    {
                        "Period": f"{month.isoformat()}T00:00:00",
                        "Контрагент_Key": _ref_key("client", client.key),
                        "Номенклатура_Key": _ref_key("product", p.sku),
                        "Регион": REGIONS_BY_CODE[client.region].name,
                        "КоличествоTurnover": qty,
                        "СтоимостьTurnover": round(qty * price, 2),
                        "СебестоимостьTurnover": round(qty * unit_cost, 2),
                    }
                )

    margin_plan = [
        {
            "Period": f"{month}-01T00:00:00",
            "Контрагент_Key": _ref_key("client", client_key),
            "ГруппаНоменклатуры_Key": _ref_key("group", group),
            "ПлановаяВыручка": round(revenue * PLAN_GROWTH, 2),
            "ПлановаяМаржаПроцент": round(margin / revenue * 100 + 0.5, 2),
        }
        for (client_key, group, month), (revenue, margin) in sorted(plan.items())
    ]

    return {
        "Catalog_Контрагенты.json": _odata("Catalog_Контрагенты", contractors),
        "Catalog_Номенклатура.json": _odata("Catalog_Номенклатура", nomenclature),
        "AccumulationRegister_Продажи_Turnovers.json": _odata(
            "AccumulationRegister_Продажи_Turnovers", turnovers
        ),
        "InformationRegister_ПланМаржи.json": _odata("InformationRegister_ПланМаржи", margin_plan),
    }


# --- Подготовленная база ----------------------------------------------------------------


@dataclass
class Offer:
    id: int
    competitor: Competitor
    own: Product  # свой товар, которому соответствует предложение
    identical: bool
    sku: str | None
    title: str
    brand: str
    level: float  # уровень цены предложения к своей цене
    appears_at: date | None = None


def _scenarios_for(competitor: Competitor) -> list[Scenario]:
    return [s for s in SCENARIOS if s.competitor == competitor.key]


def _analog_sku(product: Product, brand: str) -> str:
    return f"{brand[:2].upper()}-{product.sku.split('-')[1]}"


def _messy_title(rng: random.Random, title: str, sku: str | None) -> str:
    variants = [title, title.upper(), title.replace(" кг", "кг")]
    if sku:
        variants += [f"{title} (арт. {sku})", f"{sku} {title}"]
    return rng.choice(variants)


def _build_offers(rng: random.Random) -> list[Offer]:
    offers: list[Offer] = []
    for comp in COMPETITORS:
        for p in PRODUCTS:
            if p.group not in comp.groups or rng.random() > 0.75:
                continue
            identical = comp.identical and rng.random() < 0.8
            if identical:
                brand, level, sku = p.brand, comp.price_level, p.sku
                title = p.name
            else:
                brand, analog_level = ANALOG_BRANDS[p.group]
                level = comp.price_level * analog_level
                sku = _analog_sku(p, brand)
                title = p.name.replace(p.brand, brand)
            offers.append(
                Offer(
                    id=len(offers) + 1,
                    competitor=comp,
                    own=p,
                    identical=identical,
                    # Артикул заполнен не везде — иногда он только в названии
                    sku=sku if rng.random() < 0.7 else None,
                    title=_messy_title(rng, title, sku),
                    brand=brand,
                    level=level * rng.uniform(0.97, 1.03),
                )
            )
        for scenario in _scenarios_for(comp):
            for own_sku, appears_at, level in scenario.substitutes:
                p = PRODUCTS_BY_SKU[own_sku]
                brand, _ = ANALOG_BRANDS[p.group]
                sku = _analog_sku(p, brand)
                offers.append(
                    Offer(
                        id=len(offers) + 1,
                        competitor=comp,
                        own=p,
                        identical=False,
                        sku=sku,
                        title=p.name.replace(p.brand, brand),
                        brand=brand,
                        level=level,
                        appears_at=appears_at,
                    )
                )
    return offers


def _region_text(rng: random.Random, code: str) -> str:
    region = REGIONS_BY_CODE[code]
    return rng.choice((region.name, region.name) + region.aliases)


def _price_text(rng: random.Random, price: float) -> str:
    whole, kop = divmod(round(price * 100), 100)
    grouped = f"{whole:,}".replace(",", " ")
    return rng.choice(
        (
            f"{whole}.{kop:02d}",
            f"{grouped},{kop:02d} ₽",
            f"{grouped} руб.",
            f"{whole},{kop:02d}",
        )
    )


def _presence(comp: Competitor, region: str) -> tuple[date, date | None]:
    """Период присутствия конкурента в регионе в пределах мониторинга."""
    first, last = PRICE_WEEKS[0], None
    for s in _scenarios_for(comp):
        if s.region == region:
            first = s.enters_at or first
            last = s.exits_at or last
    return first, last


def _scenario_price_mult(offer: Offer, region: str, week: date) -> float:
    mult = 1.0
    for s in _scenarios_for(offer.competitor):
        if s.region != region:
            continue
        if s.region_price_level is not None:
            mult *= s.region_price_level / offer.competitor.price_level
        if s.price_change is None or week < s.price_change_from:
            continue
        if s.price_change_to is not None and week > s.price_change_to:
            continue
        if s.price_change_groups is None or offer.own.group in s.price_change_groups:
            mult *= s.price_change
    return mult


def build_prepared(rng: random.Random) -> dict[str, list[tuple]]:
    """Возвращает строки таблиц подготовленной базы."""
    offers = _build_offers(rng)

    competitors = [
        (
            c.key,
            c.name,
            c.inn,
            c.channel,
            c.website,
            c.registered_at,
            None if c.has_regional_warehouse is None else int(c.has_regional_warehouse),
            c.supply_scheme,
        )
        for c in COMPETITORS
    ]

    competitor_regions = []
    for c in COMPETITORS:
        for region in c.regions:
            first, last = _presence(c, region)
            competitor_regions.append(
                (c.key, _region_text(rng, region), first.isoformat(), last and last.isoformat())
            )

    offer_rows = [
        (o.id, o.competitor.key, o.sku, o.title, o.brand, o.own.unit, o.own.group) for o in offers
    ]

    prices = []
    for o in offers:
        comp = o.competitor
        source_id = SOURCE_BY_CHANNEL[comp.channel_code]
        for region in comp.regions:
            first, last = _presence(comp, region)
            base = (
                o.own.price
                * CHANNEL_PRICE_FACTOR[comp.channel_code]
                * REGION_PRICE_FACTOR[region]
                * o.level
            )
            step = 1.0  # накопленные ступенчатые изменения цены
            for week in PRICE_WEEKS:
                if week < first or (last and week > last):
                    continue
                if o.appears_at and week < o.appears_at:
                    continue
                if rng.random() < 0.04:
                    step *= 1 + rng.choice((-1, 1)) * rng.uniform(0.02, 0.06)
                if rng.random() < 0.08:  # пропуск наблюдения: страница недоступна и т. п.
                    continue
                months = (week - SALES_MONTHS[0]).days / 30.4
                price = (
                    base
                    * step
                    * (1 + PRICE_INFLATION_MONTHLY) ** months
                    * _scenario_price_mult(o, region, week)
                )
                url = f"https://{comp.website}/catalog/{o.sku or o.id}"
                prices.append(
                    (o.id, _region_text(rng, region), _price_text(rng, price),
                     week.isoformat(), source_id, url)
                )  # fmt: skip
                # Иногда то же наблюдение приходит и от торговых представителей
                if rng.random() < 0.03:
                    prices.append(
                        (o.id, _region_text(rng, region),
                         _price_text(rng, price * rng.uniform(0.98, 1.02)),
                         week.isoformat(), 4, None)
                    )  # fmt: skip

    return {
        "sources": [(i, n, k, None, r) for i, n, k, r in SOURCES],
        "competitors": competitors,
        "competitor_regions": competitor_regions,
        "offers": offer_rows,
        "prices": [(i, *row) for i, row in enumerate(prices, start=1)],
    }


PREPARED_DDL = """
create table sources (
    id integer primary key, name text not null, kind text not null, url text,
    reliability text not null
);
create table competitors (
    id text primary key, name text not null, inn text, channel text, website text,
    registered_at text, has_regional_warehouse integer, supply_scheme text
);
create table competitor_regions (
    competitor_id text not null references competitors, region text not null,
    first_seen text not null, last_seen text
);
create table offers (
    id integer primary key, competitor_id text not null references competitors, sku text,
    title text not null, brand text, unit text, category text
);
create table prices (
    id integer primary key, offer_id integer not null references offers, region text not null,
    price text not null, observed_at text not null,
    source_id integer not null references sources, url text
);
"""


def _write_sqlite(path: Path, tables: dict[str, list[tuple]]) -> None:
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(PREPARED_DDL)
        for table, rows in tables.items():
            if rows:
                marks = ", ".join("?" * len(rows[0]))
                conn.executemany(f"insert into {table} values ({marks})", rows)
    conn.close()


def generate(out_dir: Path, seed: int = DEFAULT_SEED) -> dict[str, int]:
    """Генерирует тестовые данные в out_dir, возвращает число записей по наборам."""
    rng = random.Random(seed)
    onec = build_onec(rng)
    prepared = build_prepared(rng)

    onec_dir = out_dir / "onec"
    onec_dir.mkdir(parents=True, exist_ok=True)
    for name, content in onec.items():
        (onec_dir / name).write_text(
            json.dumps(content, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )

    prepared_dir = out_dir / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    _write_sqlite(prepared_dir / "prepared.sqlite", prepared)

    stats = {f"onec/{name}": len(content["value"]) for name, content in onec.items()}
    stats.update({f"prepared/{table}": len(rows) for table, rows in prepared.items()})
    return stats
