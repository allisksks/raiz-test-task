-- Ядро хранилища: нормализованная модель с историей.

-- Происхождение числа: факт из источника, расчётная оценка или аналитическая гипотеза
create type core.provenance as enum ('fact', 'estimate', 'hypothesis');

-- Достоверность источника: A — первичные учётные данные, B — открытые источники, C — косвенные
create type core.reliability as enum ('A', 'B', 'C');

create table core.region (
    code             text primary key,   -- ISO 3166-2:RU
    name             text not null unique,
    federal_district text not null,
    map_code         text not null       -- код региона в карте Superset
);

create table core.channel (
    code text primary key,
    name text not null unique
);

create table core.data_source (
    id             bigserial primary key,
    code           text not null unique,
    name           text not null,
    kind           text not null,          -- 1c, site, price_list, marketplace, tender, registry, field
    reliability    core.reliability not null,
    auto_fetch     boolean not null default false,
    refresh_period text,                   -- daily, weekly, monthly, on_demand
    restrictions   text
);

create table core.company (
    id            bigserial primary key,
    kind          text not null check (kind in ('client', 'competitor')),
    name          text not null,
    inn           text,
    channel_code  text references core.channel,
    registered_at date,                    -- дата регистрации юрлица (ЕГРЮЛ)
    -- Признаки для гипотез о ценообразовании конкурента
    has_regional_warehouse boolean,
    supply_scheme text,
    source_id     bigint not null references core.data_source,
    source_key    text not null,           -- ключ записи в источнике (GUID 1С, id в подготовленной базе)
    unique (source_id, source_key)
);
create unique index company_inn_kind_uq on core.company (kind, inn) where inn is not null;

-- Присутствие компании в регионе; valid_to = null — присутствует сейчас
create table core.company_region (
    company_id  bigint not null references core.company on delete cascade,
    region_code text not null references core.region,
    valid_from  date not null,
    valid_to    date,
    primary key (company_id, region_code, valid_from),
    check (valid_to is null or valid_to >= valid_from)
);

create table core.product_group (
    id         bigserial primary key,
    name       text not null unique,
    source_key text
);

-- Номенклатура: свои товары (company_id = null) и товары конкурентов
create table core.product (
    id         bigserial primary key,
    group_id   bigint references core.product_group,
    company_id bigint references core.company on delete cascade,
    sku        text,
    name       text not null,
    brand      text,
    unit       text,
    source_id  bigint not null references core.data_source,
    source_key text not null,
    unique (source_id, source_key)
);

-- Сопоставление товара конкурента со своим товаром
create table core.product_match (
    competitor_product_id bigint not null references core.product on delete cascade,
    own_product_id        bigint not null references core.product on delete cascade,
    match_type            text not null check (match_type in ('identical', 'similar')),
    method                text not null,   -- sku, name, manual
    confidence            numeric(3, 2) not null check (confidence between 0 and 1),
    primary key (competitor_product_id, own_product_id)
);

-- История цен конкурентов: каждое наблюдение — отдельная строка, ничего не перезаписывается
create table core.price_observation (
    id           bigserial primary key,
    observed_at  date not null,
    region_code  text not null references core.region,
    company_id   bigint not null references core.company on delete cascade,
    product_id   bigint not null references core.product on delete cascade,
    channel_code text references core.channel,
    price        numeric(12, 2) not null check (price > 0),
    source_id    bigint not null references core.data_source,
    provenance   core.provenance not null default 'fact',
    unique (observed_at, region_code, company_id, product_id, source_id)
);
create index price_observation_lookup on core.price_observation (region_code, product_id, observed_at);

-- Продажи из 1С: обороты за месяц по клиенту и товару
create table core.sales_fact (
    period      date not null check (extract(day from period) = 1),
    client_id   bigint not null references core.company on delete cascade,
    region_code text not null references core.region,
    product_id  bigint not null references core.product,
    quantity    numeric(14, 3) not null,
    revenue     numeric(14, 2) not null,
    cost        numeric(14, 2) not null,
    margin      numeric(14, 2) generated always as (revenue - cost) stored,
    primary key (period, client_id, region_code, product_id)
);

-- План маржи из 1С: по клиенту и группе товаров на месяц
create table core.margin_plan (
    period           date not null check (extract(day from period) = 1),
    client_id        bigint not null references core.company on delete cascade,
    product_group_id bigint not null references core.product_group,
    revenue          numeric(14, 2) not null,
    margin_pct       numeric(5, 2) not null,
    primary key (period, client_id, product_group_id)
);

-- Доли рынка: факт при наличии объёмов, иначе оценка по прошлым периодам
create table core.market_share (
    period           date not null,
    region_code      text not null references core.region,
    product_group_id bigint not null references core.product_group,
    company_id       bigint references core.company on delete cascade,  -- null — своя компания
    share            numeric(5, 4) not null check (share between 0 and 1),
    provenance       core.provenance not null,
    method           text not null
);
create unique index market_share_uq on core.market_share
    (period, region_code, product_group_id, coalesce(company_id, 0));
