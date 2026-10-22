#!/usr/bin/env bash
# Одноразовая инициализация Superset: миграции, администратор, роли, дашборды,
# подключение к витринам хранилища под пользователем только на чтение.
set -euo pipefail

superset db upgrade

superset fab create-admin \
    --username "$SUPERSET_ADMIN_USER" \
    --password "$SUPERSET_ADMIN_PASSWORD" \
    --firstname Admin --lastname Admin \
    --email "$SUPERSET_ADMIN_EMAIL" || true  # уже создан при повторном запуске

superset init

# Дашборды из репозитория вместе с подключением «Market Map» к витринам (пользователь
# только на чтение). В выгрузке пароль замаскирован — подставляем адрес подключения из
# окружения во временную копию и импортируем её.
python - <<'PY'
import os
import shutil
from pathlib import Path
from urllib.parse import quote

bundle = Path("/tmp/bundle")
shutil.rmtree(bundle, ignore_errors=True)
shutil.copytree("/app/dashboards", bundle / "dashboards")
uri = "postgresql+psycopg2://{}:{}@{}:{}/{}".format(
    quote(os.environ["BI_READER_USER"], safe=""),
    quote(os.environ["BI_READER_PASSWORD"], safe=""),
    os.environ.get("POSTGRES_HOST", "db"),
    os.environ.get("POSTGRES_PORT", "5432"),
    os.environ["POSTGRES_DB"],
)
for path in (bundle / "dashboards" / "databases").glob("*.yaml"):
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [f"sqlalchemy_uri: '{uri}'" if line.startswith("sqlalchemy_uri:") else line
             for line in lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
shutil.make_archive("/tmp/dashboards", "zip", bundle, "dashboards")
PY
superset import-dashboards --path /tmp/dashboards.zip --username "$SUPERSET_ADMIN_USER"
