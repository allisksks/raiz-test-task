"""Детектор сигналов о значимых изменениях на рынке.

Каждый детектор — запрос к витринам и правило с порогом из Thresholds. Результат
пересчитывается целиком: сигналы — производные данные, история хранится в core.
Тексты сигналов — для ленты на дашборде.
"""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Connection, Engine, text


@dataclass(frozen=True)
class Thresholds:
    # Изменение цены конкурента: медиана по товарам группы к среднему за 4 прошлых наблюдения
    price_change: float = 0.05
    # Разрыв цены нового конкурента с нашей, начиная с которого он отмечается как дешевле/дороже
    price_gap: float = 0.10
    # Разрыв, начиная с которого новый конкурент — критичный сигнал
    critical_price_gap: float = 0.15
    # Сдвиг ценовой позиции (медианного разрыва) за месяц, п.п.
    price_position: float = 0.05
    # Отклонение маржи от плана, п.п.
    margin_deviation: float = 0.03
    critical_margin_deviation: float = 0.05
    # Изменение маржи клиента после входа конкурента для составного сигнала, п.п.
    composite_margin_change: float = 0.01
    # Изменение оценочной доли рынка за месяц, п.п.
    market_share: float = 0.05


@dataclass
class Signal:
    signal_type: str
    severity: str
    event_date: date
    title: str
    region_code: str | None = None
    client_id: int | None = None
    competitor_id: int | None = None
    product_group_id: int | None = None
    product_id: int | None = None
    value: float | None = None
    details: dict = field(default_factory=dict)
    provenance: str = "fact"  # estimate — если сигнал построен на оценке (доли рынка)


def _pct(value: float) -> str:
    """0.253 → «25%»"""
    return f"{abs(value) * 100:.0f}%"


def _pp(value: float) -> str:
    """0.083 → «8,3 п.п.»"""
    return f"{abs(value) * 100:.1f}".replace(".", ",") + " п.п."


def _percent(value: float) -> str:
    """0.0912 → «9,1%»"""
    return f"{value * 100:.1f}".replace(".", ",") + "%"


def _cheaper(gap: float) -> str:
    return f"на {_pct(gap)} {'ниже' if gap < 0 else 'выше'}"


