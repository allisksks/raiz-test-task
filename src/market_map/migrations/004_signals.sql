-- Сигналы о значимых изменениях на рынке. Таблица производная: детектор
-- (market-map detect-signals) пересчитывает её целиком после каждой загрузки.

create type core.signal_severity as enum ('info', 'warning', 'critical');

create table core.signal (
    id               bigserial primary key,
    signal_type      text not null,
    severity         core.signal_severity not null,
    event_date       date not null,          -- когда произошло изменение
    region_code      text references core.region,
    client_id        bigint references core.company on delete cascade,
    competitor_id    bigint references core.company on delete cascade,
    product_group_id bigint references core.product_group,
    product_id       bigint references core.product on delete cascade,
    value            numeric,                 -- главный показатель: разрыв цены, изменение маржи…
    title            text not null,           -- текст для дашборда
    details          jsonb not null default '{}',
    -- сигналы строятся по фактам; гипотезы о причинах — отдельно
    provenance       core.provenance not null default 'fact',
    detected_at      timestamptz not null default now()
);
create index signal_region_date on core.signal (region_code, event_date);

create view marts.signals as
select
    s.id,
    s.signal_type,
    case s.signal_type
        when 'composite' then 'Новый конкурент и изменение маржи'
        when 'new_competitor' then 'Новый конкурент'
        when 'competitor_exit' then 'Уход конкурента'
        when 'competitor_price_change' then 'Изменение цены конкурента'
        when 'new_substitute' then 'Новый товар-заменитель'
        when 'assortment_change' then 'Изменение ассортимента'
        when 'price_position_change' then 'Изменение ценовой позиции'
        when 'margin_plan_deviation' then 'Отклонение маржи от плана'
        when 'market_share_change' then 'Изменение доли рынка'
        else s.signal_type
    end                                             as signal_name,
    s.severity,
    case s.severity when 'critical' then 'Критично' when 'warning' then 'Внимание' else 'Информация' end
                                                    as severity_name,
    s.event_date,
    r.code                                          as region_code,
    r.name                                          as region,
    r.map_code,
    cl.name                                         as client,
    co.name                                         as competitor,
    pg.name                                         as product_group,
    p.name                                          as product,
    s.value,
    s.title,
    s.details,
    s.provenance
from core.signal s
left join core.region r on r.code = s.region_code
left join core.company cl on cl.id = s.client_id
left join core.company co on co.id = s.competitor_id
left join core.product_group pg on pg.id = s.product_group_id
left join core.product p on p.id = s.product_id;
