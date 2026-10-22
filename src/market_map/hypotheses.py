"""Движок гипотез: «за счёт чего конкурент может продавать дешевле?»

Для каждого ценового сигнала собирается контекст (цены, наша закупочная цена из 1С,
признаки конкурента из подготовленной базы) и проверяется набор гипотез из ТЗ. Каждая
гипотеза — список доводов с весами; оценка = сумма весов подтвердившихся доводов (не
больше 1). Доводы помечены как факт или оценка, сама гипотеза — всегда гипотеза.

Там же строится ценовая архитектура: из чего может складываться цена конкурента.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from sqlalchemy import Connection, Engine, text

PRICE_SIGNALS = ("composite", "new_competitor", "competitor_price_change", "new_substitute")
DIRECT_SUPPLY_WORDS = ("прям", "завод", "производител")
# После снижения цены ждём столько, прежде чем исключать временную акцию
PROMO_WINDOW = timedelta(weeks=6)
MIN_CONFIDENT_SCORE = 0.4
# Цена ниже/около нашей закупочной: порог, за которым разница не шум
COST_TOLERANCE = 0.03


@dataclass
class Evidence:
    text: str
    weight: float
    present: bool
    kind: str = "fact"  # fact | estimate


@dataclass
class Hypothesis:
    hypothesis_type: str
    title: str
    evidence: list[Evidence]

    @property
    def score(self) -> float:
        return round(min(1.0, sum(e.weight for e in self.evidence if e.present)), 2)

    @property
    def rationale(self) -> str:
        found = [e.text for e in self.evidence if e.present]
        return "; ".join(found) if found else "подтверждающих данных нет"


@dataclass
class Context:
    """Всё, что известно о конкуренте и ценах на момент сигнала."""

    signal: dict
    competitor: dict
    prices: list[dict]  # по товарам: цена конкурента, наша цена, наша закупочная цена
    regional_premium: float | None  # наша цена в регионе к средней по стране в том же канале − 1
    reversed_within: int | None  # через сколько дней цена вернулась (для снижения цены)
    days_observed_after: int  # сколько дней наблюдений после сигнала
    as_of: date
    medians: dict = field(default_factory=dict)

    def __post_init__(self):
        def med(key, match_type=None):
            values = [
                p[key]
                for p in self.prices
                if p.get(key) is not None and match_type in (None, p["match_type"])
            ]
            return median(values) if values else None

        self.medians = {
            "gap": med("gap"),
            "to_cost": med("to_cost"),
            # С нашей закупочной ценой честно сравнивать только идентичные товары:
            # аналог конкурент покупает у другого производителя
            "to_cost_identical": med("to_cost", "identical"),
            "to_cost_analog": med("to_cost", "similar"),
            "competitor_price": med("competitor_price"),
            "own_price": med("own_price"),
            "own_cost": med("own_cost"),
            "markup": med("markup"),
            "implied_cost_gap": med("implied_cost_gap"),
        }

    @property
    def scheme(self) -> str:
        return (self.competitor["supply_scheme"] or "").lower()

    @property
    def identical_share(self) -> float:
        if not self.prices:
            return 0.0
        return sum(p["match_type"] == "identical" for p in self.prices) / len(self.prices)

    @property
    def is_new_entity(self) -> bool:
        registered, entry = self.competitor["registered_at"], self.competitor["entry_date"]
        return bool(registered and entry and (entry - registered).days < 365)


def _rows(conn: Connection, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _window(signal: dict, competitor: dict) -> tuple[date, date]:
    """Период цен, по которому судим о сигнале."""
    if signal["signal_type"] == "competitor_price_change":
        return signal["event_date"], signal["event_date"] + timedelta(days=6)
    start = signal["event_date"]
    if signal["signal_type"] in ("new_competitor", "composite"):
        start = competitor["entry_date"] or start
    return start, start + timedelta(days=27)


def _context(conn: Connection, signal: dict, as_of: date) -> Context | None:
    competitor = _rows(
        conn,
        """
        select c.id, c.name, c.channel_code, c.registered_at, c.has_regional_warehouse,
               c.supply_scheme, cr.valid_from as entry_date,
               (select count(distinct region_code) from core.company_region
                where company_id = c.id and valid_to is null) as regions,
               (select count(*) from core.product where company_id = c.id) as assortment
        from core.company c
        left join core.company_region cr on cr.company_id = c.id and cr.region_code = :region
        where c.id = :competitor
        order by cr.valid_from desc nulls last
        limit 1
        """,
        competitor=signal["competitor_id"],
        region=signal["region_code"],
    )
    if not competitor:
        return None
    competitor = competitor[0]
    start, end = _window(signal, competitor)
    only_analogs = signal["signal_type"] == "new_substitute"

    prices = _rows(
        conn,
        """
        with costs as (
            select period, region_code, product_id, sum(cost) / nullif(sum(quantity), 0) as cost
            from core.sales_fact group by 1, 2, 3
        )
        select pc.own_product_id, pc.match_type,
               avg(pc.competitor_price) as competitor_price,
               avg(pc.own_price) as own_price,
               (select c.cost from costs c
                where c.region_code = pc.region_code and c.product_id = pc.own_product_id
                  and c.period <= :end order by c.period desc limit 1) as own_cost
        from marts.price_comparison pc
        join core.product p on p.id = pc.own_product_id
        where pc.competitor_id = :competitor and pc.region_code = :region
          and pc.observed_at between :start and :end and pc.comparable
          and (cast(:group_id as bigint) is null or p.group_id = :group_id)
          and (cast(:product_id as bigint) is null or pc.own_product_id = :product_id)
          and (not :only_analogs or pc.match_type = 'similar')
        group by pc.region_code, pc.own_product_id, pc.match_type
        """,
        competitor=signal["competitor_id"],
        region=signal["region_code"],
        start=start,
        end=end,
        group_id=signal["product_group_id"],
        product_id=signal["product_id"] if only_analogs else None,
        only_analogs=only_analogs,
    )
    for p in prices:
        for key in ("competitor_price", "own_price", "own_cost"):
            p[key] = float(p[key]) if p[key] is not None else None
        cp, own, cost = p["competitor_price"], p["own_price"], p["own_cost"]
        p["gap"] = cp / own - 1 if own else None
        p["to_cost"] = cp / cost - 1 if cost else None
        p["markup"] = own / cost - 1 if own and cost else None
        # Закупочная цена конкурента, если его наценка такая же, как наша
        p["implied_cost_gap"] = (
            cp / (1 + p["markup"]) / cost - 1 if p["markup"] is not None else None
        )

    premium = conn.execute(
        text(
            """
            -- внутри одного канала: иначе на результат влияет состав клиентов региона
            select percentile_cont(0.5) within group (order by r.price / n.price - 1)
            from marts.own_price r
            join marts.own_price n
                on n.month = r.month and n.product_id = r.product_id
               and n.channel_code = r.channel_code and n.national and not n.all_channels
            where not r.national and not r.all_channels and r.region_code = :region
              and r.month = (select max(month) from marts.own_price where month <= :start)
            """
        ),
        {"region": signal["region_code"], "start": start},
    ).scalar()

    reversed_within = None
    if signal["signal_type"] == "competitor_price_change" and signal["value"] < 0:
        reversal = conn.execute(
            text(
                """
                select min(event_date) from core.signal
                where signal_type = 'competitor_price_change' and value > 0
                  and competitor_id = :competitor and region_code = :region
                  and product_group_id is not distinct from :group_id
                  and event_date > :date and event_date <= :until
                """
            ),
            {
                "competitor": signal["competitor_id"],
                "region": signal["region_code"],
                "group_id": signal["product_group_id"],
                "date": signal["event_date"],
                "until": signal["event_date"] + PROMO_WINDOW,
            },
        ).scalar()
        if reversal:
            reversed_within = (reversal - signal["event_date"]).days

    return Context(
        signal=signal,
        competitor=competitor,
        prices=prices,
        regional_premium=float(premium) if premium is not None else None,
        reversed_within=reversed_within,
        days_observed_after=(as_of - signal["event_date"]).days,
        as_of=as_of,
    )


# --- Гипотезы ---------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{abs(value) * 100:.0f}%"


def build_hypotheses(ctx: Context) -> list[Hypothesis]:
    m, c, s = ctx.medians, ctx.competitor, ctx.signal
    promo = ctx.reversed_within is not None
    # Низкая цена во время акции объясняется акцией, а не закупочными условиями
    to_cost = None if promo else m["to_cost_identical"]
    below_cost = to_cost is not None and to_cost < -COST_TOLERANCE
    near_cost = to_cost is not None and abs(to_cost) <= COST_TOLERANCE
    gap = m["gap"]
    is_new = s["signal_type"] in ("new_competitor", "composite")
    direct = any(word in ctx.scheme for word in DIRECT_SUPPLY_WORDS)
    analogs = ctx.prices and ctx.identical_share < 0.5
    if below_cost:
        cost_text = f"цена конкурента на те же товары ниже нашей закупочной на {_pct(to_cost)}"
    elif near_cost:
        cost_text = "цена конкурента на те же товары примерно равна нашей закупочной"
    else:
        cost_text = ""
    analog_cost = m["to_cost_analog"]
    promo_cost = m["to_cost_identical"] if promo else None

    hypotheses = [
        Hypothesis(
            "direct_purchase",
            "Прямые закупки у производителя",
            [
                Evidence(
                    f"схема поставок по данным мониторинга: «{c['supply_scheme']}»", 0.35, direct
                ),
                Evidence(f"{cost_text}: при наших условиях закупки это убыточно", 0.35, below_cost),
                Evidence(f"{cost_text}: работа почти без наценки", 0.2, near_cost),
                Evidence(
                    f"продаёт те же товары, что и мы ({_pct(ctx.identical_share)} позиций)",
                    0.15,
                    ctx.identical_share >= 0.5,
                ),
            ],
        ),
        Hypothesis(
            "volume_purchase",
            "Большие объёмы закупок, более выгодные закупочные цены",
            [
                Evidence(
                    f"схема поставок: «{c['supply_scheme']}»", 0.3, "централизован" in ctx.scheme
                ),
                Evidence(f"работает в {c['regions']} регионах", 0.2, c["regions"] >= 3),
                Evidence(
                    f"широкий ассортимент: {c['assortment']} позиций", 0.15, c["assortment"] >= 15
                ),
                Evidence(cost_text, 0.2, below_cost or near_cost),
            ],
        ),
        Hypothesis(
            "short_chain",
            "Новая дистрибьюторская цепочка, меньше посредников",
            [
                Evidence(
                    f"юрлицо зарегистрировано {c['registered_at']:%d.%m.%Y}, "
                    "меньше чем за год до выхода в регион"
                    if c["registered_at"]
                    else "",
                    0.3,
                    ctx.is_new_entity,
                ),
                Evidence(
                    "работает как дистрибьютор/оптовик",
                    0.2,
                    c["channel_code"] in ("distribution", "wholesale"),
                ),
                Evidence("поставки напрямую, минуя федеральных дистрибьюторов", 0.2, direct),
                Evidence("недавно вышел в регион", 0.15, is_new),
            ],
        ),
        Hypothesis(
            "regional_warehouse",
            "Региональный склад, более дешёвая логистика",
            [
                Evidence(
                    "по данным мониторинга есть склад в регионе",
                    0.4,
                    bool(c["has_regional_warehouse"]),
                ),
                Evidence(
                    f"наша цена в регионе выше средней по стране на {_pct(ctx.regional_premium)} — "
                    "у нас дорогая доставка"
                    if ctx.regional_premium is not None
                    else "",
                    0.3,
                    ctx.regional_premium is not None and ctx.regional_premium >= 0.02,
                    "estimate",
                ),
            ],
        ),
        Hypothesis(
            "temporary_promo",
            "Временное снижение цены (акция, временно сниженная маржа)",
            [
                Evidence(
                    f"цена вернулась через {ctx.reversed_within} дн."
                    if ctx.reversed_within
                    else "",
                    0.7,
                    ctx.reversed_within is not None,
                ),
                Evidence(
                    f"во время снижения цена ниже нашей закупочной на {_pct(promo_cost)} — "
                    "долго так не торгуют"
                    if promo_cost is not None
                    else "",
                    0.15,
                    promo_cost is not None and promo_cost < -COST_TOLERANCE,
                ),
                Evidence(
                    "после снижения прошло меньше 6 недель — акцию нельзя исключить",
                    0.2,
                    ctx.reversed_within is None and ctx.days_observed_after < PROMO_WINDOW.days,
                    "estimate",
                ),
            ],
        ),
        Hypothesis(
            "analog_cost",
            "Дешёвый аналог, другая структура затрат",
            [
                Evidence("продаёт в основном аналоги других брендов", 0.4, bool(analogs)),
                Evidence(f"схема поставок: «{c['supply_scheme']}»", 0.3, "аналог" in ctx.scheme),
                Evidence(
                    "сигнал о новом товаре-заменителе", 0.2, s["signal_type"] == "new_substitute"
                ),
                Evidence(
                    f"аналог дешевле нашей закупочной цены оригинала на {_pct(analog_cost)}"
                    if analog_cost is not None
                    else "",
                    0.15,
                    analog_cost is not None and analog_cost < -COST_TOLERANCE,
                ),
            ],
        ),
    ]
    if is_new:
        hypotheses.append(
            Hypothesis(
                "aggressive_entry",
                "Агрессивный выход на рынок: цена ниже рынка ради доли",
                [
                    Evidence("недавно вышел в регион", 0.3, True),
                    Evidence(
                        f"цена ниже нашей на {_pct(gap)}" if gap is not None else "",
                        0.3,
                        gap is not None and gap <= -0.15,
                    ),
                    Evidence(f"{cost_text} — похоже на инвестиции в долю", 0.15, below_cost),
                    Evidence("новое юрлицо", 0.1, ctx.is_new_entity),
                ],
            )
        )
    # Только гипотезы, у которых есть хотя бы один довод
    found = sorted((h for h in hypotheses if h.score > 0), key=lambda h: -h.score)
    if not found or found[0].score < MIN_CONFIDENT_SCORE:
        found.append(
            Hypothesis(
                "insufficient_data",
                "Другие факторы: данных для уверенного вывода недостаточно",
                [
                    Evidence(
                        "нужны данные о закупочных ценах, логистике или условиях конкурента",
                        0.3,
                        True,
                        "estimate",
                    )
                ],
            )
        )
    return found


def build_price_breakdown(ctx: Context) -> list[tuple]:
    """(шаг, компонент, значение, единица, происхождение, пояснение)"""
    m = ctx.medians
    if m["own_cost"] is None or m["competitor_price"] is None or m["own_price"] is None:
        return []
    n = len(ctx.prices)
    note = f"медиана по {n} товарам" if n > 1 else None
    steps = [
        ("Наша закупочная цена", m["own_cost"], "rub", "fact", note or "из 1С"),
        ("Наша цена продажи", m["own_price"], "rub", "fact", note or "из 1С"),
        ("Наша наценка", m["markup"], "share", "fact", "цена продажи / закупочная − 1"),
        ("Цена конкурента", m["competitor_price"], "rub", "fact", note or "мониторинг цен"),
        ("Разрыв с нашей ценой", m["gap"], "share", "fact", None),
        ("Цена конкурента к нашей закупочной", m["to_cost"], "share", "fact",
         "отрицательное значение — конкурент продаёт дешевле, чем мы покупаем"),
        ("Закупочная цена конкурента при нашей наценке",
         m["competitor_price"] / (1 + m["markup"]) if m["markup"] is not None else None,
         "rub", "estimate", "если конкурент закладывает такую же наценку, как мы"),
        ("Разница с нашей закупочной ценой", m["implied_cost_gap"], "share", "estimate",
         "насколько дешевле конкурент должен закупать при нашей наценке"),
    ]  # fmt: skip
    return [
        (i, component, round(value, 4), unit, provenance, comment)
        for i, (component, value, unit, provenance, comment) in enumerate(steps, start=1)
        if value is not None
    ]


def generate_hypotheses(engine: Engine) -> dict[str, int]:
    """Пересчитывает гипотезы и ценовую архитектуру по всем ценовым сигналам."""
    with engine.begin() as conn:
        as_of = conn.execute(text("select as_of_date from marts.as_of")).scalar()
        signals = _rows(
            conn,
            "select * from core.signal where signal_type = any(:types) "
            "and competitor_id is not null and (signal_type <> 'competitor_price_change' "
            "or value < 0) order by id",
            types=list(PRICE_SIGNALS),
        )
        conn.execute(text("delete from core.hypothesis"))
        conn.execute(text("delete from core.price_breakdown"))
        stats = {"signals": 0, "hypotheses": 0, "price_breakdowns": 0}
        for signal in signals:
            ctx = _context(conn, signal, as_of)
            if ctx is None:
                continue
            hypotheses = build_hypotheses(ctx)
            conn.execute(
                text(
                    "insert into core.hypothesis "
                    "(signal_id, hypothesis_type, title, score, rationale, evidence) "
                    "values (:s, :t, :title, :score, :rationale, :evidence)"
                ),
                [
                    {
                        "s": signal["id"],
                        "t": h.hypothesis_type,
                        "title": h.title,
                        "score": h.score,
                        "rationale": h.rationale,
                        "evidence": json.dumps(
                            [e.__dict__ for e in h.evidence if e.text], ensure_ascii=False
                        ),
                    }
                    for h in hypotheses
                ],
            )
            breakdown = build_price_breakdown(ctx)
            if breakdown:
                conn.execute(
                    text(
                        "insert into core.price_breakdown "
                        "(signal_id, step, component, value, unit, provenance, note) "
                        "values (:s, :step, :c, :v, :u, cast(:p as core.provenance), :n)"
                    ),
                    [
                        {"s": signal["id"], "step": st, "c": c, "v": v, "u": u, "p": p, "n": n}
                        for st, c, v, u, p, n in breakdown
                    ],
                )
            stats["signals"] += 1
            stats["hypotheses"] += len(hypotheses)
            stats["price_breakdowns"] += bool(breakdown)
    return stats