def _rows(conn: Connection, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _group_ids(conn: Connection) -> dict[str, int]:
    return {
        name: id_ for name, id_ in conn.execute(text("select name, id from core.product_group"))
    }


# --- Конкуренты -------------------------------------------------------------------------


def _new_competitors(conn: Connection, t: Thresholds) -> list[dict]:
    """Конкуренты, появившиеся в регионах наших клиентов после начала мониторинга."""
    return _rows(
        conn,
        """
        select p.company_id, p.company, p.channel, p.region_code, p.region, p.valid_from,
               p.registered_at, p.has_regional_warehouse, p.supply_scheme,
               (select percentile_cont(0.5) within group (order by pc.price_gap)
                from marts.price_comparison pc
                where pc.competitor_id = p.company_id and pc.region_code = p.region_code
                  and pc.comparable and pc.observed_at < p.valid_from + 28) as gap
        from marts.presence p
        where p.kind = 'competitor'
          and p.valid_from > (select min(observed_at) from core.price_observation)
          and p.region_code in (select region_code from marts.presence where kind = 'client')
        order by p.valid_from
        """,
    )


def detect_new_competitors(conn: Connection, t: Thresholds) -> list[Signal]:
    signals = []
    for c in _new_competitors(conn, t):
        gap = c["gap"]
        price = (
            f" с ценой {_cheaper(gap)} нашей" if gap is not None and abs(gap) >= t.price_gap else ""
        )
        severity = "critical" if gap is not None and gap <= -t.critical_price_gap else "warning"
        signals.append(
            Signal(
                "new_competitor",
                severity,
                c["valid_from"],
                f"В регионе {c['region']} появился новый конкурент {c['company']} "
                f"({c['channel']}){price}",
                region_code=c["region_code"],
                competitor_id=c["company_id"],
                value=gap,
                details={
                    "registered_at": c["registered_at"] and c["registered_at"].isoformat(),
                    "has_regional_warehouse": c["has_regional_warehouse"],
                    "supply_scheme": c["supply_scheme"],
                },
            )
        )
    return signals


def detect_competitor_exits(conn: Connection, t: Thresholds) -> list[Signal]:
    rows = _rows(
        conn,
        """
        select company_id, company, region_code, region, valid_to
        from marts.presence
        where kind = 'competitor' and not is_active
          and region_code in (select region_code from marts.presence where kind = 'client')
        """,
    )
    return [
        Signal(
            "competitor_exit",
            "info",
            r["valid_to"],
            f"{r['company']} ушёл из региона {r['region']}: "
            f"последнее наблюдение {r['valid_to']:%d.%m.%Y}",
            region_code=r["region_code"],
            competitor_id=r["company_id"],
        )
        for r in rows
    ]


def detect_competitor_price_changes(conn: Connection, t: Thresholds) -> list[Signal]:
    """Ступенчатое изменение цен конкурента на группу товаров.

    Цена каждого товара сравнивается со средней за 4 прошлых наблюдения. Сигнал — если
    в одну сторону за порог ушли хотя бы половина товаров группы (и не меньше двух):
    случайные изменения отдельных позиций сигналом не считаются. Сигнал ставится в неделю,
    когда изменение началось; следующие недели того же изменения не повторяются,
    а разворот (например, конец акции) — новый сигнал.
    """
    rows = _rows(
        conn,
        """
        with changes as (
            select observed_at, region_code, region, competitor_id, competitor, product_group,
                   competitor_price / avg(competitor_price) over w - 1 as change,
                   count(*) over w as history
            from marts.price_comparison
            -- история — по товару конкурента: у одного нашего товара может быть и
            -- идентичный товар, и аналог у того же конкурента
            window w as (partition by competitor_id, region_code, own_product_id,
                                      competitor_product
                         order by observed_at rows between 4 preceding and 1 preceding)
        )
        select observed_at, region_code, region, competitor_id, competitor, product_group,
               percentile_cont(0.5) within group (order by change) as change,
               count(*) as products,
               count(*) filter (where change <= -:th) as down,
               count(*) filter (where change >= :th) as up
        from changes
        where history = 4
        group by 1, 2, 3, 4, 5, 6
        order by competitor_id, region_code, product_group, observed_at
        """,
        th=t.price_change,
    )
    group_ids = _group_ids(conn)
    signals, previous = [], {}
    for r in rows:
        key = (r["competitor_id"], r["region_code"], r["product_group"])
        enough = max(2, (r["products"] + 1) // 2)
        direction = -1 if r["down"] >= enough else 1 if r["up"] >= enough else 0
        if direction and direction != previous.get(key):
            moved = [c for c in (r["down"], r["up"])][direction > 0]
            signals.append(
                Signal(
                    "competitor_price_change",
                    "warning" if direction < 0 else "info",
                    r["observed_at"],
                    f"{r['competitor']} {'снизил' if direction < 0 else 'повысил'} цены "
                    f"на «{r['product_group']}» в регионе {r['region']} на {_pct(r['change'])}",
                    region_code=r["region_code"],
                    competitor_id=r["competitor_id"],
                    product_group_id=group_ids[r["product_group"]],
                    value=round(r["change"], 4),
                    details={"products": r["products"], "products_changed": moved},
                )
            )
        previous[key] = direction
    return signals


def detect_assortment_changes(conn: Connection, t: Thresholds) -> list[Signal]:
    """Новые товары у конкурентов, которые уже работали в регионе: аналоги — товары-заменители."""
    rows = _rows(
        conn,
        """
        with first_seen as (
            select competitor_id, competitor, region_code, region, own_product_id, own_product,
                   competitor_product, competitor_brand, match_type,
                   min(observed_at) as first_seen
            from marts.price_comparison
            group by 1, 2, 3, 4, 5, 6, 7, 8, 9
        ),
        presence as (
            select company_id, region_code, valid_from from marts.presence where kind = 'competitor'
        )
        select f.*, pr.valid_from, p.group_id,
               (select pc.price_gap from marts.price_comparison pc
                where pc.competitor_id = f.competitor_id and pc.region_code = f.region_code
                  and pc.own_product_id = f.own_product_id
                  and pc.competitor_product = f.competitor_product and pc.comparable
                order by pc.observed_at limit 1) as gap
        from first_seen f
        join presence pr on pr.company_id = f.competitor_id and pr.region_code = f.region_code
        join core.product p on p.id = f.own_product_id
        where f.first_seen > pr.valid_from + 28
          and f.first_seen > (select min(observed_at) from core.price_observation) + 28
        order by f.first_seen
        """,
    )
    signals = []
    for r in rows:
        substitute = r["match_type"] == "similar"
        gap = r["gap"]
        price = f" {_cheaper(gap)} нашего" if gap is not None else ""
        if substitute:
            title = (
                f"{r['competitor']} в регионе {r['region']} начал продавать аналог "
                f"«{r['competitor_product']}»{price} товара «{r['own_product']}»"
            )
        else:
            title = (
                f"{r['competitor']} расширил ассортимент в регионе {r['region']}: "
                f"«{r['competitor_product']}»"
            )
        signals.append(
            Signal(
                "new_substitute" if substitute else "assortment_change",
                "warning" if substitute and gap is not None and gap < 0 else "info",
                r["first_seen"],
                title,
                region_code=r["region_code"],
                competitor_id=r["competitor_id"],
                product_group_id=r["group_id"],
                product_id=r["own_product_id"],
                value=gap,
                details={"brand": r["competitor_brand"]},
            )
        )
    return signals


# --- Наша позиция -----------------------------------------------------------------------


def detect_price_position_changes(conn: Connection, t: Thresholds) -> list[Signal]:
    """Сдвиг ценовой позиции за месяц: медианный разрыв цен конкурентов с нашей.

    Месяцы сравниваются по одним и тем же парам «конкурент × товар», чтобы пропуски
    наблюдений и смена состава не выглядели как изменение позиции. Пары новых
    конкурентов в первый месяц не участвуют — для них есть отдельный сигнал.
    """
    rows = _rows(
        conn,
        """
        with pairs as (
            select month, region_code, region, product_group, competitor_id, own_product_id,
                   avg(price_gap) as gap
            from marts.price_comparison
            where comparable
            group by 1, 2, 3, 4, 5, 6
        )
        select cur.month, cur.region_code, cur.region, cur.product_group, g.id as group_id,
               percentile_cont(0.5) within group (order by cur.gap) as gap,
               percentile_cont(0.5) within group (order by prev.gap) as previous_gap,
               count(*) as pairs
        from pairs cur
        join pairs prev
            on prev.month = (cur.month - interval '1 month')::date
           and prev.region_code = cur.region_code and prev.competitor_id = cur.competitor_id
           and prev.own_product_id = cur.own_product_id
        join core.product_group g on g.name = cur.product_group
        group by 1, 2, 3, 4, 5
        """,
    )
    signals = []
    for r in rows:
        if abs(r["gap"] - r["previous_gap"]) < t.price_position:
            continue
        worse = r["gap"] < r["previous_gap"]  # конкуренты стали относительно дешевле
        signals.append(
            Signal(
                "price_position_change",
                "warning" if worse else "info",
                r["month"],
                f"Ценовая позиция по «{r['product_group']}» в регионе {r['region']} "
                f"{'ухудшилась' if worse else 'улучшилась'}: конкуренты {_cheaper(r['gap'])} "
                f"нашей цены (месяцем ранее — {_cheaper(r['previous_gap'])})",
                region_code=r["region_code"],
                product_group_id=r["group_id"],
                value=round(r["gap"] - r["previous_gap"], 4),
                details={
                    "gap": round(r["gap"], 4),
                    "previous_gap": round(r["previous_gap"], 4),
                    "pairs": r["pairs"],
                },
            )
        )
    return signals


def detect_margin_deviations(conn: Connection, t: Thresholds) -> list[Signal]:
    """Отклонение маржи клиента от плана. Один сигнал на эпизод (подряд идущие месяцы
    с отклонением) — в первый месяц, со значением худшего месяца эпизода."""
    rows = _rows(
        conn,
        """
        select m.month, m.region_code, m.region, m.client_id, m.client, m.product_group,
               g.id as group_id, m.margin_pct, m.plan_margin_pct, m.margin_pct_deviation
        from marts.margin_plan_fact m
        join core.product_group g on g.name = m.product_group
        where m.margin_pct_deviation is not null
        order by m.client_id, m.product_group, m.month
        """,
    )
    episodes: list[list[dict]] = []
    current: dict[tuple, list[dict]] = {}
    for r in rows:
        key = (r["client_id"], r["product_group"])
        if abs(r["margin_pct_deviation"]) < t.margin_deviation:
            current.pop(key, None)
        elif key in current:
            current[key].append(r)
        else:
            current[key] = [r]
            episodes.append(current[key])
    signals = []
    for months in episodes:
        first = months[0]
        worst = max(months, key=lambda m: abs(m["margin_pct_deviation"]))
        deviation = float(worst["margin_pct_deviation"])
        below = deviation < 0
        signals.append(
            Signal(
                "margin_plan_deviation",
                "critical" if deviation <= -t.critical_margin_deviation else "warning",
                first["month"],
                f"Маржа клиента {first['client']} по «{first['product_group']}» "
                f"{'ниже' if below else 'выше'} плана на {_pp(deviation)} "
                f"({_percent(worst['margin_pct'])} при плане {_percent(worst['plan_margin_pct'])})",
                region_code=first["region_code"],
                client_id=first["client_id"],
                product_group_id=first["group_id"],
                value=round(deviation, 4),
                details={"months": [m["month"].isoformat() for m in months]},
            )
        )
    return signals


def detect_market_share_changes(conn: Connection, t: Thresholds) -> list[Signal]:
    rows = _rows(
        conn,
        """
        select s.month, s.region_code, s.region, s.product_group, g.id as group_id, s.share,
               lag(s.share) over (partition by s.region_code, s.product_group order by s.month)
                   as previous_share
        from marts.market_share s
        join core.product_group g on g.name = s.product_group
        where s.player_kind = 'own'
        """,
    )
    signals = []
    for r in rows:
        if r["previous_share"] is None:
            continue
        change = float(r["share"] - r["previous_share"])
        if abs(change) < t.market_share:
            continue
        signals.append(
            Signal(
                "market_share_change",
                "warning" if change < 0 else "info",
                r["month"],
                f"Оценочная доля рынка по «{r['product_group']}» в регионе {r['region']} "
                f"{'снизилась' if change < 0 else 'выросла'} на {_pp(change)}: "
                f"{r['previous_share'] * 100:.0f}% → {r['share'] * 100:.0f}%",
                region_code=r["region_code"],
                product_group_id=r["group_id"],
                value=round(change, 4),
                details={"share": float(r["share"]), "previous_share": float(r["previous_share"])},
                provenance="estimate",
            )
        )
    return signals


# --- Составной сигнал -------------------------------------------------------------------


def detect_composite(conn: Connection, t: Thresholds) -> list[Signal]:
    """«В регионе появились новые конкуренты с ценой на x% ниже/выше цены клиента.
    Одновременно маржа клиента снизилась/повысилась на y п.п.»

    Для каждого клиента в регионе нового конкурента и каждой группы товаров, где они
    пересекаются: цена конкурента сравнивается с ценой этого клиента, маржа — последний
    месяц против месяца до появления конкурента.
    """
    signals = []
    for c in _new_competitors(conn, t):
        rows = _rows(
            conn,
            """
            with entry as (select date_trunc('month', cast(:entry as date))::date as month),
            client_price as (
                select s.client_id, s.product_id, p.group_id, s.period,
                       sum(s.revenue) / nullif(sum(s.quantity), 0) as price
                from core.sales_fact s
                join core.product p on p.id = s.product_id
                where s.region_code = :region
                group by 1, 2, 3, 4
            ),
            last_month as (
                select max(period) as month from core.sales_fact
            ),
            gaps as (
                select cp.client_id, cp.group_id,
                       percentile_cont(0.5) within group (
                           order by pc.competitor_price / cp.price - 1) as gap
                from client_price cp
                join marts.price_comparison pc
                    on pc.own_product_id = cp.product_id and pc.region_code = :region
                   and pc.competitor_id = :competitor
                   and date_trunc('month', pc.observed_at) = cp.period
                where cp.period = (select month from last_month)
                group by 1, 2
            ),
            margins as (
                select s.client_id, p.group_id,
                       sum(s.margin) filter (where s.period = (select month from last_month))
                         / nullif(sum(s.revenue) filter (
                               where s.period = (select month from last_month)), 0) as after,
                       sum(s.margin) filter (where s.period = (select month from entry)
                                                 - interval '1 month')
                         / nullif(sum(s.revenue) filter (
                               where s.period = (select month from entry)
                                                - interval '1 month'), 0) as before
                from core.sales_fact s
                join core.product p on p.id = s.product_id
                where s.region_code = :region
                group by 1, 2
            )
            select g.client_id, c.name as client, g.group_id, pg.name as product_group,
                   g.gap, m.before, m.after, (select month from last_month) as month
            from gaps g
            join margins m using (client_id, group_id)
            join core.company c on c.id = g.client_id
            join core.product_group pg on pg.id = g.group_id
            order by c.name, pg.name
            """,
            entry=c["valid_from"],
            region=c["region_code"],
            competitor=c["company_id"],
        )
        for r in rows:
            if r["before"] is None or r["after"] is None:
                continue
            gap, change = float(r["gap"]), float(r["after"] - r["before"])
            if abs(gap) < t.price_gap or abs(change) < t.composite_margin_change:
                continue
            signals.append(
                Signal(
                    "composite",
                    "critical" if gap < 0 and change < 0 else "warning",
                    c["valid_from"],
                    f"В регионе {c['region']} появился новый конкурент {c['company']} "
                    f"с ценой {_cheaper(gap)} цены клиента {r['client']} "
                    f"по «{r['product_group']}». Одновременно маржа клиента "
                    f"{'снизилась' if change < 0 else 'повысилась'} на {_pp(change)} "
                    f"(с {_percent(r['before'])} до {_percent(r['after'])})",
                    region_code=c["region_code"],
                    client_id=r["client_id"],
                    competitor_id=c["company_id"],
                    product_group_id=r["group_id"],
                    value=round(gap, 4),
                    details={
                        "price_gap": round(gap, 4),
                        "margin_before": round(float(r["before"]), 4),
                        "margin_after": round(float(r["after"]), 4),
                        "margin_change": round(change, 4),
                        "margin_month": r["month"].isoformat(),
                    },
                )
            )
    return signals


DETECTORS = (
    detect_composite,
    detect_new_competitors,
    detect_competitor_exits,
    detect_competitor_price_changes,
    detect_assortment_changes,
    detect_price_position_changes,
    detect_margin_deviations,
    detect_market_share_changes,
)


def detect_signals(engine: Engine, thresholds: Thresholds | None = None) -> dict[str, int]:
    """Пересчитывает все сигналы; возвращает их число по типам."""
    t = thresholds or Thresholds()
    with engine.begin() as conn:
        signals = [s for detector in DETECTORS for s in detector(conn, t)]
        conn.execute(text("delete from core.signal"))
        if signals:
            conn.execute(
                text(
                    "insert into core.signal (signal_type, severity, event_date, region_code, "
                    "client_id, competitor_id, product_group_id, product_id, value, title, "
                    "details, provenance) values (:signal_type, :severity, :event_date, "
                    ":region_code, :client_id, :competitor_id, :product_group_id, :product_id, "
                    ":value, :title, :details, cast(:provenance as core.provenance))"
                ),
                [
                    {**s.__dict__, "details": json.dumps(s.details, ensure_ascii=False)}
                    for s in signals
                ],
            )
    counts: dict[str, int] = defaultdict(int)
    for s in signals:
        counts[s.signal_type] += 1
    return dict(counts)
