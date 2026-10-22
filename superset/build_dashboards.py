"""Сборка дашбордов Superset через REST API («дашборды как код»).

Запускается вручную против работающего Superset, затем результат выгружается в
superset/dashboards/ (python superset/build_dashboards.py --export). При разворачивании
стенда выгрузка импортируется автоматически (superset/bootstrap.sh).

    python superset/build_dashboards.py --url http://localhost:8088 --export
"""

import argparse
import io
import json
import re
import shutil
import uuid
import zipfile
from pathlib import Path

import httpx

DATABASE = "Market Map"
SCHEMA = "marts"
EXPORT_DIR = Path(__file__).parent / "dashboards"


class Superset:
    def __init__(self, url: str, user: str, password: str):
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60)
        token = self.client.post(
            "/api/v1/security/login",
            json={"username": user, "password": password, "provider": "db", "refresh": True},
        ).json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"
        csrf = self.client.get("/api/v1/security/csrf_token/").json()["result"]
        self.client.headers["X-CSRFToken"] = csrf
        self.client.headers["Referer"] = url

    def request(self, method: str, path: str, **kwargs) -> dict:
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text}")
        return response.json() if response.content else {}

    def find(self, resource: str, column: str, value: str) -> int | None:
        q = json.dumps({"filters": [{"col": column, "opr": "eq", "value": value}]})
        result = self.request("GET", f"/api/v1/{resource}/", params={"q": q})["result"]
        return result[0]["id"] if result else None


def metric(sql: str, label: str) -> dict:
    return {"expressionType": "SQL", "sqlExpression": sql, "label": label}


def where(sql: str) -> dict:
    return {"expressionType": "SQL", "sqlExpression": sql, "clause": "WHERE"}


# --- Описание дашбордов -----------------------------------------------------------------

DATASETS = (
    "region_summary", "presence", "signals", "hypotheses", "price_breakdown",
    "price_comparison", "market_share", "margin_plan_fact", "sales", "source_freshness",
)  # fmt: skip

TABLE = {
    "query_mode": "raw",
    "row_limit": 1000,
    "server_pagination": False,
    "include_search": True,
    "table_timestamp_format": "%d.%m.%Y",
}
PERCENT = {"d3NumberFormat": ".1%"}
RUBLES = {"d3NumberFormat": ",.0f"}

# Подписи колонок в таблицах и фильтрах
VERBOSE = {
    "region": "Регион", "clients": "Клиенты", "competitors": "Конкуренты",
    "new_competitors": "Новые конкуренты", "revenue": "Выручка, ₽", "margin_pct": "Маржа",
    "plan_margin_pct": "План маржи", "margin_pct_deviation": "Отклонение от плана",
    "median_price_gap": "Разрыв цен конкурентов", "event_date": "Дата", "severity_name": "Важность",
    "signal_name": "Сигнал", "title": "Описание", "competitor": "Конкурент", "rank": "Место",
    "score": "Оценка", "rationale": "Доводы", "step": "Шаг", "component": "Компонент",
    "value": "Значение", "unit": "Ед.", "provenance": "Происхождение", "note": "Пояснение",
    "client": "Клиент", "channel": "Канал", "product_group": "Группа товаров",
    "player": "Участник", "month": "Месяц", "observed_at": "Неделя", "signal_id": "№ сигнала",
    "provenance_name": "Факт / оценка", "name": "Источник", "kind": "Тип",
    "reliability": "Достоверность", "refresh_period_name": "Периодичность", "last_load": "Загружено",
    "last_data_text": "Данные по", "records": "Записей", "failed_loads_7d": "Сбоев за 7 дней",
    "status": "Статус",
}  # fmt: skip

