"""Подключение к хранилищу данных."""

from sqlalchemy import Engine, create_engine, text

from market_map.config import Settings, get_settings

# Слои хранилища: сырые загрузки → нормализованное ядро → витрины для BI.
SCHEMAS = ("staging", "core", "marts")


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True)


def missing_schemas(engine: Engine) -> list[str]:
    """Возвращает схемы из SCHEMAS, которых нет в базе."""
    with engine.connect() as conn:
        existing = set(
            conn.execute(text("select schema_name from information_schema.schemata")).scalars()
        )
    return [s for s in SCHEMAS if s not in existing]
