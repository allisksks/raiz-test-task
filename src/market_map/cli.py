"""Командная строка: `python -m market_map <команда>`."""

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from market_map.config import get_settings
from market_map.db import get_engine, missing_schemas
from market_map.migrate import migrate


def check(args: argparse.Namespace) -> int:
    """Проверяет подключение к базе и наличие слоёв хранилища."""
    missing = missing_schemas(get_engine())
    if missing:
        print(f"Нет схем: {', '.join(missing)}", file=sys.stderr)
        return 1
    print("OK: база доступна, схемы на месте")
    return 0


def migrate_cmd(args: argparse.Namespace) -> int:
    """Применяет миграции и загружает справочники."""
    new = migrate(get_engine())
    print(f"Применены миграции: {', '.join(new)}" if new else "Схема актуальна")
    return 0


def refresh_marts_cmd(args: argparse.Namespace) -> int:
    """Пересчитывает материализованные витрины."""
    from market_map.marts import refresh_marts

    refresh_marts(get_engine())
    print("Витрины пересчитаны")
    return 0


def generate_samples(args: argparse.Namespace) -> int:
    """Генерирует тестовые данные 1С и подготовленной базы."""
    from market_map.samples import generate

    for name, count in generate(args.out, args.seed).items():
        print(f"{name}: {count}")
    return 0


def _month(value: str) -> date:
    return date.fromisoformat(f"{value}-01")


def load_1c(args: argparse.Namespace) -> int:
    """Загружает справочники, продажи и план маржи из 1С."""
    from market_map.integrations.onec import FileSource, HttpSource, load_onec

    settings = get_settings()
    start, end = args.start, args.end
    if args.path or not settings.onec_odata_url:
        source = FileSource(args.path or Path(settings.onec_files_dir))
    else:
        source = HttpSource(settings.onec_odata_url, settings.onec_user, settings.onec_password)
        if start is None:  # по умолчанию с живой базы — текущий и два предыдущих месяца
            end = end or date.today().replace(day=1)
            start = end
            for _ in range(2):
                start = (start - timedelta(days=1)).replace(day=1)

    result = load_onec(get_engine(), source, start, end)
    for name, count in result.stats.items():
        print(f"{name}: {count}")
    for entity, reason, _ in result.rejected[:20]:
        print(f"Отклонено ({entity}): {reason}", file=sys.stderr)
    print(f"Журнал загрузки: staging.load_run id={result.run_id}")
    return 0


def load_prepared(args: argparse.Namespace) -> int:
    """Загружает подготовленную базу конкурентов и цен."""
    from market_map.integrations.prepared import load_prepared as load

    url = f"sqlite:///{args.path}" if args.path else get_settings().prepared_db_url
    result = load(get_engine(), url)
    for name, count in result.stats.items():
        print(f"{name}: {count}")
    for entity, reason, _ in result.rejected[:20]:
        print(f"Отклонено ({entity}): {reason}", file=sys.stderr)
    print(f"Журнал загрузки: staging.load_run id={result.run_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-map")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help=check.__doc__).set_defaults(func=check)
    commands.add_parser("migrate", help=migrate_cmd.__doc__).set_defaults(func=migrate_cmd)
    commands.add_parser("refresh-marts", help=refresh_marts_cmd.__doc__).set_defaults(
        func=refresh_marts_cmd
    )

    gen = commands.add_parser("generate-samples", help=generate_samples.__doc__)
    gen.add_argument("--out", type=Path, default=Path("data/samples"))
    gen.add_argument("--seed", type=int, default=None)
    gen.set_defaults(func=generate_samples)

    onec = commands.add_parser("load-1c", help=load_1c.__doc__)
    onec.add_argument("--path", type=Path, help="папка с файлами выгрузки OData")
    onec.add_argument("--from", dest="start", type=_month, help="первый месяц, YYYY-MM")
    onec.add_argument("--to", dest="end", type=_month, help="последний месяц, YYYY-MM")
    onec.set_defaults(func=load_1c)

    prepared = commands.add_parser("load-prepared", help=load_prepared.__doc__)
    prepared.add_argument("--path", type=Path, help="файл SQLite с подготовленной базой")
    prepared.set_defaults(func=load_prepared)

    args = parser.parse_args(argv)
    if getattr(args, "seed", 0) is None:
        from market_map.samples.generator import DEFAULT_SEED

        args.seed = DEFAULT_SEED
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
