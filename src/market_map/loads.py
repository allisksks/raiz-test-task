"""Журнал загрузок: запуск, статистика, отклонённые записи (staging.load_run/load_reject)."""

import json
from dataclasses import dataclass, field

from sqlalchemy import Connection, Engine, text


@dataclass
class LoadResult:
    run_id: int
    stats: dict[str, int] = field(default_factory=dict)
    rejected: list[tuple[str, str, dict]] = field(
        default_factory=list
    )  # (сущность, причина, запись)

    def reject(self, entity: str, reason: str, row: dict) -> None:
        self.rejected.append((entity, reason, row))


def start_run(engine: Engine, source_code: str, params: dict) -> LoadResult:
    with engine.begin() as conn:
        run_id = conn.execute(
            text("insert into staging.load_run (source_code, params) values (:s, :p) returning id"),
            {"s": source_code, "p": json.dumps(params, ensure_ascii=False)},
        ).scalar_one()
    return LoadResult(run_id=run_id)


def finish_run(engine: Engine, result: LoadResult, error: str | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "update staging.load_run set finished_at = now(), status = :st, stats = :stats, "
                "error = :e where id = :id"
            ),
            {
                "id": result.run_id,
                "st": "failed" if error else "success",
                "stats": json.dumps(result.stats),
                "e": error,
            },
        )
        if result.rejected:
            conn.execute(
                text(
                    "insert into staging.load_reject (run_id, entity, reason, payload) "
                    "values (:r, :e, :reason, :p)"
                ),
                [
                    {"r": result.run_id, "e": e, "reason": reason,
                     "p": json.dumps(row, ensure_ascii=False, default=str)}
                    for e, reason, row in result.rejected
                ],
            )  # fmt: skip


def source_ids(conn: Connection, table: str, source_id: int) -> dict[str, int]:
    """{source_key: id} записей таблицы core.<table>, загруженных из источника."""
    rows = conn.execute(
        text(f"select source_key, id from core.{table} where source_id = :s"), {"s": source_id}
    )
    return {key: id_ for key, id_ in rows}


def product_group_ids(conn: Connection, by: str = "source_key") -> dict[str, int]:
    rows = conn.execute(text(f"select {by}, id from core.product_group"))
    return {key: id_ for key, id_ in rows}
