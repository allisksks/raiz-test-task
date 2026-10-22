"""Командная строка: `python -m market_map <команда>`."""

import argparse
import sys
from datetime import date
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


def detect_signals_cmd(args: argparse.Namespace) -> int:
    """Пересчитывает сигналы о рыночных изменениях."""
    from market_map.signals import detect_signals

    for signal_type, count in sorted(detect_signals(get_engine()).items()):
        print(f"{signal_type}: {count}")
    return 0


def generate_hypotheses_cmd(args: argparse.Namespace) -> int:
    """Пересчитывает гипотезы о причинах ценовых различий и ценовую архитектуру."""
    from market_map.hypotheses import generate_hypotheses

    for name, count in generate_hypotheses(get_engine()).items():
        print(f"{name}: {count}")
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
    from market_map.integrations.onec import load_onec
    from market_map.pipeline import onec_source

    source, start, end = onec_source(get_settings(), args.path, args.start, args.end)
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


def run_pipeline_cmd(args: argparse.Namespace) -> int:
    """Полное обновление: миграции, 1С, подготовленная база (витрины, сигналы, гипотезы)."""
    from market_map.pipeline import run_pipeline

    for name, result in run_pipeline(get_engine(), get_settings()).items():
        print(f"{name}: {result.stats} (staging.load_run id={result.run_id})")
    return 0


def schedule_cmd(args: argparse.Namespace) -> int:
    """Запускает обновление каждый день в SCHEDULE_TIME (часовой пояс SCHEDULE_TZ)."""
    from market_map.pipeline import schedule

    schedule(get_engine(), get_settings())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-map")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help=check.__doc__).set_defaults(func=check)
    commands.add_parser("migrate", help=migrate_cmd.__doc__).set_defaults(func=migrate_cmd)
    commands.add_parser("refresh-marts", help=refresh_marts_cmd.__doc__).set_defaults(
        func=refresh_marts_cmd
    )
    commands.add_parser("detect-signals", help=detect_signals_cmd.__doc__).set_defaults(
        func=detect_signals_cmd
    )
    commands.add_parser("generate-hypotheses", help=generate_hypotheses_cmd.__doc__).set_defaults(
        func=generate_hypotheses_cmd
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

    commands.add_parser("run-pipeline", help=run_pipeline_cmd.__doc__).set_defaults(
        func=run_pipeline_cmd
    )
    commands.add_parser("schedule", help=schedule_cmd.__doc__).set_defaults(func=schedule_cmd)

    args = parser.parse_args(argv)
    if getattr(args, "seed", 0) is None:
        from market_map.samples.generator import DEFAULT_SEED

        args.seed = DEFAULT_SEED
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
