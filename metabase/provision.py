"""Дашборды Metabase как код: вторая BI поверх тех же витрин marts.

Скрипт идемпотентный: при каждом старте стенда создаёт администратора, подключение
к витринам под пользователем только на чтение, карту регионов России, вопросы
(карточки) и два дашборда. Найденное по названию обновляется, лишнее в коллекции
«Market Map» архивируется.

В стенде работает в сервисе metabase-provision с --serve: заодно раздаёт Metabase
GeoJSON регионов (Metabase загружает карты только по HTTP) и после сборки помечает
себя готовым для healthcheck.

    python metabase/provision.py --url http://localhost:3000
"""

import argparse
import functools
import os
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

DATABASE = "Market Map"
SCHEMA = "marts"
COLLECTION = "Market Map"
GEOJSON_KEY = "russia_regions"
GEOJSON_DIR = Path(__file__).parent / "geo"
GEOJSON_URL = os.environ.get(
    "METABASE_GEOJSON_URL", "http://metabase-provision:8000/russia-regions.json"
)
READY_FLAG = Path("/tmp/metabase-provisioned")
# Русские даты и числа: 01.09.2026, 1 493 899,5
FORMATTING = {
    "type/Temporal": {"date_style": "D/M/YYYY", "date_separator": ".", "time_style": "HH:mm"},
    "type/Number": {"number_separators": ", "},
}


class Metabase:
    def __init__(self, url: str):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=120)

    def request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:500]}")
        return response.json() if response.content else {}

    def wait_healthy(self, timeout: int = 600) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.client.get("/api/health").json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(3)
        raise SystemExit("Metabase не поднялся")

    def login(self, email: str, password: str, site_name: str) -> None:
        properties = self.request("GET", "/api/session/properties")
        if not properties.get("has-user-setup"):
            session = self.request(
                "POST",
                "/api/setup",
                json={
                    "token": properties["setup-token"],
                    "user": {
                        "email": email,
                        "password": password,
                        "first_name": "Admin",
                        "last_name": "Admin",
                        "site_name": site_name,
                    },
                    "prefs": {"site_name": site_name, "site_locale": "ru", "allow_tracking": False},
                },
            )["id"]
        else:
            session = self.request(
                "POST", "/api/session", json={"username": email, "password": password}
            )["id"]
        self.client.headers["X-Metabase-Session"] = session


# --- Описание дашбордов -----------------------------------------------------------------

# Фильтр дашборда → колонка витрины, на которую он действует
FILTER_COLUMNS = {
    "region": "region",
    "product_group": "product_group",
    "competitor": "competitor",
    "client": "client",
    "channel": "channel",
    "product": "product",
    "new_competitor": "is_new_competitor",
    "competitor_price": "competitor_price",
}
# Фильтр «Период» действует на основную дату витрины
TIME_COLUMNS = {
    "signals": "event_date", "hypotheses": "event_date", "price_breakdown": "event_date",
    "price_comparison": "observed_at", "market_share": "month", "margin_plan_fact": "month",
    "sales": "month", "channel_share": "month",
}  # fmt: skip
WIDGETS = {"period": "date/all-options", "competitor_price": "number/between"}
FILTER_NAMES = {
    "region": "Регион", "product_group": "Группа товаров", "competitor": "Конкурент",
    "client": "Клиент", "channel": "Канал", "product": "Товар", "period": "Период",
    "new_competitor": "Новый конкурент", "competitor_price": "Цена конкурента, ₽",
}  # fmt: skip

