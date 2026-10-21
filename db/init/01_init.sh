#!/usr/bin/env bash
# Первичная инициализация хранилища. Запускается образом postgres при пустом томе,
# в CI — напрямую (подключение берётся из PGHOST/PGPASSWORD).
set -euo pipefail

BI_READER_USER="${BI_READER_USER:-bi_reader}"
BI_READER_PASSWORD="${BI_READER_PASSWORD:-bi_reader}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v bi_user="$BI_READER_USER" -v bi_password="$BI_READER_PASSWORD" <<'SQL'
-- Слои хранилища
create schema if not exists staging;  -- сырые загрузки из 1С и подготовленной базы
create schema if not exists core;     -- нормализованная модель с историей
create schema if not exists marts;    -- витрины для BI

comment on schema staging is 'Сырые загрузки из источников';
comment on schema core is 'Нормализованная модель данных с историей';
comment on schema marts is 'Витрины для BI';

-- Пользователь BI: только чтение витрин
select format('create role %I login password %L', :'bi_user', :'bi_password')
where not exists (select from pg_roles where rolname = :'bi_user')
\gexec

grant connect on database :"DBNAME" to :"bi_user";
grant usage on schema marts to :"bi_user";
grant select on all tables in schema marts to :"bi_user";
alter default privileges in schema marts grant select on tables to :"bi_user";

-- Отдельная база для метаданных Superset
select 'create database superset'
where not exists (select from pg_database where datname = 'superset')
\gexec
SQL
