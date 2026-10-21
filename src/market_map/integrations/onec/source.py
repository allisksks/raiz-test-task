"""Источники данных 1С: живая база по OData или файлы выгрузки в том же формате.

Имена сущностей собраны в одном месте: при подключении к конкретной конфигурации
(например, «Управление торговлей 11») их меняют здесь — см. docs/integration-1c.md.
"""

import json
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import httpx

CONTRACTORS = "Catalog_Контрагенты"
NOMENCLATURE = "Catalog_Номенклатура"
SALES = "AccumulationRegister_Продажи"
SALES_DIMENSIONS = "Контрагент,Номенклатура,Регион"
MARGIN_PLAN = "InformationRegister_ПланМаржи"

# Файлы выгрузки называются по сущностям; обороты — с суффиксом _Turnovers
SALES_FILE = f"{SALES}_Turnovers"


def next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def months(start: date, end: date) -> Iterator[date]:
    """Первые числа месяцев от start до end включительно."""
    current = start.replace(day=1)
    while current <= end:
        yield current
        current = next_month(current)


def _in_period(row: dict, start: date | None, end: date | None) -> bool:
    period = date.fromisoformat(row["Period"][:10])
    return (start is None or period >= start) and (end is None or period <= end)


class OneCSource(Protocol):
    def catalog(self, entity: str) -> list[dict]: ...

    def sales(self, start: date | None, end: date | None) -> list[dict]:
        """Обороты продаж по месяцам; у каждой строки есть Period (первое число месяца)."""
        ...

    def margin_plan(self, start: date | None, end: date | None) -> list[dict]: ...


class FileSource:
    """Файлы выгрузки OData: `<dir>/<сущность>.json`."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _read(self, name: str) -> list[dict]:
        path = self.directory / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))["value"]

    def catalog(self, entity: str) -> list[dict]:
        return self._read(entity)

    def sales(self, start: date | None, end: date | None) -> list[dict]:
        return [r for r in self._read(SALES_FILE) if _in_period(r, start, end)]

    def margin_plan(self, start: date | None, end: date | None) -> list[dict]:
        return [r for r in self._read(MARGIN_PLAN) if _in_period(r, start, end)]


def _odata_datetime(d: date, time: str = "00:00:00") -> str:
    return f"datetime'{d.isoformat()}T{time}'"


class HttpSource:
    """Живая база 1С: `http://<сервер>/<база>/odata/standard.odata/`."""

    PAGE_SIZE = 5000

    def __init__(
        self, base_url: str, user: str, password: str, *, client: httpx.Client | None = None
    ):
        self.client = client or httpx.Client(timeout=60)
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = httpx.BasicAuth(user, password)

    def _get(self, path: str, params: dict[str, str] | None = None) -> list[dict]:
        """Читает сущность целиком, постранично ($top/$skip)."""
        rows: list[dict] = []
        while True:
            page_params = {"$format": "json", "$top": str(self.PAGE_SIZE), "$skip": str(len(rows))}
            response = self.client.get(
                self.base_url + path, params={**(params or {}), **page_params}, auth=self.auth
            )
            response.raise_for_status()
            page = response.json()["value"]
            rows.extend(page)
            if len(page) < self.PAGE_SIZE:
                return rows

    def catalog(self, entity: str) -> list[dict]:
        return self._get(entity)

    def sales(self, start: date | None, end: date | None) -> list[dict]:
        if start is None or end is None:
            raise ValueError("Для живой базы 1С нужно указать период загрузки продаж")
        rows = []
        # Виртуальная таблица оборотов не умеет группировать по месяцам — запрашиваем помесячно
        for month in months(start, end):
            last_day = next_month(month) - timedelta(days=1)
            path = (
                f"{SALES}/Turnovers(StartPeriod={_odata_datetime(month)},"
                f"EndPeriod={_odata_datetime(last_day, '23:59:59')},"
                f"Dimensions='{SALES_DIMENSIONS}')"
            )
            rows.extend({**r, "Period": f"{month.isoformat()}T00:00:00"} for r in self._get(path))
        return rows

    def margin_plan(self, start: date | None, end: date | None) -> list[dict]:
        conditions = []
        if start is not None:
            conditions.append(f"Period ge {_odata_datetime(start)}")
        if end is not None:
            conditions.append(f"Period le {_odata_datetime(end)}")
        params = {"$filter": " and ".join(conditions)} if conditions else None
        return self._get(MARGIN_PLAN, params)
