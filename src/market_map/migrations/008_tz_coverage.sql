-- Витрины, закрывающие оставшиеся пункты ТЗ: ключевые конкуренты клиента, доля по типу
-- клиента, ассортимент конкурентов, оценка долей по статистике прошлых периодов.

-- Структура продаж по типу клиента (каналу): какую долю выручки региона и группы
-- товаров дают дистрибьюторы, опт, розница и т. д. (ТЗ, п. 7 «доля типа клиента»)
create view marts.channel_share as
select
    s.period                                       as month,
    r.code                                         as region_code,
    r.name                                         as region,
    r.map_code,
    pg.name                                        as product_group,
    coalesce(ch.name, 'Не указан')                 as channel,
    sum(s.revenue)                                 as revenue,
    sum(s.margin)                                  as margin,
    round(sum(s.revenue) / nullif(sum(sum(s.revenue)) over (
        partition by s.period, r.code, pg.name), 0), 4) as share
from core.sales_fact s
join core.company c on c.id = s.client_id
join core.product p on p.id = s.product_id
join core.product_group pg on pg.id = p.group_id
join core.region r on r.code = s.region_code
left join core.channel ch on ch.code = c.channel_code
group by 1, 2, 3, 4, 5, 6;

-- Ассортимент конкурентов: каждая позиция в регионе — когда появилась, когда последний
-- раз наблюдалась, последняя цена и статус
create view marts.competitor_assortment as
with positions as (
    select pc.*,
           min(pc.observed_at) over w                                    as first_seen,
           max(pc.observed_at) over w                                    as last_seen,
           row_number() over (partition by pc.competitor_id, pc.region_code,
                              pc.own_product_id, pc.competitor_product
                              order by pc.observed_at desc)              as rn
    from marts.price_comparison pc
    window w as (partition by pc.competitor_id, pc.region_code, pc.own_product_id,
                 pc.competitor_product)
)
select
    p.competitor_id,
    p.competitor,
    p.competitor_channel,
    p.region_code,
    p.region,
    p.map_code,
    p.product_group,
    p.competitor_product,
    p.competitor_brand,
    case p.match_type when 'identical' then 'тот же товар' else 'аналог' end as match_type,
    p.own_product_id,
    p.own_product,
    p.first_seen,
    p.last_seen,
    p.competitor_price,
    p.own_price,
    p.price_gap,
    case
        when pr.is_active is false then 'конкурент ушёл из региона'
        -- три недели без наблюдений при том, что конкурент в регионе есть
        when p.last_seen < a.as_of_date - 21 then 'пропал из ассортимента'
        else 'в продаже'
    end                                                                  as status,
    case when pr.is_new then 'да' else 'нет' end                         as is_new_competitor
from positions p
cross join marts.as_of a
left join marts.presence pr
    on pr.company_id = p.competitor_id and pr.region_code = p.region_code
where p.rn = 1;