# Карточки: SQL к витрине table. {{фильтр}} — поле-фильтр Metabase: подставляется
# условие по колонке витрины, без значения — 1 = 1.
CARDS = {
    "map_margin": dict(
        name="Карта: отклонение маржи от плана, п.п.",
        table="region_summary",
        display="map",
        sql="""
select region as "Регион", round(margin_pct_deviation * 100, 1) as "Отклонение маржи, п.п."
from marts.region_summary
where clients > 0""",
        settings={
            "map.type": "region",
            "map.region": GEOJSON_KEY,
            "map.dimension": "Регион",
            "map.metric": "Отклонение маржи, п.п.",
            # от отрицательных отклонений (красный) к положительным (синий)
            "map.colors": ["#d7191c", "#fdae61", "#ffffbf", "#abd9e9", "#2c7bb6"],
        },
    ),
    "map_new_competitors": dict(
        name="Карта: новые конкуренты",
        table="region_summary",
        display="map",
        sql="""
select region as "Регион", new_competitors as "Новые конкуренты"
from marts.region_summary
where new_competitors > 0""",
        settings={
            "map.type": "region",
            "map.region": GEOJSON_KEY,
            "map.dimension": "Регион",
            "map.metric": "Новые конкуренты",
            "map.colors": ["#fee6ce", "#fdd0a2", "#fdae6b", "#f16913", "#a63603"],
        },
    ),
    "kpi_critical": dict(
        name="Критичные сигналы",
        table="signals",
        display="scalar",
        sql="""
select count(*) as "Критичные сигналы"
from marts.signals
where severity = 'critical' and {{region}} and {{product_group}}""",
    ),
    "kpi_new_competitors": dict(
        name="Новые конкуренты за 120 дней",
        table="region_summary",
        display="scalar",
        sql="""
select coalesce(sum(new_competitors), 0) as "Новые конкуренты"
from marts.region_summary
where {{region}}""",
    ),
    "regions": dict(
        name="Регионы присутствия",
        table="region_summary",
        display="table",
        sql="""
select region as "Регион", clients as "Клиенты", competitors as "Конкуренты",
    new_competitors as "Новые конкуренты", round(revenue) as "Выручка, ₽",
    round(margin_pct * 100, 1) as "Маржа, %", round(plan_margin_pct * 100, 1) as "План маржи, %",
    round(margin_pct_deviation * 100, 1) as "Отклонение от плана, п.п.",
    round(median_price_gap::numeric * 100, 1) as "Разрыв цен конкурентов, %"
from marts.region_summary
where clients > 0 and {{region}}
order by margin_pct_deviation""",
    ),
    "signals": dict(
        name="Лента сигналов",
        table="signals",
        display="table",
        sql="""
select event_date as "Дата", severity_name as "Важность", signal_name as "Сигнал",
    region as "Регион", title as "Описание"
from marts.signals
where {{region}} and {{product_group}} and {{competitor}} and {{client}} and {{period}}
order by event_date desc, severity""",
    ),
    "hypotheses": dict(
        name="Гипотезы: за счёт чего конкурент дешевле",
        table="hypotheses",
        display="table",
        sql="""
select region as "Регион", competitor as "Конкурент", signal_name as "Сигнал",
    rank as "Место", title as "Гипотеза", round(score, 2) as "Оценка", rationale as "Доводы"
from marts.hypotheses
where rank <= 3 and signal_name <> 'Новый конкурент и изменение маржи'
    and {{region}} and {{competitor}} and {{client}} and {{period}}
order by signal_id, rank""",
    ),
    "price_breakdown": dict(
        name="Ценовая архитектура товара конкурента",
        table="price_breakdown",
        display="table",
        sql="""
select region as "Регион", competitor as "Конкурент", step as "Шаг",
    component as "Компонент", round(value, 2) as "Значение", unit as "Ед.",
    provenance_name as "Факт / оценка", note as "Пояснение"
from marts.price_breakdown
where signal_name = 'Новый конкурент' and {{region}} and {{competitor}} and {{period}}
order by signal_id, step""",
    ),
    "key_competitors": dict(
        name="Ключевые конкуренты клиента",
        table="client_competitors",
        display="table",
        sql="""
select client as "Клиент", channel as "Канал", rank as "Место", competitor as "Конкурент",
    competitor_channel as "Канал конкурента", is_new_competitor as "Новый конкурент",
    products as "Товаров в пересечении",
    round(overlap_share * 100, 1) as "Доля закупок клиента в пересечении, %",
    round(price_gap * 100, 1) as "Разрыв цены, %", round(threat, 3) as "Угроза"
from marts.client_competitors
where rank <= 3 and {{region}} and {{competitor}} and {{client}} and {{channel}}
    and {{new_competitor}}
order by client, rank""",
    ),
    "competitor_assortment": dict(
        name="Конкурент: ассортимент и цены",
        table="competitor_assortment",
        display="table",
        sql="""
select competitor as "Конкурент", competitor_product as "Товар конкурента",
    competitor_brand as "Бренд", match_type as "Совпадение", own_product as "Наш товар",
    round(competitor_price) as "Цена конкурента, ₽", round(own_price) as "Наша цена, ₽",
    round(price_gap * 100, 1) as "Разрыв цены, %", first_seen as "Появился",
    last_seen as "Последнее наблюдение", status as "Статус", is_new_competitor as "Новый конкурент"
from marts.competitor_assortment
where {{region}} and {{competitor}} and {{new_competitor}} and {{competitor_price}}
order by competitor, competitor_product""",
    ),
    "price_gap": dict(
        name="Разрыв цены конкурентов с нашей, %",
        table="price_comparison",
        display="line",
        sql="""
select date_trunc('week', observed_at)::date as "Неделя", competitor as "Конкурент",
    round(avg(price_gap) * 100, 1) as "Разрыв, %"
from marts.price_comparison
where comparable and {{region}} and {{competitor}} and {{product_group}} and {{period}}
group by 1, 2
order by 1, 2""",
        settings={"graph.dimensions": ["Неделя", "Конкурент"], "graph.metrics": ["Разрыв, %"]},
    ),
    "market_share": dict(
        name="Доли рынка, % (оценка по статистике прошлых периодов)",
        table="market_share",
        display="line",
        sql="""
select month as "Месяц", player as "Участник", round(avg(share) * 100, 1) as "Доля, %"
from marts.market_share
where method_code = 'history' and {{region}} and {{product_group}} and {{period}}
group by 1, 2
order by 1, 2""",
        settings={"graph.dimensions": ["Месяц", "Участник"], "graph.metrics": ["Доля, %"]},
    ),
    "margin_plan_fact": dict(
        name="Маржа: план и факт, %",
        table="margin_plan_fact",
        display="line",
        sql="""
select month as "Месяц",
    round(sum(margin) / nullif(sum(revenue), 0) * 100, 2) as "Факт, %",
    round(sum(plan_revenue * plan_margin_pct) / nullif(sum(plan_revenue), 0) * 100, 2)
        as "План, %"
from marts.margin_plan_fact
where {{region}} and {{product_group}} and {{client}} and {{channel}} and {{period}}
group by 1
order by 1""",
        settings={"graph.dimensions": ["Месяц"], "graph.metrics": ["Факт, %", "План, %"]},
    ),
    "channel_share": dict(
        name="Продажи по типу клиента, ₽",
        table="channel_share",
        display="bar",
        sql="""
select month as "Месяц", channel as "Канал", round(sum(revenue)) as "Выручка, ₽"
from marts.channel_share
where {{region}} and {{channel}} and {{period}}
group by 1, 2
order by 1, 2""",
        settings={
            "graph.dimensions": ["Месяц", "Канал"],
            "graph.metrics": ["Выручка, ₽"],
            "stackable.stack_type": "stacked",
        },
    ),
    "sales_by_client": dict(
        name="Клиенты и номенклатура: выручка, маржа, цена",
        table="sales",
        display="table",
        sql="""
select region as "Регион", channel as "Канал", client as "Клиент",
    product_group as "Группа товаров", product as "Товар", round(sum(revenue)) as "Выручка, ₽",
    round(sum(margin) / nullif(sum(revenue), 0) * 100, 1) as "Маржа, %",
    round(sum(revenue) / nullif(sum(quantity), 0), 2) as "Средняя цена, ₽"
from marts.sales
where {{region}} and {{product_group}} and {{client}} and {{channel}} and {{product}}
    and {{period}}
group by 1, 2, 3, 4, 5
order by 6 desc""",
    ),
    "sources": dict(
        name="Источники данных: свежесть и достоверность",
        table="source_freshness",
        display="table",
        sql="""
select name as "Источник", kind as "Тип", reliability::text as "Достоверность",
    refresh_period_name as "Периодичность", last_load as "Загружено",
    last_data_text as "Данные по", records as "Записей", failed_loads_7d as "Сбоев за 7 дней",
    status as "Статус"
from marts.source_freshness
order by reliability""",
    ),
}

