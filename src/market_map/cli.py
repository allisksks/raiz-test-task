"""Командная строка: `python -m market_map <команда>`."""

import argparse
import sys

from market_map.db import get_engine, missing_schemas


def check() -> int:
    """Проверяет подключение к базе и наличие слоёв хранилища."""
    missing = missing_schemas(get_engine())
    if missing:
        print(f"Нет схем: {', '.join(missing)}", file=sys.stderr)
        return 1
    print("OK: база доступна, схемы на месте")
    return 0


COMMANDS = {"check": check}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-map")
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
