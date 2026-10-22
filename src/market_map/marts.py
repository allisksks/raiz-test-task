"""Пересчёт материализованных витрин (в порядке зависимостей)."""

from sqlalchemy import Engine, text

MATERIALIZED = ("marts.own_price", "marts.own_channel_factor", "marts.price_comparison")


def refresh_marts(engine: Engine) -> None:
    with engine.begin() as conn:
        for view in MATERIALIZED:
            conn.execute(text(f"refresh materialized view {view}"))