MAP_INTRO = """### Интерактивная карта рынка

**Клик по региону на карте или в таблице** фильтрует весь дашборд. Клик по сигналу
открывает **«Диагностику: сигнал → причина»** по его региону.

Данные: факт (1С, мониторинг цен), оценка (доли рынка), гипотеза (причины)."""

DIAGNOSTICS_INTRO = """\
### Диагностика: клиент → регион → товар → конкурент → цена → изменение → причина

**Гипотезы — это выводы, а не факты**: у каждой есть оценка и доводы. Ценовая
архитектура показывает, где заканчиваются факты и начинаются оценки."""

# Ряды раскладки: (карточка, ширина, высота); сетка Metabase — 24 колонки
DASHBOARDS = {
    "market-map": dict(
        name="Карта рынка",
        rows=[
            [("text", MAP_INTRO, 12, 5), ("kpi_critical", 6, 5), ("kpi_new_competitors", 6, 5)],
            [("map_margin", 12, 11), ("map_new_competitors", 12, 11)],
            [("regions", 24, 7)],
            [("signals", 24, 8)],
            [("sources", 24, 5)],
        ],
        filters={"region": None, "product_group": None, "period": None},
        # Клик по региону ставит фильтр «Регион» на этом же дашборде
        crossfilter=("map_margin", "map_new_competitors", "regions"),
        # Клик по сигналу открывает диагностику с его регионом
        drill=("signals", "diagnostics"),
    ),
    "diagnostics": dict(
        name="Диагностика: сигнал → причина",
        rows=[
            [("text", DIAGNOSTICS_INTRO, 24, 3)],
            [("signals", 24, 7)],
            [("hypotheses", 24, 7)],
            [("price_breakdown", 24, 8)],
            [("key_competitors", 24, 7)],
            [("competitor_assortment", 24, 7)],
            [("price_gap", 12, 7), ("market_share", 12, 7)],
            [("margin_plan_fact", 12, 7), ("channel_share", 12, 7)],
            [("sales_by_client", 24, 8)],
        ],
        # По умолчанию — сценарий из ТЗ: новый конкурент в Новосибирской области
        filters={
            "region": ["Новосибирская область"],
            "competitor": None,
            "product_group": ["Сухие смеси"],
            "channel": None,
            "client": None,
            "product": None,
            "period": None,
            "new_competitor": None,
            "competitor_price": None,
        },  # fmt: skip
        # Группа товаров по умолчанию — только для графиков и продаж: у сигнала «новый
        # конкурент» группы нет, и фильтр скрыл бы его из ленты (как и в Superset)
        unfiltered={"product_group": ("signals",)},
    ),
}