CHARTS = {
    "map_margin": dict(
        name="Карта: отклонение маржи от плана, п.п.",
        dataset="region_summary",
        viz_type="country_map",
        params=dict(
            select_country="russia",
            entity="map_code",
            metric=metric("AVG(margin_pct_deviation) * 100", "Отклонение маржи, п.п."),
            linear_color_scheme="red_yellow_blue",
            number_format=".1f",
        ),
    ),
    "map_new_competitors": dict(
        name="Карта: новые конкуренты",
        dataset="region_summary",
        viz_type="country_map",
        params=dict(
            select_country="russia",
            entity="map_code",
            metric=metric("SUM(new_competitors)", "Новые конкуренты"),
            linear_color_scheme="oranges",
            number_format=".0f",
        ),
    ),
    "kpi_critical": dict(
        name="Критичные сигналы",
        dataset="signals",
        viz_type="big_number_total",
        params=dict(
            metric=metric("COUNT(*)", "Критичные сигналы"),
            adhoc_filters=[where("severity = 'critical'")],
            subheader="требуют реакции",
        ),
    ),
    "kpi_new_competitors": dict(
        name="Новые конкуренты",
        dataset="region_summary",
        viz_type="big_number_total",
        params=dict(
            metric=metric("SUM(new_competitors)", "Новые конкуренты"), subheader="за 120 дней"
        ),
    ),
    "regions": dict(
        name="Регионы присутствия",
        dataset="region_summary",
        viz_type="table",
        params=dict(
            **TABLE,
            all_columns=[
                "region",
                "clients",
                "competitors",
                "new_competitors",
                "revenue",
                "margin_pct",
                "plan_margin_pct",
                "margin_pct_deviation",
                "median_price_gap",
            ],
            order_by_cols=[json.dumps(["margin_pct_deviation", True])],
            adhoc_filters=[where("clients > 0")],
            column_config={
                "revenue": RUBLES,
                "margin_pct": PERCENT,
                "plan_margin_pct": PERCENT,
                "margin_pct_deviation": PERCENT,
                "median_price_gap": PERCENT,
            },
        ),
    ),  # fmt: skip
    "signals": dict(
        name="Лента сигналов",
        dataset="signals",
        viz_type="table",
        params=dict(
            **TABLE,
            all_columns=["event_date", "severity_name", "signal_name", "region", "title"],
            order_by_cols=[json.dumps(["event_date", False])],
        ),
    ),
    "hypotheses": dict(
        name="Гипотезы: за счёт чего конкурент дешевле",
        dataset="hypotheses",
        viz_type="table",
        params=dict(
            **TABLE,
            all_columns=[
                "region",
                "competitor",
                "signal_name",
                "rank",
                "title",
                "score",
                "rationale",
            ],
            order_by_cols=[json.dumps(["signal_id", True]), json.dumps(["rank", True])],
            adhoc_filters=[
                where("rank <= 3 AND signal_name <> 'Новый конкурент и изменение маржи'")
            ],
            column_config={"score": {"d3NumberFormat": ".2f"}},
        ),
    ),  # fmt: skip
    "price_breakdown": dict(
        name="Ценовая архитектура товара конкурента",
        dataset="price_breakdown",
        viz_type="table",
        params=dict(
            **TABLE,
            all_columns=[
                "region",
                "competitor",
                "signal_name",
                "step",
                "component",
                "value",
                "unit",
                "provenance_name",
                "note",
            ],
            order_by_cols=[json.dumps(["signal_id", True]), json.dumps(["step", True])],
            adhoc_filters=[where("signal_name = 'Новый конкурент'")],
            column_config={"value": {"d3NumberFormat": ",.2f"}},
        ),
    ),  # fmt: skip
    "sources": dict(
        name="Источники данных: свежесть и достоверность",
        dataset="source_freshness",
        viz_type="table",
        params=dict(
            **{**TABLE, "table_timestamp_format": "%d.%m.%Y %H:%M"},
            all_columns=[
                "name",
                "kind",
                "reliability",
                "refresh_period_name",
                "last_load",
                "last_data_text",
                "records",
                "failed_loads_7d",
                "status",
            ],
            order_by_cols=[json.dumps(["reliability", True])],
        ),
    ),
    "market_share": dict(
        name="Доли рынка (оценка), %",
        dataset="market_share",
        viz_type="echarts_timeseries_line",
        params=dict(
            x_axis="month",
            time_grain_sqla="P1M",
            metrics=[metric("AVG(share) * 100", "Доля, %")],
            groupby=["player"],
            row_limit=10000,
            show_legend=True,
            y_axis_format=".0f",
        ),
    ),
    "price_gap": dict(
        name="Разрыв цены конкурентов с нашей, %",
        dataset="price_comparison",
        viz_type="echarts_timeseries_line",
        params=dict(
            x_axis="observed_at",
            time_grain_sqla="P1W",
            metrics=[metric("AVG(price_gap) * 100", "Разрыв, %")],
            groupby=["competitor"],
            adhoc_filters=[where("comparable")],
            row_limit=10000,
            show_legend=True,
            y_axis_format=".0f",
        ),
    ),
    "margin_plan_fact": dict(
        name="Маржа: план и факт, %",
        dataset="margin_plan_fact",
        viz_type="echarts_timeseries_line",
        params=dict(
            x_axis="month",
            time_grain_sqla="P1M",
            metrics=[
                metric("SUM(margin) / NULLIF(SUM(revenue), 0) * 100", "Факт, %"),
                metric(
                    "SUM(plan_revenue * plan_margin_pct) / NULLIF(SUM(plan_revenue), 0) * 100",
                    "План, %",
                ),
            ],
            row_limit=10000,
            show_legend=True,
            y_axis_format=".1f",
        ),
    ),
    "sales_by_client": dict(
        name="Клиенты: выручка и маржа",
        dataset="sales",
        viz_type="table",
        params=dict(
            query_mode="aggregate",
            groupby=["region", "channel", "client", "product_group"],
            metrics=[
                metric("SUM(revenue)", "Выручка, ₽"),
                metric("SUM(margin) / NULLIF(SUM(revenue), 0) * 100", "Маржа, %"),
            ],
            row_limit=1000,
            include_search=True,
        ),
    ),
}

