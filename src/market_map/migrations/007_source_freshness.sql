-- Свежесть и достоверность источников данных (docs/data-sources.md, раздел
-- «Как источники видны в системе»).

create view marts.source_freshness as
with data_by_source as (
    select source_id, max(observed_at) as last_data, count(*) as records
    from core.price_observation
    group by 1
    union all
    select c.source_id, (max(s.period) + interval '1 month' - interval '1 day')::date, count(*)
    from core.sales_fact s
    join core.company c on c.id = s.client_id
    group by 1
),
loads as (
    select source_code,
           max(finished_at) filter (where status = 'success')                               as last_load,
           count(*) filter (where status = 'failed' and started_at > now() - interval '7 days') as failed_7d
    from staging.load_run
    group by 1
)
select
    ds.code,
    ds.name,
    case ds.kind
        when '1c' then '1С'
        when 'prepared_db' then 'Подготовленная база'
        when 'site' then 'Сайты конкурентов'
        when 'price_list' then 'Прайс-листы'
        when 'marketplace' then 'Маркетплейсы'
        when 'field' then 'Полевой мониторинг'
        else ds.kind
    end                                                         as kind,
    ds.reliability,
    ds.auto_fetch,
    ds.refresh_period,
    case ds.refresh_period
        when 'daily' then 'ежедневно'
        when 'weekly' then 'еженедельно'
        when 'monthly' then 'ежемесячно'
        else 'по мере поступления'
    end                                                         as refresh_period_name,
    ds.restrictions,
    l.last_load,
    d.last_data,
    to_char(d.last_data, 'DD.MM.YYYY')                          as last_data_text,
    coalesce(d.records, 0)                                      as records,
    coalesce(l.failed_7d, 0)                                    as failed_loads_7d,
    case
        when ds.refresh_period not in ('daily', 'weekly', 'monthly') then 'по мере поступления'
        when l.last_load is null then 'нет загрузок'
        when l.last_load < now() - 2 * case ds.refresh_period
                                           when 'daily' then interval '1 day'
                                           when 'weekly' then interval '7 days'
                                           else interval '31 days'
                                       end then 'устарел'
        else 'актуален'
    end                                                         as status
from core.data_source ds
-- источники наблюдений подготовленной базы (prepared_db:N) загружаются её загрузчиком
left join loads l on l.source_code = split_part(ds.code, ':', 1)
-- у «родительского» источника — сумма по его дочерним
left join lateral (
    select max(x.last_data) as last_data, sum(x.records) as records
    from data_by_source x
    join core.data_source child on child.id = x.source_id
    where child.code = ds.code or child.code like ds.code || ':%'
) d on true;
