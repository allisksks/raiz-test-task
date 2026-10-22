"""Загрузка подготовленной базы конкурентов: источники, конкуренты, присутствие в регионах,
ассортимент с сопоставлением своей номенклатуре, история цен.

Подготовленная база читается через SQLAlchemy — это может быть SQLite-файл (тестовые
данные), PostgreSQL или любая другая СУБД с той же структурой таблиц (docs/test-data.md).
Загрузка идемпотентна: повторная загрузка тех же данных ничего не дублирует.
"""

from datetime import date

from sqlalchemy import Connection, Engine, create_engine, text

from market_map.loads import (
    LoadResult,
    exclusive_load,
    finish_run,
    product_group_ids,
    source_ids,
    start_run,
)
from market_map.marts import refresh_derived
from market_map.matching import Matcher, OwnProduct, clean_title, sku_from_title
from market_map.normalize import channel_code, parse_price, region_code

SOURCE_CODE = "prepared_db"
REFRESH_BY_KIND = {"site": "weekly", "price_list": "weekly", "marketplace": "daily"}


def _rows(source: Engine, table: str) -> list[dict]:
    with source.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(f"select * from {table}"))]


def _ensure_sources(conn: Connection, sources: list[dict]) -> tuple[int, dict[int, int]]:
    """Источник «подготовленная база» и источники наблюдений; возвращает их id в core."""
    master_id = conn.execute(
        text(
            "insert into core.data_source "
            "(code, name, kind, reliability, auto_fetch, refresh_period, restrictions) "
            "values (:c, 'Подготовленная база цен и конкурентов', 'prepared_db', 'B', true, "
            "'weekly', 'Данные парсинга открытых источников') "
            "on conflict (code) do update set name = excluded.name returning id"
        ),
        {"c": SOURCE_CODE},
    ).scalar_one()
    ids = {}
    for s in sources:
        ids[s["id"]] = conn.execute(
            text(
                "insert into core.data_source (code, name, kind, reliability, auto_fetch, "
                "refresh_period) values (:c, :n, :k, :r, :auto, :p) "
                "on conflict (code) do update set name = excluded.name, kind = excluded.kind, "
                "reliability = excluded.reliability returning id"
            ),
            {
                "c": f"{SOURCE_CODE}:{s['id']}",
                "n": s["name"],
                "k": s["kind"],
                "r": s["reliability"],
                "auto": s["kind"] != "field",
                "p": REFRESH_BY_KIND.get(s["kind"], "on_demand"),
            },
        ).scalar_one()
    return master_id, ids


def _load_competitors(conn: Connection, source_id: int, rows: list[dict], result: LoadResult):
    competitors = []
    for r in rows:
        channel = channel_code(r["channel"])
        if channel is None and r["channel"]:
            result.reject("competitors", f"не распознан канал: {r['channel']!r}", r)
        warehouse = r["has_regional_warehouse"]
        competitors.append(
            {
                "n": r["name"],
                "inn": r["inn"] or None,
                "c": channel,
                "reg": date.fromisoformat(r["registered_at"]) if r["registered_at"] else None,
                "w": None if warehouse is None else bool(warehouse),
                "sup": r["supply_scheme"],
                "s": source_id,
                "k": str(r["id"]),
            }
        )
    if competitors:
        conn.execute(
            text(
                "insert into core.company (kind, name, inn, channel_code, registered_at, "
                "has_regional_warehouse, supply_scheme, source_id, source_key) "
                "values ('competitor', :n, :inn, :c, :reg, :w, :sup, :s, :k) "
                "on conflict (source_id, source_key) do update set name = excluded.name, "
                "inn = excluded.inn, channel_code = excluded.channel_code, "
                "registered_at = excluded.registered_at, "
                "has_regional_warehouse = excluded.has_regional_warehouse, "
                "supply_scheme = excluded.supply_scheme"
            ),
            competitors,
        )
    result.stats["competitors"] = len(competitors)


def _load_presence(conn: Connection, source_id: int, rows: list[dict], result: LoadResult):
    company_ids = source_ids(conn, "company", source_id)
    presence = []
    for r in rows:
        region = region_code(r["region"])
        company = company_ids.get(str(r["competitor_id"]))
        if region is None or company is None:
            result.reject("competitor_regions", "неизвестный регион или конкурент", r)
            continue
        presence.append(
            {
                "c": company,
                "r": region,
                "f": date.fromisoformat(r["first_seen"]),
                "t": date.fromisoformat(r["last_seen"]) if r["last_seen"] else None,
            }
        )
    if presence:
        conn.execute(
            text(
                "insert into core.company_region (company_id, region_code, valid_from, valid_to) "
                "values (:c, :r, :f, :t) on conflict (company_id, region_code, valid_from) "
                "do update set valid_to = excluded.valid_to"
            ),
            presence,
        )
    result.stats["competitor_regions"] = len(presence)


def _own_products(conn: Connection) -> list[OwnProduct]:
    rows = conn.execute(
        text("select id, sku, name, brand, group_id from core.product where company_id is null")
    )
    return [OwnProduct(*r) for r in rows]


