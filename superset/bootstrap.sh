#!/usr/bin/env bash
# Одноразовая инициализация Superset: миграции, администратор, роли,
# подключение к витринам хранилища под пользователем только на чтение.
set -euo pipefail

superset db upgrade

superset fab create-admin \
    --username "$SUPERSET_ADMIN_USER" \
    --password "$SUPERSET_ADMIN_PASSWORD" \
    --firstname Admin --lastname Admin \
    --email "$SUPERSET_ADMIN_EMAIL" || true  # уже создан при повторном запуске

superset init

superset set-database-uri \
    --database_name "Market Map" \
    --uri "postgresql+psycopg2://${BI_READER_USER}:${BI_READER_PASSWORD}@${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432}/${POSTGRES_DB}"