# --- Сборка -----------------------------------------------------------------------------


def ensure_geojson(api: Metabase) -> None:
    api.request(
        "PUT",
        "/api/setting/custom-geojson",
        json={
            "value": {
                GEOJSON_KEY: {
                    "name": "Регионы России",
                    "url": GEOJSON_URL,
                    "region_key": "name",
                    "region_name": "name",
                }
            }
        },
    )


def ensure_database(api: Metabase) -> int:
    details = {
        "host": os.environ.get("POSTGRES_HOST", "db"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "dbname": os.environ.get("POSTGRES_DB", "market_map"),
        "user": os.environ.get("BI_READER_USER", "bi_reader"),
        "password": os.environ.get("BI_READER_PASSWORD", "bi_reader"),
        "schema-filters-type": "inclusion",
        "schema-filters-patterns": SCHEMA,
        "ssl": False,
    }
    databases = api.request("GET", "/api/database")["data"]
    found = next((d for d in databases if d["name"] == DATABASE), None)
    payload = {"engine": "postgres", "name": DATABASE, "details": details}
    if found is None:
        return api.request("POST", "/api/database", json=payload)["id"]
    api.request("PUT", f"/api/database/{found['id']}", json=payload)
    return found["id"]


def wait_fields(api: Metabase, database_id: int, timeout: int = 600) -> dict:
    """Ждёт, пока Metabase увидит все нужные витрины (их создают миграции планировщика).
    Возвращает {(витрина, колонка): id поля}."""
    tables = {card["table"] for card in CARDS.values()}
    deadline = time.monotonic() + timeout
    while True:
        api.request("POST", f"/api/database/{database_id}/sync_schema")
        metadata = api.request("GET", f"/api/database/{database_id}/metadata")
        fields = {
            (t["name"], f["name"]): f["id"]
            for t in metadata["tables"]
            if t["schema"] == SCHEMA
            for f in t["fields"]
        }
        if tables <= {table for table, _ in fields}:
            return fields
        if time.monotonic() > deadline:
            raise SystemExit(f"Нет витрин: {sorted(tables - {t for t, _ in fields})}")
        time.sleep(5)


def wait_data(api: Metabase, database_id: int, timeout: int = 600) -> None:
    """Ждёт первую загрузку данных планировщиком: списки значений фильтров Metabase
    собирает по данным, и по пустым витринам они остались бы пустыми до ночного
    пересканирования."""
    query = {
        "database": database_id,
        "type": "native",
        "native": {"query": "select count(*) from marts.sales"},
    }
    deadline = time.monotonic() + timeout
    while api.request("POST", "/api/dataset", json=query)["data"]["rows"][0][0] == 0:
        if time.monotonic() > deadline:
            raise SystemExit("Витрины пустые: планировщик не загрузил данные")
        time.sleep(5)


def card_filters(card: dict) -> list[str]:
    return [name for name in [*FILTER_COLUMNS, "period"] if "{{" + name + "}}" in card["sql"]]


def filter_column(card: dict, name: str) -> str:
    return TIME_COLUMNS[card["table"]] if name == "period" else FILTER_COLUMNS[name]


def enable_dropdowns(api: Metabase, fields: dict) -> None:
    """Списки значений в фильтрах: Metabase сам делает список только для колонок с
    небольшим числом значений, а регионов, клиентов и товаров больше."""
    for card in CARDS.values():
        for name in card_filters(card):
            if name in WIDGETS:
                continue
            field_id = fields[(card["table"], filter_column(card, name))]
            api.request("PUT", f"/api/field/{field_id}", json={"has_field_values": "list"})


def ensure_collection(api: Metabase) -> int:
    for collection in api.request("GET", "/api/collection"):
        if collection.get("name") == COLLECTION and not collection.get("archived"):
            return collection["id"]
    return api.request("POST", "/api/collection", json={"name": COLLECTION, "color": "#509EE3"})[
        "id"
    ]


def collection_items(api: Metabase, collection_id: int, model: str) -> dict[str, int]:
    items = api.request("GET", f"/api/collection/{collection_id}/items", params={"models": model})
    return {item["name"]: item["id"] for item in items["data"]}


def tag_id(key: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"market-map/{key}/{name}"))


