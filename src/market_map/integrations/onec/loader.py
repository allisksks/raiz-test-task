"""Загрузка данных 1С в хранилище: staging (как есть) → core (нормализовано).

Загрузка идемпотентна: справочники обновляются по GUID 1С (Ref_Key), продажи и план
за загружаемые месяцы заменяются целиком.
"""

import json
from datetime import date

from sqlalchemy import Connection, Engine, text

from market_map.integrations.onec.source import (
    CONTRACTORS,
    MARGIN_PLAN,
    NOMENCLATURE,
    SALES,
    OneCSource,
    months,
)
from market_map.loads import (
    LoadResult,
    exclusive_load,
    finish_run,
    product_group_ids,
    source_ids,
    start_run,
)
from market_map.marts import refresh_derived
from market_map.normalize import channel_code, region_code

SOURCE_CODE = "1c"


def _period(row: dict) -> date:
    return date.fromisoformat(row["Period"][:10])


def _ensure_source(conn: Connection) -> int:
    return conn.execute(
        text(
            "insert into core.data_source "
            "(code, name, kind, reliability, auto_fetch, refresh_period, restrictions) "
            "values (:code, '1С: учётная система компании', '1c', 'A', true, 'daily', "
            "'Внутренние данные компании') "
            "on conflict (code) do update set name = excluded.name returning id"
        ),
        {"code": SOURCE_CODE},
    ).scalar_one()


def _save_raw(conn: Connection, run_id: int, entity: str, rows: list[dict]) -> None:
    if rows:
        conn.execute(
            text("insert into staging.onec_raw (run_id, entity, payload) values (:r, :e, :p)"),
            [{"r": run_id, "e": entity, "p": json.dumps(row, ensure_ascii=False)} for row in rows],
        )


def _load_nomenclature(conn: Connection, source_id: int, rows: list[dict], result: LoadResult):
    groups = {r["Ref_Key"]: r["Description"] for r in rows if r["IsFolder"]}
    for ref_key, name in groups.items():
        conn.execute(
            text(
                "insert into core.product_group (name, source_key) values (:n, :k) "
                "on conflict (name) do update set source_key = excluded.source_key"
            ),
            {"n": name, "k": ref_key},
        )
    group_ids = product_group_ids(conn)

    products = []
    for r in rows:
        if r["IsFolder"] or r["DeletionMark"]:
            continue
        group_id = group_ids.get(r["Parent_Key"])
        if group_id is None:
            result.reject(NOMENCLATURE, "товар вне группы номенклатуры", r)
            continue
        products.append(
            {
                "g": group_id,
                "sku": r.get("Артикул") or r.get("Code"),
                "n": r["Description"],
                "b": r.get("Бренд"),
                "u": r.get("ЕдиницаИзмерения"),
                "s": source_id,
                "k": r["Ref_Key"],
            }
        )
    if products:
        conn.execute(
            text(
                "insert into core.product "
                "(group_id, company_id, sku, name, brand, unit, source_id, source_key) "
                "values (:g, null, :sku, :n, :b, :u, :s, :k) "
                "on conflict (source_id, source_key) do update set group_id = excluded.group_id, "
                "sku = excluded.sku, name = excluded.name, brand = excluded.brand, "
                "unit = excluded.unit"
            ),
            products,
        )
    result.stats["product_groups"] = len(groups)
    result.stats["products"] = len(products)


def _load_clients(
    conn: Connection, source_id: int, rows: list[dict], result: LoadResult
) -> dict[str, str]:
    """Загружает клиентов; возвращает {Ref_Key: код региона}."""
    clients, regions = [], {}
    for r in rows:
        if r["DeletionMark"]:
            continue
        region = region_code(r.get("Регион"))
        if region is None:
            result.reject(CONTRACTORS, f"не распознан регион: {r.get('Регион')!r}", r)
            continue
        channel = channel_code(r.get("КаналСбыта"))
        if channel is None and r.get("КаналСбыта"):
            result.reject(CONTRACTORS, f"не распознан канал: {r['КаналСбыта']!r}", r)
            continue
        regions[r["Ref_Key"]] = region
        clients.append(
            {
                "n": r["Description"],
                "inn": r.get("ИНН") or None,
                "c": channel,
                "s": source_id,
                "k": r["Ref_Key"],
            }
        )
    if clients:
        conn.execute(
            text(
                "insert into core.company (kind, name, inn, channel_code, source_id, source_key) "
                "values ('client', :n, :inn, :c, :s, :k) "
                "on conflict (source_id, source_key) do update set name = excluded.name, "
                "inn = excluded.inn, channel_code = excluded.channel_code"
            ),
            clients,
        )
    result.stats["clients"] = len(clients)
    return regions


def _load_presence(
    conn: Connection, source_id: int, regions: dict[str, str], sales: list[dict]
) -> None:
    """Присутствие клиента в регионе — с первого месяца продаж (или с сегодняшнего дня)."""
    first_sale: dict[str, date] = {}
    for r in sales:
        key = r["Контрагент_Key"]
        first_sale[key] = min(first_sale.get(key, date.max), _period(r))
    client_ids = source_ids(conn, "company", source_id)
    for ref_key, region in regions.items():
        params = {
            "c": client_ids[ref_key],
            "r": region,
            "f": first_sale.get(ref_key, date.today().replace(day=1)),
        }
        # Открытый период присутствия уже есть — только сдвигаем его начало назад при необходимости
        updated = conn.execute(
            text(
                "update core.company_region set valid_from = least(valid_from, :f) "
                "where company_id = :c and region_code = :r and valid_to is null"
            ),
            params,
        ).rowcount
        if not updated:
            conn.execute(
                text(
                    "insert into core.company_region (company_id, region_code, valid_from) "
                    "values (:c, :r, :f)"
                ),
                params,
            )