MAP_INTRO = """### Интерактивная карта рынка

Карта по регионам присутствия: где маржа клиентов ниже плана и где появились новые
конкуренты. Лента сигналов — значимые изменения, переход к причинам — дашборд
**«Диагностика: сигнал → причина»**.

Данные: факт (1С, мониторинг цен), оценка (доли рынка), гипотеза (причины) — помечено в таблицах."""

DIAGNOSTICS_INTRO = """### Диагностика: клиент → регион → товар → конкурент → цена → изменение → причина

Выберите регион, конкурента и группу товаров в фильтрах слева. **Гипотезы — это выводы,
а не факты**: у каждой есть оценка и доводы. Ценовая архитектура показывает, где
заканчиваются факты (наша закупочная цена из 1С, цены мониторинга) и начинаются оценки."""

DASHBOARDS = {
    "market-map": dict(
        title="Карта рынка",
        rows=[
            [("markdown", MAP_INTRO, 6, 30), ("kpi_critical", 3, 30), ("kpi_new_competitors", 3, 30)],
            [("map_margin", 6, 80), ("map_new_competitors", 6, 80)],
            [("regions", 12, 60)],
            [("signals", 12, 80)],
            [("sources", 12, 50)],
        ],
        filters=[
            ("Регион", "region", "region_summary", None, ()),
            ("Группа товаров", "product_group", "signals", None, ()),
        ],
    ),
    "diagnostics": dict(
        title="Диагностика: сигнал → причина",
        rows=[
            [("markdown", DIAGNOSTICS_INTRO, 12, 25)],
            [("signals", 12, 60)],
            [("hypotheses", 12, 55)],
            [("price_breakdown", 12, 70)],
            [("price_gap", 6, 60), ("market_share", 6, 60)],
            [("margin_plan_fact", 6, 60), ("sales_by_client", 6, 60)],
        ],
        # По умолчанию открывается сценарий из ТЗ: новый конкурент в Новосибирске.
        # Группа товаров по умолчанию — только для графиков: у сигнала «новый конкурент»
        # группы нет, и фильтр скрыл бы его из ленты.
        filters=[
            ("Регион", "region", "signals", "Новосибирская область", ()),
            ("Конкурент", "competitor", "price_comparison", None, ()),
            ("Группа товаров", "product_group", "signals", "Сухие смеси",
             ("signals", "hypotheses", "price_breakdown", "sales_by_client")),
            ("Канал", "channel", "sales", None, ()),
        ],
    ),
}  # fmt: skip


# --- Сборка -----------------------------------------------------------------------------