-- Ключевые конкуренты клиента (ТЗ, п. 2 «ближайшие и ключевые конкуренты»): кто из
-- конкурентов в регионе клиента продаёт то, что клиент у нас покупает, и насколько дешевле.
--   overlap_share — доля закупок клиента (выручки последнего месяца), которую перекрывает
--                   ассортимент конкурента;
--   price_gap     — медиана (цена конкурента за 8 недель / цена клиента − 1) по этим товарам;
--   threat        — overlap_share × скидка конкурента к цене клиента (0, если конкурент
--                   дороже): какую часть закупок клиента конкурент может забрать ценой;
--                   по нему ранжируются конкуренты клиента, при равенстве — по пересечению;
--   same_channel  — тот же канал, что у клиента («ближайший» конкурент).
create view marts.client_competitors as
with last_month as (
    select max(period) as month from core.sales_fact
),
client_prices as (
    select s.client_id, s.region_code, s.product_id,
           sum(s.revenue) as revenue, sum(s.revenue) / nullif(sum(s.quantity), 0) as price
    from core.sales_fact s, last_month lm
    where s.period = lm.month
    group by 1, 2, 3
),
client_totals as (
    select client_id, sum(revenue) as total from client_prices group by 1
),
recent as (
    select pc.region_code, pc.competitor_id, pc.own_product_id,
           percentile_cont(0.5) within group (order by pc.competitor_price) as price
    from marts.price_comparison pc, marts.as_of a
    where pc.observed_at > a.as_of_date - 56
    group by 1, 2, 3
),
pairs as (
    select cp.client_id, rc.competitor_id,
           count(*)                                                          as products,
           sum(cp.revenue)                                                   as overlap_revenue,
           percentile_cont(0.5) within group (order by rc.price / cp.price - 1) as price_gap
    from client_prices cp
    join recent rc on rc.region_code = cp.region_code and rc.own_product_id = cp.product_id
    group by 1, 2
),
scored as (
    select p.*, round(p.overlap_revenue / nullif(t.total, 0), 4) as overlap_share
    from pairs p join client_totals t using (client_id)
)
select
    cl.id                                                              as client_id,
    cl.name                                                            as client,
    coalesce(clch.name, 'Не указан')                                   as channel,
    r.code                                                             as region_code,
    r.name                                                             as region,
    r.map_code,
    co.id                                                              as competitor_id,
    co.name                                                            as competitor,
    coalesce(coch.name, 'Не указан')                                   as competitor_channel,
    cl.channel_code is not distinct from co.channel_code               as same_channel,
    s.products,
    s.overlap_share,
    round(s.price_gap::numeric, 4)                                     as price_gap,
    round((s.overlap_share * greatest(-s.price_gap, 0))::numeric, 4)   as threat,
    rank() over (partition by cl.id
                 order by s.overlap_share * greatest(-s.price_gap, 0) desc,
                          s.overlap_share desc)                         as rank,
    case when pr.is_new then 'да' else 'нет' end                       as is_new_competitor
from scored s
join core.company cl on cl.id = s.client_id
join core.company co on co.id = s.competitor_id
left join core.channel clch on clch.code = cl.channel_code
left join core.channel coch on coch.code = co.channel_code
join lateral (
    select cr.region_code from core.company_region cr
    where cr.company_id = cl.id order by cr.valid_to nulls first limit 1
) cr on true
join core.region r on r.code = cr.region_code
left join marts.presence pr on pr.company_id = co.id and pr.region_code = r.code;

