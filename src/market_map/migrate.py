"""Миграции схемы и загрузка справочников."""

from importlib import resources

from sqlalchemy import Connection, Engine, text

from market_map.reference import CHANNELS, REGIONS

MIGRATIONS_TABLE = "public.schema_migrations"


def _migration_files() -> list[tuple[str, str]]:
    files = resources.files("market_map") / "migrations"
    return sorted(
        (f.name.removesuffix(".sql"), f.read_text(encoding="utf-8"))
        for f in files.iterdir()
        if f.name.endswith(".sql")
    )


def apply_migrations(conn: Connection) -> list[str]:
    """Применяет ещё не применённые миграции, возвращает их версии."""
    conn.execute(
        text(
            f"create table if not exists {MIGRATIONS_TABLE} "
            "(version text primary key, applied_at timestamptz not null default now())"
        )
    )
    applied = set(conn.execute(text(f"select version from {MIGRATIONS_TABLE}")).scalars())
    new = []
    for version, sql in _migration_files():
        if version in applied:
            continue
        # Через курсор драйвера без параметров: иначе «%» в тексте миграции считается плейсхолдером
        with conn.connection.cursor() as cursor:
            cursor.execute(sql)
        conn.execute(text(f"insert into {MIGRATIONS_TABLE} (version) values (:v)"), {"v": version})
        new.append(version)
    return new


def seed_reference(conn: Connection) -> None:
    """Загружает справочники регионов и каналов (идемпотентно)."""
    conn.execute(
        text(
            "insert into core.region (code, name, federal_district, map_code) "
            "values (:code, :name, :fd, :map_code) "
            "on conflict (code) do update set name = excluded.name, "
            "federal_district = excluded.federal_district, map_code = excluded.map_code"
        ),
        [
            {"code": r.code, "name": r.name, "fd": r.federal_district, "map_code": r.map_code}
            for r in REGIONS
        ],
    )
    conn.execute(
        text(
            "insert into core.channel (code, name) values (:code, :name) "
            "on conflict (code) do update set name = excluded.name"
        ),
        [{"code": c.code, "name": c.name} for c in CHANNELS],
    )


def migrate(engine: Engine) -> list[str]:
    with engine.begin() as conn:
        new = apply_migrations(conn)
        seed_reference(conn)
    return new
