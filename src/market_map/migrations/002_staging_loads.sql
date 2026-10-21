-- Журнал загрузок и сырые данные источников (для аудита и разбора ошибок).

create table staging.load_run (
    id          bigserial primary key,
    source_code text not null,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status      text not null default 'running' check (status in ('running', 'success', 'failed')),
    params      jsonb not null default '{}',
    stats       jsonb not null default '{}',
    error       text
);

-- Записи, которые не удалось загрузить в core, с причиной
create table staging.load_reject (
    run_id  bigint not null references staging.load_run on delete cascade,
    entity  text not null,
    reason  text not null,
    payload jsonb not null
);

-- Сырые записи 1С как пришли из OData
create table staging.onec_raw (
    run_id  bigint not null references staging.load_run on delete cascade,
    entity  text not null,
    payload jsonb not null
);
create index onec_raw_run on staging.onec_raw (run_id, entity);
