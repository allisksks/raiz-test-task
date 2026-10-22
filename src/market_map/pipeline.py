"""Конвейер обновления данных и его запуск по расписанию.

Конвейер: миграции → 1С → подготовленная база. Витрины, сигналы и гипотезы
пересчитываются загрузчиками сами. Планировщик (`market-map schedule`) запускает
конвейер раз в сутки в заданное время; ошибка одного запуска не останавливает
следующие — она остаётся в журнале staging.load_run.
"""

import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from market_map.config import Settings
from market_map.integrations.onec import FileSource, HttpSource, OneCSource, load_onec
from market_map.integrations.prepared import load_prepared
from market_map.loads import LoadResult
from market_map.migrate import migrate

# С живой базы 1С по умолчанию перезагружаются текущий и два предыдущих месяца
ONEC_MONTHS_BACK = 2


def onec_source(
    settings: Settings,
    path: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    today: date | None = None,
) -> tuple[OneCSource, date | None, date | None]:
    """Источник 1С и период загрузки: файлы выгрузки или живая база по OData."""
    if path or not settings.onec_odata_url:
        return FileSource(path or Path(settings.onec_files_dir)), start, end
    source = HttpSource(settings.onec_odata_url, settings.onec_user, settings.onec_password)
    if start is None:
        end = end or (today or date.today()).replace(day=1)
        start = end
        for _ in range(ONEC_MONTHS_BACK):
            start = (start - timedelta(days=1)).replace(day=1)
    return source, start, end


def run_pipeline(engine: Engine, settings: Settings) -> dict[str, LoadResult]:
    migrate(engine)
    source, start, end = onec_source(settings)
    return {
        "1c": load_onec(engine, source, start, end),
        "prepared_db": load_prepared(engine, settings.prepared_db_url),
    }


def next_run(now: datetime, at: str) -> datetime:
    """Ближайший момент «HH:MM» после now (в часовом поясе now)."""
    hour, minute = map(int, at.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _log(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def _run_logged(engine: Engine, settings: Settings) -> None:
    _log("запуск конвейера")
    try:
        for name, result in run_pipeline(engine, settings).items():
            _log(f"{name}: {result.stats} (staging.load_run id={result.run_id})")
    except Exception:
        _log("конвейер завершился ошибкой:\n" + traceback.format_exc())


def schedule(engine: Engine, settings: Settings) -> None:
    tz = ZoneInfo(settings.schedule_tz)
    if settings.schedule_run_on_start:
        _run_logged(engine, settings)
    while True:
        at = next_run(datetime.now(tz), settings.schedule_time)
        _log(f"следующий запуск: {at.isoformat(timespec='minutes')}")
        time.sleep(max(0.0, (at - datetime.now(tz)).total_seconds()))
        _run_logged(engine, settings)