def ensure_datasets(api: Superset, database_id: int) -> dict[str, int]:
    ids = {}
    for table in DATASETS:
        dataset_id = api.find("dataset", "table_name", table)
        if dataset_id is None:
            dataset_id = api.request(
                "POST", "/api/v1/dataset/",
                json={"database": database_id, "schema": SCHEMA, "table_name": table},
            )["id"]  # fmt: skip
        else:
            # подтянуть колонки, появившиеся в витрине после создания датасета
            api.request("PUT", f"/api/v1/dataset/{dataset_id}/refresh")
        ids[table] = dataset_id
    return ids


def set_verbose_names(api: Superset, datasets: dict[str, int]) -> None:
    keep = ("id", "column_name", "verbose_name", "type", "is_dttm", "groupby", "filterable",
            "expression", "description")  # fmt: skip
    for dataset_id in datasets.values():
        columns = api.request("GET", f"/api/v1/dataset/{dataset_id}")["result"]["columns"]
        payload = [
            {**{k: c[k] for k in keep if k in c}, "verbose_name": VERBOSE.get(c["column_name"])}
            for c in columns
        ]
        api.request("PUT", f"/api/v1/dataset/{dataset_id}", json={"columns": payload})


def ensure_charts(api: Superset, datasets: dict[str, int]) -> dict[str, int]:
    ids = {}
    for key, chart in CHARTS.items():
        params = {"viz_type": chart["viz_type"], "adhoc_filters": [], **chart["params"]}
        payload = {
            "slice_name": chart["name"],
            "viz_type": chart["viz_type"],
            "datasource_id": datasets[chart["dataset"]],
            "datasource_type": "table",
            "params": json.dumps(params, ensure_ascii=False),
        }
        chart_id = api.find("chart", "slice_name", chart["name"])
        if chart_id is None:
            chart_id = api.request("POST", "/api/v1/chart/", json=payload)["id"]
        else:
            api.request("PUT", f"/api/v1/chart/{chart_id}", json=payload)
        ids[key] = chart_id
    return ids


def chart_uuids(api: Superset, chart_ids: list[int]) -> dict[int, str]:
    """uuid графиков (их нет в списке REST API — берём из экспорта). По uuid импорт
    дашборда на другом стенде находит графики, у которых там будут другие id."""
    q = "!(" + ",".join(map(str, chart_ids)) + ")"
    response = api.client.get("/api/v1/chart/export/", params={"q": q})
    response.raise_for_status()
    uuids = {}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            if "/charts/" not in name:
                continue
            chart_id = int(re.search(r"(\d+)\.yaml$", name).group(1))
            text = archive.read(name).decode("utf-8")
            uuids[chart_id] = re.search(r"^uuid: (\S+)$", text, re.MULTILINE).group(1)
    return uuids


def layout(title: str, rows: list, charts: dict[str, int], uuids: dict[int, str]) -> dict:
    position = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"type": "HEADER", "id": "HEADER_ID", "meta": {"text": title}},
    }
    for r, row in enumerate(rows):
        row_id = f"ROW-{r}"
        position["GRID_ID"]["children"].append(row_id)
        position[row_id] = {
            "type": "ROW", "id": row_id, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }  # fmt: skip
        for c, item in enumerate(row):
            if item[0] == "markdown":
                _, code, width, height = item
                item_id = f"MARKDOWN-{r}-{c}"
                meta = {"code": code, "width": width, "height": height}
                kind = "MARKDOWN"
            else:
                key, width, height = item
                item_id = f"CHART-{key}"
                meta = {"chartId": charts[key], "uuid": uuids[charts[key]], "width": width,
                        "height": height, "sliceName": CHARTS[key]["name"]}  # fmt: skip
                kind = "CHART"
            position[row_id]["children"].append(item_id)
            position[item_id] = {
                "type": kind, "id": item_id, "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_id], "meta": meta,
            }  # fmt: skip
    return position


