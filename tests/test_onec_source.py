import json
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from market_map.integrations.onec.source import (
    CONTRACTORS,
    MARGIN_PLAN,
    FileSource,
    HttpSource,
    months,
)

SAMPLES = Path(__file__).parents[1] / "data" / "samples" / "onec"


def test_months():
    assert list(months(date(2025, 11, 15), date(2026, 2, 1))) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)
    ]  # fmt: skip


def test_file_source_filters_period():
    source = FileSource(SAMPLES)
    rows = source.sales(date(2026, 8, 1), date(2026, 9, 1))
    assert rows
    assert {r["Period"][:7] for r in rows} == {"2026-08", "2026-09"}
    assert len(source.sales(None, None)) > len(rows)


class FakeOData:
    """Имитация сервера OData 1С поверх файлов выгрузки."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.sales = FileSource(SAMPLES).sales(None, None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"].startswith("Basic ")
        path = unquote(request.url.path).split("/odata/standard.odata/")[1]
        params = request.url.params
        skip, top = int(params["$skip"]), int(params["$top"])

        if path.startswith("AccumulationRegister_Продажи/Turnovers("):
            start = path.split("StartPeriod=datetime'")[1][:7]
            # Виртуальная таблица оборотов не возвращает Period
            rows = [
                {k: v for k, v in r.items() if k != "Period"}
                for r in self.sales
                if r["Period"].startswith(start)
            ]
        else:
            rows = json.loads((SAMPLES / f"{path}.json").read_text(encoding="utf-8"))["value"]
        return httpx.Response(200, json={"value": rows[skip : skip + top]})


@pytest.fixture
def fake():
    return FakeOData()


@pytest.fixture
def http_source(fake, monkeypatch):
    monkeypatch.setattr(HttpSource, "PAGE_SIZE", 10)
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return HttpSource("http://1c.example/trade/odata/standard.odata", "user", "pass", client=client)


def test_http_catalog_reads_all_pages(http_source, fake):
    rows = http_source.catalog(CONTRACTORS)
    assert len(rows) == len(FileSource(SAMPLES).catalog(CONTRACTORS))
    assert len(fake.requests) == len(rows) // 10 + 1
    assert all(r.url.params["$format"] == "json" for r in fake.requests)


def test_http_sales_requests_each_month(http_source, fake):
    rows = http_source.sales(date(2026, 8, 1), date(2026, 9, 1))
    expected = FileSource(SAMPLES).sales(date(2026, 8, 1), date(2026, 9, 1))
    assert len(rows) == len(expected)
    assert {r["Period"] for r in rows} == {"2026-08-01T00:00:00", "2026-09-01T00:00:00"}

    paths = {unquote(r.url.path) for r in fake.requests}
    assert any(
        "StartPeriod=datetime'2026-09-01T00:00:00',EndPeriod=datetime'2026-09-30T23:59:59',"
        "Dimensions='Контрагент,Номенклатура,Регион'" in p
        for p in paths
    )


def test_http_sales_requires_period(http_source):
    with pytest.raises(ValueError):
        http_source.sales(None, date(2026, 9, 1))


def test_http_margin_plan_filter(http_source, fake):
    http_source.margin_plan(date(2026, 8, 1), date(2026, 9, 1))
    assert fake.requests[0].url.params["$filter"] == (
        "Period ge datetime'2026-08-01T00:00:00' and Period le datetime'2026-09-01T00:00:00'"
    )
    assert MARGIN_PLAN in unquote(fake.requests[0].url.path)