def _load_offers(
    conn: Connection, source_id: int, rows: list[dict], result: LoadResult
) -> dict[str, tuple[int, int]]:
    """Загружает предложения и сопоставления; возвращает {id предложения: (товар, конкурент)}."""
    company_ids = source_ids(conn, "company", source_id)
    group_ids = product_group_ids(conn, by="name")
    products = []
    for r in rows:
        company = company_ids.get(str(r["competitor_id"]))
        if company is None:
            result.reject("offers", "неизвестный конкурент", r)
            continue
        group = group_ids.get(r["category"])
        if group is None:
            result.reject("offers", f"неизвестная категория: {r['category']!r}", r)
            continue
        products.append(
            {
                "g": group,
                "c": company,
                "sku": r["sku"] or sku_from_title(r["title"]),
                "n": clean_title(r["title"]),
                "b": r["brand"],
                "u": r["unit"],
                "s": source_id,
                "k": str(r["id"]),
            }
        )
    if products:
        conn.execute(
            text(
                "insert into core.product "
                "(group_id, company_id, sku, name, brand, unit, source_id, source_key) "
                "values (:g, :c, :sku, :n, :b, :u, :s, :k) "
                "on conflict (source_id, source_key) do update set group_id = excluded.group_id, "
                "company_id = excluded.company_id, sku = excluded.sku, name = excluded.name, "
                "brand = excluded.brand, unit = excluded.unit"
            ),
            products,
        )

    product_ids = source_ids(conn, "product", source_id)
    matcher = Matcher(_own_products(conn), brands={p["b"] for p in products if p["b"]})
    matches, unmatched = [], 0
    for p in products:
        m = matcher.match(p["sku"], p["n"], p["b"], p["g"])
        if m is None:
            unmatched += 1
            continue
        matches.append(
            {
                "cp": product_ids[p["k"]],
                "op": m.own_product_id,
                "t": m.match_type,
                "m": m.method,
                "conf": m.confidence,
            }
        )
    # Ручные сопоставления (method = manual) не трогаем
    conn.execute(
        text(
            "delete from core.product_match pm using core.product p "
            "where p.id = pm.competitor_product_id and p.source_id = :s and pm.method <> 'manual'"
        ),
        {"s": source_id},
    )
    if matches:
        conn.execute(
            text(
                "insert into core.product_match "
                "(competitor_product_id, own_product_id, match_type, method, confidence) "
                "values (:cp, :op, :t, :m, :conf) on conflict do nothing"
            ),
            matches,
        )
    result.stats["offers"] = len(products)
    result.stats["matched_identical"] = sum(m["t"] == "identical" for m in matches)
    result.stats["matched_similar"] = sum(m["t"] == "similar" for m in matches)
    result.stats["unmatched"] = unmatched
    return {p["k"]: (product_ids[p["k"]], p["c"]) for p in products}


def _load_prices(
    conn: Connection,
    rows: list[dict],
    offers: dict[str, tuple[int, int]],
    observation_sources: dict[int, int],
    result: LoadResult,
) -> None:
    channels = {
        id_: channel
        for id_, channel in conn.execute(
            text("select id, channel_code from core.company where kind = 'competitor'")
        )
    }
    observations: dict[tuple, dict] = {}
    duplicates = 0
    for r in rows:
        offer = offers.get(str(r["offer_id"]))
        region = region_code(r["region"])
        price = parse_price(r["price"])
        source = observation_sources.get(r["source_id"])
        if offer is None or source is None:
            result.reject("prices", "неизвестное предложение или источник", r)
            continue
        if region is None:
            result.reject("prices", f"не распознан регион: {r['region']!r}", r)
            continue
        if price is None:
            result.reject("prices", f"не распознана цена: {r['price']!r}", r)
            continue
        product, company = offer
        key = (date.fromisoformat(r["observed_at"][:10]), region, company, product, source)
        if key in observations:
            duplicates += 1  # одно и то же наблюдение пришло дважды — берём последнее
        observations[key] = {
            "d": key[0],
            "r": region,
            "c": company,
            "p": product,
            "ch": channels.get(company),
            "price": round(price, 2),
            "s": source,
        }
    if observations:
        conn.execute(
            text(
                "insert into core.price_observation "
                "(observed_at, region_code, company_id, product_id, channel_code, price, "
                "source_id, provenance) values (:d, :r, :c, :p, :ch, :price, :s, 'fact') "
                "on conflict (observed_at, region_code, company_id, product_id, source_id) "
                "do update set price = excluded.price, channel_code = excluded.channel_code"
            ),
            list(observations.values()),
        )
    result.stats["prices"] = len(observations)
    result.stats["price_duplicates"] = duplicates


@exclusive_load
def load_prepared(engine: Engine, source_url: str) -> LoadResult:
    """Загружает подготовленную базу по адресу SQLAlchemy (например, sqlite:///…/prepared.sqlite).

    Своя номенклатура (из 1С) должна быть загружена раньше — с ней сопоставляются
    предложения конкурентов. Изменения в core пишутся одной транзакцией.
    """
    result = start_run(engine, SOURCE_CODE, {"source": source_url.split("@")[-1]})
    source = create_engine(source_url)
    try:
        tables = {
            t: _rows(source, t)
            for t in ("sources", "competitors", "competitor_regions", "offers", "prices")
        }
        with engine.begin() as conn:
            master_id, observation_sources = _ensure_sources(conn, tables["sources"])
            _load_competitors(conn, master_id, tables["competitors"], result)
            _load_presence(conn, master_id, tables["competitor_regions"], result)
            offers = _load_offers(conn, master_id, tables["offers"], result)
            _load_prices(conn, tables["prices"], offers, observation_sources, result)
        result.stats["rejected"] = len(result.rejected)
    except Exception as exc:
        finish_run(engine, result, error=repr(exc))
        raise
    finally:
        source.dispose()
    refresh_derived(engine)
    finish_run(engine, result)
    return result
