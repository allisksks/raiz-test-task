"""Командная строка: `python -m market_map <команда>`."""

import argparse
import sys
from pathlib import Path

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


def generate_samples(args: argparse.Namespace) -> int:
    """Генерирует тестовые данные 1С и подготовленной базы."""
    from market_map.samples import generate

    for name, count in generate(args.out, args.seed).items():
        print(f"{name}: {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-map")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help=check.__doc__).set_defaults(func=check)
    commands.add_parser("migrate", help=migrate_cmd.__doc__).set_defaults(func=migrate_cmd)

    gen = commands.add_parser("generate-samples", help=generate_samples.__doc__)
    gen.add_argument("--out", type=Path, default=Path("data/samples"))
    gen.add_argument("--seed", type=int, default=None)
    gen.set_defaults(func=generate_samples)

    args = parser.parse_args(argv)
    if getattr(args, "seed", 0) is None:
        from market_map.samples.generator import DEFAULT_SEED

        args.seed = DEFAULT_SEED
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