def _replace_periods(conn: Connection, table: str, source_id: int, periods: set[date]) -> None:
    conn.execute(
        text(
            f"delete from core.{table} t using core.company c "
            "where c.id = t.client_id and c.source_id = :s and t.period = any(:p)"
        ),
        {"s": source_id, "p": sorted(periods)},
    )


def _load_sales(
    conn: Connection, source_id: int, rows: list[dict], periods: set[date], result: LoadResult
) -> None:
    client_ids = source_ids(conn, "company", source_id)
    product_ids = source_ids(conn, "product", source_id)
    facts: dict[tuple, dict] = {}
    for r in rows:
        client = client_ids.get(r["Контрагент_Key"])
        product = product_ids.get(r["Номенклатура_Key"])
        region = region_code(r.get("Регион"))
        if client is None or product is None:
            result.reject(SALES, "неизвестный клиент или товар", r)
            continue
        if region is None:
            result.reject(SALES, f"не распознан регион: {r.get('Регион')!r}", r)
            continue
        key = (_period(r), client, region, product)
        # Если один регион записан по-разному, строки схлопнутся — суммируем
        fact = facts.setdefault(key, {"q": 0, "rev": 0, "cost": 0})
        fact["q"] += r["КоличествоTurnover"]
        fact["rev"] += r["СтоимостьTurnover"]
        fact["cost"] += r["СебестоимостьTurnover"]

    _replace_periods(conn, "sales_fact", source_id, periods)
    if facts:
        conn.execute(
            text(
                "insert into core.sales_fact "
                "(period, client_id, region_code, product_id, quantity, revenue, cost) "
                "values (:p, :c, :r, :pr, :q, :rev, :cost)"
            ),
            [{"p": p, "c": c, "r": r, "pr": pr, **v} for (p, c, r, pr), v in facts.items()],
        )
    result.stats["sales_rows"] = len(facts)


def _load_margin_plan(
    conn: Connection, source_id: int, rows: list[dict], periods: set[date], result: LoadResult
) -> None:
    client_ids = source_ids(conn, "company", source_id)
    group_ids = product_group_ids(conn)
    plans = []
    for r in rows:
        client = client_ids.get(r["Контрагент_Key"])
        group = group_ids.get(r["ГруппаНоменклатуры_Key"])
        if client is None or group is None:
            result.reject(MARGIN_PLAN, "неизвестный клиент или группа номенклатуры", r)
            continue
        plans.append(
            {
                "p": _period(r),
                "c": client,
                "g": group,
                "rev": r["ПлановаяВыручка"],
                "m": r["ПлановаяМаржаПроцент"],
            }
        )
    _replace_periods(conn, "margin_plan", source_id, periods)
    if plans:
        conn.execute(
            text(
                "insert into core.margin_plan (period, client_id, product_group_id, revenue, "
                "margin_pct) values (:p, :c, :g, :rev, :m)"
            ),
            plans,
        )
    result.stats["margin_plan_rows"] = len(plans)


@exclusive_load
def load_onec(
    engine: Engine, source: OneCSource, start: date | None = None, end: date | None = None
) -> LoadResult:
    """Загружает справочники, продажи и план маржи за период [start, end] (по месяцам).

    Без периода загружается всё, что отдаёт источник. Изменения в core пишутся одной
    транзакцией: при ошибке core не меняется, а запуск помечается в журнале как failed.
    """
    result = start_run(
        engine,
        SOURCE_CODE,
        {"start": start and start.isoformat(), "end": end and end.isoformat()},
    )
    try:
        contractors = source.catalog(CONTRACTORS)
        nomenclature = source.catalog(NOMENCLATURE)
        sales = source.sales(start, end)
        plan = source.margin_plan(start, end)

        if start is not None and end is not None:
            periods = set(months(start, end))
        else:
            periods = {_period(r) for r in sales} | {_period(r) for r in plan}

        # Сырые данные сохраняются отдельно: если загрузка в core упадёт, их можно разобрать
        with engine.begin() as conn:
            for entity, rows in (
                (CONTRACTORS, contractors),
                (NOMENCLATURE, nomenclature),
                (SALES, sales),
                (MARGIN_PLAN, plan),
            ):
                _save_raw(conn, result.run_id, entity, rows)

        with engine.begin() as conn:
            source_id = _ensure_source(conn)
            _load_nomenclature(conn, source_id, nomenclature, result)
            regions = _load_clients(conn, source_id, contractors, result)
            _load_presence(conn, source_id, regions, sales)
            _load_sales(conn, source_id, sales, periods, result)
            _load_margin_plan(conn, source_id, plan, periods, result)
        result.stats["rejected"] = len(result.rejected)
    except Exception as exc:
        finish_run(engine, result, error=repr(exc))
        raise
    refresh_derived(engine)
    finish_run(engine, result)
    return result