-- Доли рынка: к оценке по ассортименту и цене добавлен метод из ТЗ — «по статистике
-- предыдущих периодов». Ёмкость рынка региона и группы берётся из базового месяца
-- (первый месяц мониторинга: наша выручка / наша доля по ассортиментной оценке) и
-- переносится на следующие месяцы по сезонности наших продаж по стране в прошлых
-- периодах. Наша доля = наша выручка / ёмкость; остаток делится между конкурентами
-- пропорционально их весу. Если наши продажи падают сильнее, чем объясняет сезонность,
-- доля снижается.
create or replace view marts.market_share as
with competitor_prices as (
    select date_trunc('month', observed_at)::date as month, region_code, own_product_id,
           product_group, competitor_id as company_id, competitor as player,
           percentile_cont(0.5) within group (order by competitor_price) as price
    from marts.price_comparison
    group by 1, 2, 3, 4, 5, 6
),
own_prices as (
    select p.month, p.region_code, p.product_id as own_product_id, pg.name as product_group,
           null::bigint as company_id, 'Своя компания'::text as player, p.price
    from marts.own_price p
    join core.product pr on pr.id = p.product_id
    join core.product_group pg on pg.id = pr.group_id
    where p.all_channels and not p.national
),
prices as (
    select * from competitor_prices
    where month in (select period from core.sales_fact)
    union all
    select * from own_prices
    where (month, region_code) in (select month, region_code from competitor_prices)
),
reference as (
    select month, region_code, own_product_id,
           percentile_cont(0.5) within group (order by price) as ref_price
    from prices group by 1, 2, 3
),
weights as (
    select p.month, p.region_code, p.product_group, p.company_id, p.player,
           sum(r.ref_price / p.price) as weight
    from prices p
    join reference r using (month, region_code, own_product_id)
    group by 1, 2, 3, 4, 5
),
assortment as (
    select w.*,
           w.weight / sum(w.weight) over (partition by w.month, w.region_code, w.product_group)
               as share
    from weights w
),
own_revenue as (
    select s.period as month, s.region_code, pg.name as product_group, sum(s.revenue) as revenue
    from core.sales_fact s
    join core.product p on p.id = s.product_id
    join core.product_group pg on pg.id = p.group_id
    group by 1, 2, 3
),
national as (
    select period as month, sum(revenue) as revenue from core.sales_fact group by 1
),
base as (
    -- базовый месяц: первый месяц мониторинга в регионе и группе
    select distinct on (a.region_code, a.product_group)
           a.region_code, a.product_group, a.month, a.share
    from assortment a
    where a.company_id is null
    order by a.region_code, a.product_group, a.month
),
market as (
    select o.month, o.region_code, o.product_group, o.revenue,
           ob.revenue / nullif(b.share, 0) * n.revenue / nullif(nb.revenue, 0) as capacity
    from own_revenue o
    join base b using (region_code, product_group)
    join own_revenue ob
        on ob.region_code = b.region_code and ob.product_group = b.product_group
       and ob.month = b.month
    join national n on n.month = o.month
    join national nb on nb.month = b.month
    where o.month >= b.month
),
history_own as (
    select month, region_code, product_group,
           least(1, revenue / nullif(capacity, 0)) as share
    from market
),
history as (
    select h.month, h.region_code, h.product_group, null::bigint as company_id,
           'Своя компания'::text as player, h.share
    from history_own h
    union all
    select a.month, a.region_code, a.product_group, a.company_id, a.player,
           (1 - h.share) * a.weight / sum(a.weight) over (
               partition by a.month, a.region_code, a.product_group)
    from assortment a
    join history_own h using (month, region_code, product_group)
    where a.company_id is not null
)
select
    a.month,
    a.region_code,
    rg.name                                                         as region,
    rg.map_code,
    a.product_group,
    case when a.company_id is null then 'own' else 'competitor' end as player_kind,
    a.company_id,
    a.player,
    round(a.share::numeric, 4)                                      as share,
    'estimate'::core.provenance                                     as provenance,
    'ассортимент × медианная цена / цена'::text                     as method,
    'assortment'::text                                              as method_code
from assortment a
join core.region rg on rg.code = a.region_code
union all
select
    h.month, h.region_code, rg.name, rg.map_code, h.product_group,
    case when h.company_id is null then 'own' else 'competitor' end,
    h.company_id, h.player, round(h.share::numeric, 4),
    'estimate'::core.provenance,
    'по статистике прошлых периодов: ёмкость базового месяца × сезонность'::text,
    'history'::text
from history h
join core.region rg on rg.code = h.region_code
union all
select
    ms.period, ms.region_code, rg.name, rg.map_code, pg.name,
    case when ms.company_id is null then 'own' else 'competitor' end,
    ms.company_id, coalesce(c.name, 'Своя компания'), ms.share, ms.provenance, ms.method,
    'fact'::text
from core.market_share ms
join core.region rg on rg.code = ms.region_code
join core.product_group pg on pg.id = ms.product_group_id
left join core.company c on c.id = ms.company_id
where ms.provenance = 'fact';

-- Новый тип сигнала «позиция пропала из ассортимента» — подпись в витрине сигналов
create or replace view marts.signals as
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
        when 'assortment_exit' then 'Позиция пропала из ассортимента'
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