def ensure_cards(api: Metabase, database_id: int, collection_id: int, fields: dict) -> dict:
    existing = collection_items(api, collection_id, "card")
    ids = {}
    for key, card in CARDS.items():
        tags = {
            name: {
                "id": tag_id(key, name),
                "name": name,
                "display-name": FILTER_NAMES[name],
                "type": "dimension",
                "dimension": ["field", fields[(card["table"], filter_column(card, name))], None],
                "widget-type": WIDGETS.get(name, "string/="),
            }
            for name in card_filters(card)
        }
        payload = {
            "name": card["name"],
            "display": card["display"],
            "collection_id": collection_id,
            "dataset_query": {
                "type": "native",
                "database": database_id,
                "native": {"query": card["sql"].strip(), "template-tags": tags},
            },
            "visualization_settings": card.get("settings", {}),
        }
        if card["name"] in existing:
            ids[key] = existing.pop(card["name"])
            api.request("PUT", f"/api/card/{ids[key]}", json={**payload, "archived": False})
        else:
            ids[key] = api.request("POST", "/api/card", json=payload)["id"]
    for name, card_id in existing.items():  # переименованные и удалённые карточки
        api.request("PUT", f"/api/card/{card_id}", json={"archived": True})
        print(f"В архив: {name}")
    return ids


def parameters(dashboard: dict) -> list[dict]:
    result = []
    for name, default in dashboard["filters"].items():
        widget = WIDGETS.get(name, "string/=")
        parameter = {
            "id": name,
            "name": FILTER_NAMES[name],
            "slug": name,
            "type": widget,
            "sectionId": widget.split("/")[0],
        }
        if default is not None:
            parameter["default"] = default
        result.append(parameter)
    return result


def click_to_filter(card_column: str, parameter: str) -> dict:
    return {
        "source": {"type": "column", "id": card_column, "name": card_column},
        "target": {"type": "parameter", "id": parameter},
        "id": parameter,
    }


