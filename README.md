# Market Map — интерактивная карта России

Дашборд для мониторинга клиентов, конкурентов, номенклатуры и цен по регионам. Автоматически выявляет изменения на рынке и предлагает гипотезы об их причинах.

План разработки: [PLAN.md](PLAN.md).

## Состав

| Компонент | Что делает |
|---|---|
| `db` | PostgreSQL 16 — хранилище: схемы `staging` → `core` → `marts` |
| `app` | Python-пакет `market_map`: ETL (1С, подготовленная база), сигналы, гипотезы |
| `superset` | Apache Superset — BI: карта РФ, drill-down, фильтры |

## Запуск

Нужны Docker и Docker Compose.

```bash
cp .env.example .env      # при необходимости поменяйте пароли
docker compose up -d --build --wait
```

- Superset: http://localhost:8088 (логин и пароль — `SUPERSET_ADMIN_USER` / `SUPERSET_ADMIN_PASSWORD`, по умолчанию `admin` / `admin`).
- Подключение к хранилищу «Market Map» в Superset создаётся автоматически под пользователем только на чтение (`BI_READER_USER`).
- Проверка хранилища: `docker compose run --rm app check`.
- Создать таблицы и загрузить справочники: `docker compose run --rm app migrate`.
- Загрузить данные 1С: `docker compose run --rm app load-1c` (без настроек — из тестовой выгрузки; подключение к живой базе — [docs/integration-1c.md](docs/integration-1c.md)).
- Загрузить подготовленную базу конкурентов: `docker compose run --rm app load-prepared` (после 1С — с её номенклатурой сопоставляются товары конкурентов; подробности — [docs/integration-prepared-db.md](docs/integration-prepared-db.md)).

Витрины для BI (схема `marts`) и сигналы о рыночных изменениях пересчитываются автоматически после каждой загрузки: что в витринах — [docs/marts.md](docs/marts.md), какие бывают сигналы — [docs/signals.md](docs/signals.md).

## Тестовые данные

Выгрузка «как из 1С» и подготовленная база цен конкурентов лежат в `data/samples/`. Формат и заложенные сценарии описаны в [docs/test-data.md](docs/test-data.md). Пересоздать: `make samples`.

Остановить: `docker compose down`. Удалить вместе с данными: `docker compose down -v`.

## Разработка

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make lint     # ruff
make test     # pytest (тесты с PostgreSQL пропускаются, если база не запущена)
```

Ветки и правила коммитов — в разделе 8 [PLAN.md](PLAN.md#8-git-процесс).