def native_filters(filters: list, datasets: dict, charts: dict, slug: str) -> list[dict]:
    result = []
    for name, column, dataset, default, excluded in filters:
        mask = {"filterState": {}, "extraFormData": {}}
        if default is not None:
            mask = {
                "filterState": {"value": [default]},
                "extraFormData": {"filters": [{"col": column, "op": "IN", "val": [default]}]},
            }
        result.append(
            {
                "id": f"NATIVE_FILTER-{uuid.uuid5(uuid.NAMESPACE_URL, slug + column).hex[:12]}",
                "name": name,
                "filterType": "filter_select",
                "targets": [{"datasetId": datasets[dataset], "column": {"name": column}}],
                "defaultDataMask": mask,
                "controlValues": {
                    "multiSelect": True, "enableEmptyFilter": False, "defaultToFirstItem": False,
                    "searchAllOptions": False, "inverseSelection": False,
                },
                "cascadeParentIds": [],
                "scope": {"rootPath": ["ROOT_ID"], "excluded": [charts[k] for k in excluded]},
                "type": "NATIVE_FILTER",
                "description": "",
            }
        )  # fmt: skip
    return result


def ensure_dashboards(api: Superset, datasets: dict, charts: dict) -> dict[str, int]:
    ids = {}
    uuids = chart_uuids(api, list(charts.values()))
    for slug, dashboard in DASHBOARDS.items():
        used = [item[0] for row in dashboard["rows"] for item in row if item[0] != "markdown"]
        payload = {
            "dashboard_title": dashboard["title"],
            "slug": slug,
            "published": True,
            "position_json": json.dumps(
                layout(dashboard["title"], dashboard["rows"], charts, uuids), ensure_ascii=False
            ),
            "json_metadata": json.dumps(
                {
                    "native_filter_configuration": native_filters(
                        dashboard["filters"], datasets, charts, slug
                    ),
                    "cross_filters_enabled": True,
                    # Цветовую схему дашборду не задаём: с ней карта красит регионы
                    # категориальными цветами вместо шкалы по значению
                    "refresh_frequency": 0,
                },
                ensure_ascii=False,
            ),
        }
        dashboard_id = api.find("dashboard", "slug", slug)
        if dashboard_id is None:
            dashboard_id = api.request("POST", "/api/v1/dashboard/", json=payload)["id"]
        else:
            api.request("PUT", f"/api/v1/dashboard/{dashboard_id}", json=payload)
        for key in used:
            chart = api.request("GET", f"/api/v1/chart/{charts[key]}")["result"]
            linked = {d["id"] for d in chart.get("dashboards", [])} | {dashboard_id}
            api.request("PUT", f"/api/v1/chart/{charts[key]}", json={"dashboards": sorted(linked)})
        ids[slug] = dashboard_id
    return ids


def export(api: Superset, charts: dict[str, int], dashboards: dict[str, int]) -> None:
    """Выгрузка штатным экспортом. Файлы графиков и дашбордов называются по ключам из
    CHARTS/DASHBOARDS, а не по id: id меняются от стенда к стенду, и без этого каждая
    пересборка «меняла» бы все файлы."""
    names = {
        "charts": {chart_id: key for key, chart_id in charts.items()},
        "dashboards": {dashboard_id: slug for slug, dashboard_id in dashboards.items()},
    }
    q = "!(" + ",".join(map(str, dashboards.values())) + ")"
    response = api.client.get("/api/v1/dashboard/export/", params={"q": q})
    response.raise_for_status()
    shutil.rmtree(EXPORT_DIR, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            # dashboard_export_<дата>/charts/…_<id>.yaml → charts/<ключ>.yaml
            relative = Path(*Path(name).parts[1:])
            kind = relative.parts[0]
            if kind in names:
                object_id = int(re.search(r"(\d+)\.yaml$", relative.name).group(1))
                relative = Path(kind, f"{names[kind][object_id]}.yaml")
            target = EXPORT_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8088")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--export", action="store_true", help=f"выгрузить в {EXPORT_DIR}")
    args = parser.parse_args()

    api = Superset(args.url, args.user, args.password)
    database_id = api.find("database", "database_name", DATABASE)
    if database_id is None:
        raise SystemExit(f"Нет подключения «{DATABASE}» — сначала поднимите стенд")
    datasets = ensure_datasets(api, database_id)
    set_verbose_names(api, datasets)
    charts = ensure_charts(api, datasets)
    dashboards = ensure_dashboards(api, datasets, charts)
    print(f"Датасетов: {len(datasets)}, графиков: {len(charts)}, дашбордов: {len(dashboards)}")
    if args.export:
        export(api, charts, dashboards)
        print(f"Выгружено в {EXPORT_DIR}")


if __name__ == "__main__":
    main()
