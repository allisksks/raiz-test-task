"""Сценарии рыночных изменений, заложенные в тестовые данные.

Каждый сценарий меняет цены конкурента (подготовленная база) и, при необходимости,
продажи своей компании (1С), чтобы детектор сигналов и движок гипотез было на чём
проверять. Описание и ожидаемые сигналы — docs/test-data.md.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class SalesEffect:
    """Влияние на продажи своей компании в регионе по группе товаров."""

    region: str
    groups: tuple[str, ...]
    channels: tuple[str, ...] | None  # None — все каналы клиентов
    # месяц (YYYY-MM) → (множитель цены, множитель объёма)
    by_month: dict[str, tuple[float, float]]


@dataclass(frozen=True)
class Scenario:
    code: str
    title: str
    competitor: str
    region: str
    # Появление в регионе / уход из региона
    enters_at: date | None = None
    exits_at: date | None = None
    # Изменение цены конкурента с даты (и до даты, если акция): множитель
    price_change: float | None = None
    price_change_from: date | None = None
    price_change_to: date | None = None
    price_change_groups: tuple[str, ...] | None = None
    # Уровень цен в регионе вместо уровня конкурента по умолчанию
    region_price_level: float | None = None
    # Новые товары-заменители: (артикул своего товара, дата появления, уровень цены)
    substitutes: tuple[tuple[str, date, float], ...] = ()
    sales_effects: tuple[SalesEffect, ...] = field(default=())


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        code="S1",
        title="Новый конкурент в Новосибирске с ценой на 25% ниже, маржа клиентов падает",
        competitor="C-13",
        region="RU-NVS",
        enters_at=date(2026, 7, 6),
        sales_effects=(
            SalesEffect(
                region="RU-NVS",
                groups=("Сухие смеси", "Клеи и затирки"),
                channels=("wholesale", "distribution", "construction"),
                by_month={
                    "2026-07": (0.97, 0.90),
                    "2026-08": (0.94, 0.80),
                    "2026-09": (0.92, 0.72),
                },
            ),
        ),
    ),
    Scenario(
        code="S2",
        title="Конкурент в Краснодаре поднял цены на гидроизоляцию на 12%",
        competitor="C-08",
        region="RU-KDA",
        price_change=1.12,
        price_change_from=date(2026, 9, 7),
        price_change_groups=("Гидроизоляция",),
        sales_effects=(SalesEffect("RU-KDA", ("Гидроизоляция",), None, {"2026-09": (1.0, 1.12)}),),
    ),
    Scenario(
        code="S3",
        title="В Екатеринбурге появились дешёвые аналоги гидроизоляции (товар-заменитель)",
        competitor="C-14",
        region="RU-SVE",
        substitutes=(
            ("GS-302", date(2026, 8, 17), 0.82),
            ("GS-305", date(2026, 8, 17), 0.82),
        ),
        sales_effects=(SalesEffect("RU-SVE", ("Гидроизоляция",), None, {"2026-09": (1.0, 0.90)}),),
    ),
    Scenario(
        code="S4",
        title="Временная акция −20% на утеплители у конкурента в Казани",
        competitor="C-05",
        region="RU-TA",
        price_change=0.80,
        price_change_from=date(2026, 8, 3),
        price_change_to=date(2026, 8, 24),
        price_change_groups=("Утеплители",),
        sales_effects=(SalesEffect("RU-TA", ("Утеплители",), None, {"2026-08": (1.0, 0.93)}),),
    ),
    Scenario(
        code="S5",
        title="Конкурент ушёл из Москвы",
        competitor="C-11",
        region="RU-MOW",
        exits_at=date(2026, 6, 29),
        sales_effects=(
            SalesEffect(
                "RU-MOW",
                ("Сухие смеси", "Клеи и затирки", "Гидроизоляция", "Грунтовки", "Утеплители"),
                ("retail", "diy"),
                {"2026-07": (1.0, 1.06), "2026-08": (1.0, 1.06), "2026-09": (1.0, 1.06)},
            ),
        ),
    ),
    Scenario(
        code="S6",
        title="Федеральная DIY-сеть агрессивно заходит в Самару",
        competitor="C-15",
        region="RU-SAM",
        enters_at=date(2026, 9, 14),
        region_price_level=0.86,
        sales_effects=(
            SalesEffect(
                "RU-SAM",
                ("Сухие смеси", "Клеи и затирки", "Гидроизоляция", "Грунтовки", "Утеплители"),
                ("retail", "construction"),
                {"2026-09": (0.98, 0.92)},
            ),
        ),
    ),
)
