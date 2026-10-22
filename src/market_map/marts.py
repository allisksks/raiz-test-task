"""Пересчёт материализованных витрин (в порядке зависимостей)."""

from sqlalchemy import Engine, text

MATERIALIZED = ("marts.own_price", "marts.own_channel_factor", "marts.price_comparison")


def refresh_marts(engine: Engine) -> None:
    with engine.begin() as conn:
        for view in MATERIALIZED:
            conn.execute(text(f"refresh materialized view {view}"))


def refresh_derived(engine: Engine) -> None:
    """После загрузки: пересчитать витрины, затем сигналы (они строятся по витринам)."""
    from market_map.signals import detect_signals

    refresh_marts(engine)
    detect_signals(engine)