def dashcards(dashboard: dict, cards: dict, dashboard_ids: dict) -> list[dict]:
    result = []
    crossfilter = dashboard.get("crossfilter", ())
    drill_card, drill_target = dashboard.get("drill", (None, None))
    unfiltered = dashboard.get("unfiltered", {})
    row_y = 0
    for row in dashboard["rows"]:
        col = 0
        for item in row:
            if item[0] == "text":
                _, text, width, height = item
                card_id, settings, mappings = None, _text_card(text), []
            else:
                key, width, height = item
                card = CARDS[key]
                card_id, settings = cards[key], {}
                mappings = [
                    {
                        "parameter_id": name,
                        "card_id": card_id,
                        "target": ["dimension", ["template-tag", name]],
                    }
                    for name in card_filters(card)
                    if name in dashboard["filters"] and key not in unfiltered.get(name, ())
                ]
                if key in crossfilter:
                    settings["click_behavior"] = {
                        "type": "crossfilter",
                        "parameterMapping": {"region": click_to_filter("Регион", "region")},
                    }
                if key == drill_card and drill_target in dashboard_ids:
                    settings["click_behavior"] = {
                        "type": "link",
                        "linkType": "dashboard",
                        "targetId": dashboard_ids[drill_target],
                        "parameterMapping": {"region": click_to_filter("Регион", "region")},
                    }
            result.append(
                {
                    "id": -len(result) - 1,  # отрицательный id — новая карточка на дашборде
                    "card_id": card_id,
                    "row": row_y,
                    "col": col,
                    "size_x": width,
                    "size_y": height,
                    "parameter_mappings": mappings,
                    "visualization_settings": settings,
                }
            )
            col += width
        row_y += max(item[-1] for item in row)
    return result


def _text_card(text: str) -> dict:
    return {
        "virtual_card": {
            "name": None,
            "display": "text",
            "visualization_settings": {},
            "dataset_query": {},
            "archived": False,
        },
        "text": text,
    }


def ensure_dashboards(api: Metabase, collection_id: int, cards: dict) -> dict:
    existing = collection_items(api, collection_id, "dashboard")
    ids = {}
    # Сначала создаём недостающие дашборды: ссылка «сигнал → диагностика» требует id цели
    for slug, dashboard in DASHBOARDS.items():
        if dashboard["name"] in existing:
            ids[slug] = existing.pop(dashboard["name"])
        else:
            ids[slug] = api.request(
                "POST",
                "/api/dashboard",
                json={"name": dashboard["name"], "collection_id": collection_id},
            )["id"]
    for slug, dashboard in DASHBOARDS.items():
        api.request(
            "PUT",
            f"/api/dashboard/{ids[slug]}",
            json={
                "name": dashboard["name"],
                "collection_id": collection_id,
                "parameters": parameters(dashboard),
                "dashcards": dashcards(dashboard, cards, ids),
                "tabs": [],
                "archived": False,
            },
        )
    for name, dashboard_id in existing.items():
        api.request("PUT", f"/api/dashboard/{dashboard_id}", json={"archived": True})
        print(f"В архив: {name}")
    return ids


def serve_geojson(port: int) -> None:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=GEOJSON_DIR)
    server = ThreadingHTTPServer(("", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def provision(api: Metabase, email: str, password: str) -> dict:
    api.wait_healthy()
    api.login(email, password, site_name="Market Map")
    api.request("PUT", "/api/setting/site-locale", json={"value": "ru"})
    api.request("PUT", "/api/setting/custom-formatting", json={"value": FORMATTING})
    ensure_geojson(api)
    database_id = ensure_database(api)
    fields = wait_fields(api, database_id)
    wait_data(api, database_id)
    enable_dropdowns(api, fields)
    api.request("POST", f"/api/database/{database_id}/rescan_values")
    collection_id = ensure_collection(api)
    cards = ensure_cards(api, database_id, collection_id, fields)
    dashboards = ensure_dashboards(api, collection_id, cards)
    print(f"Metabase: карточек {len(cards)}, дашбордов {len(dashboards)}", flush=True)
    return dashboards


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("METABASE_URL", "http://localhost:3000"))
    parser.add_argument(
        "--email", default=os.environ.get("METABASE_ADMIN_EMAIL", "admin@example.com")
    )
    parser.add_argument(
        "--password", default=os.environ.get("METABASE_ADMIN_PASSWORD", "market-map-2026")
    )
    parser.add_argument("--serve", action="store_true", help="раздавать GeoJSON и не завершаться")
    parser.add_argument("--port", type=int, default=8000, help="порт для GeoJSON")
    args = parser.parse_args()

    if args.serve:
        READY_FLAG.unlink(missing_ok=True)
        serve_geojson(args.port)
    dashboards = provision(Metabase(args.url), args.email, args.password)
    for slug, dashboard_id in dashboards.items():
        print(f"  {DASHBOARDS[slug]['name']}: /dashboard/{dashboard_id}", flush=True)
    if args.serve:
        READY_FLAG.touch()
        threading.Event().wait()


if __name__ == "__main__":
    main()
