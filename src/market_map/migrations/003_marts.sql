-- Витрины для BI. Тяжёлые расчёты (своя цена, сравнение цен) — материализованные
-- представления, их пересчитывает market-map refresh-marts (вызывается после каждой
-- загрузки); остальные витрины — обычные представления поверх них и core.
-- Все суммы — в рублях, доли и отклонения — долями единицы (0.25 = 25%).

-- Дата, на которую считаются «новые» конкуренты и «текущее» присутствие:
-- последняя дата мониторинга цен (а не now(), чтобы исторические данные оставались осмысленными)
create view marts.as_of as
select coalesce(
    (select max(observed_at) from core.price_observation),
    (select (max(period) + interval '1 month' - interval '1 day')::date from core.sales_fact),
    current_date
) as as_of_date,
-- конкурент считается новым, если появился в регионе за этот срок (квартал с запасом)
120 as new_competitor_days;

-- Продажи для сквозной аналитики: компания → регион → тип клиента → клиент → группа → номенклатура
create view marts.sales as
select
    s.period                                   as month,
    r.code                                     as region_code,
    r.name                                     as region,
    r.federal_district,
    r.map_code,
    ch.code                                    as channel_code,
    coalesce(ch.name, 'Не указан')             as channel,
    c.id                                       as client_id,
    c.name                                     as client,
    pg.name                                    as product_group,
    p.sku,
    p.name                                     as product,
    s.quantity,
    s.revenue,
    s.cost,
    s.margin,
    round(s.margin / nullif(s.revenue, 0), 4)  as margin_pct,
    round(s.revenue / nullif(s.quantity, 0), 2) as avg_price
from core.sales_fact s
join core.company c on c.id = s.client_id
join core.region r on r.code = s.region_code
join core.product p on p.id = s.product_id
join core.product_group pg on pg.id = p.group_id
left join core.channel ch on ch.code = c.channel_code;

-- План/факт маржи по клиенту и группе товаров
create view marts.margin_plan_fact as
with fact as (
    select s.period, s.client_id, p.group_id, s.region_code,
           sum(s.revenue) as revenue, sum(s.margin) as margin
    from core.sales_fact s
    join core.product p on p.id = s.product_id
    group by 1, 2, 3, 4
)
select
    coalesce(f.period, mp.period)                              as month,
    r.code                                                     as region_code,
    r.name                                                     as region,
    r.map_code,
    coalesce(ch.name, 'Не указан')                             as channel,
    c.id                                                       as client_id,
    c.name                                                     as client,
    pg.name                                                    as product_group,
    coalesce(f.revenue, 0)                                     as revenue,
    coalesce(f.margin, 0)                                      as margin,
    round(f.margin / nullif(f.revenue, 0), 4)                  as margin_pct,
    mp.revenue                                                 as plan_revenue,
    round(mp.margin_pct / 100, 4)                              as plan_margin_pct,
    round(f.margin / nullif(f.revenue, 0) - mp.margin_pct / 100, 4) as margin_pct_deviation,
    round(f.revenue / nullif(mp.revenue, 0) - 1, 4)            as revenue_deviation
from fact f
full join core.margin_plan mp
    on mp.period = f.period and mp.client_id = f.client_id and mp.product_group_id = f.group_id
join core.company c on c.id = coalesce(f.client_id, mp.client_id)
join core.product_group pg on pg.id = coalesce(f.group_id, mp.product_group_id)
left join core.channel ch on ch.code = c.channel_code
-- регион клиента: из продаж, а если продаж не было — из присутствия
left join lateral (
    select cr.region_code from core.company_region cr
    where cr.company_id = c.id order by cr.valid_to nulls first limit 1
) cr on true
join core.region r on r.code = coalesce(f.region_code, cr.region_code);

-- Своя цена: средняя цена продажи за месяц по товару — в разрезе региона и канала,
-- по региону (все каналы), по каналу (вся страна) и по стране в целом
create materialized view marts.own_price as
select
    s.period                                    as month,
    s.region_code,
    s.product_id,
    c.channel_code,
    grouping(s.region_code) = 1                 as national,
    grouping(c.channel_code) = 1                as all_channels,
    round(sum(s.revenue) / nullif(sum(s.quantity), 0), 2) as price,
    sum(s.quantity)                             as quantity,
    sum(s.revenue)                              as revenue
from core.sales_fact s
join core.company c on c.id = s.client_id
group by grouping sets ((s.period, s.region_code, s.product_id, c.channel_code),
                        (s.period, s.region_code, s.product_id),
                        (s.period, s.product_id, c.channel_code),
                        (s.period, s.product_id));
create index own_price_lookup on marts.own_price (region_code, product_id, month);

-- Соотношение цены в канале к средней цене по стране — для оценки своей цены в канале,
-- где в регионе нет продаж
create materialized view marts.own_channel_factor as
select ch.month, ch.product_id, ch.channel_code, ch.price / nullif(total.price, 0) as factor
from marts.own_price ch
join marts.own_price total
    on total.month = ch.month and total.product_id = ch.product_id
   and total.national and total.all_channels
where ch.national and not ch.all_channels;
create index own_channel_factor_lookup on marts.own_channel_factor (product_id, channel_code, month);

-- Сравнение цен конкурентов со своей ценой.
-- Если за неделю одна и та же цена пришла из нескольких источников, берётся самый достоверный.
-- Своя цена для сравнения — за последний месяц не позже наблюдения, по приоритету:
--   1) в регионе и канале конкурента (факт);
--   2) в регионе по всем каналам × соотношение цены канала к средней по стране (оценка);
--   3) в регионе по всем каналам (грубое сравнение, в сводные показатели не входит).
create materialized view marts.price_comparison as
with ranked as (
    select po.*, ds.reliability, ds.name as source,
           row_number() over (
               partition by po.observed_at, po.region_code, po.company_id, po.product_id
               order by ds.reliability, po.id
           ) as rn
    from core.price_observation po
    join core.data_source ds on ds.id = po.source_id
)
select
    o.observed_at,
    date_trunc('month', o.observed_at)::date      as month,
    r.code                                        as region_code,
    r.name                                        as region,
    r.map_code,
    pg.name                                       as product_group,
    op.id                                         as own_product_id,
    op.sku                                        as own_sku,
    op.name                                       as own_product,
    comp.id                                       as competitor_id,
    comp.name                                     as competitor,
    comp.channel_code                             as competitor_channel_code,
    coalesce(ch.name, 'Не указан')                as competitor_channel,
    pm.match_type,
    cp.name                                       as competitor_product,
    cp.brand                                      as competitor_brand,
    o.price                                       as competitor_price,
    own.price                                     as own_price,
    own.month                                     as own_price_month,
    own.basis                                     as own_price_basis,
    round(o.price / nullif(own.price, 0) - 1, 4)  as price_gap,
    coalesce(own.basis <> 'все каналы', false)    as comparable,
    o.reliability,
    o.source,
    o.provenance
from ranked o
join core.product_match pm on pm.competitor_product_id = o.product_id
join core.product cp on cp.id = o.product_id
join core.product op on op.id = pm.own_product_id
join core.product_group pg on pg.id = op.group_id
join core.company comp on comp.id = o.company_id
join core.region r on r.code = o.region_code
left join core.channel ch on ch.code = comp.channel_code
left join lateral (
    select candidate.price, candidate.month, candidate.basis
    from (
        select p.price, p.month, 1 as priority, 'канал конкурента' as basis
        from marts.own_price p
        where not p.national and p.region_code = o.region_code and p.product_id = pm.own_product_id
          and not p.all_channels and p.channel_code = comp.channel_code
        union all
        select round(p.price * f.factor, 2), p.month, 2, 'оценка для канала'
        from marts.own_price p
        join marts.own_channel_factor f
            on f.month = p.month and f.product_id = p.product_id
           and f.channel_code = comp.channel_code
        where not p.national and p.region_code = o.region_code and p.product_id = pm.own_product_id
          and p.all_channels
        union all
        select p.price, p.month, 3, 'все каналы'
        from marts.own_price p
        where not p.national and p.region_code = o.region_code and p.product_id = pm.own_product_id
          and p.all_channels
    ) candidate
    where candidate.month <= o.observed_at
    order by candidate.month desc, candidate.priority
    limit 1
) own on true
where o.rn = 1;
create index price_comparison_region on marts.price_comparison (region_code, month);
create index price_comparison_competitor on marts.price_comparison (competitor_id);

-- Ценовая позиция по месяцам: насколько конкуренты дешевле/дороже своей цены
create view marts.price_position as
select
    month,
    region_code,
    region,
    map_code,
    product_group,
    match_type,
    count(distinct competitor_id)                                      as competitors,
    count(distinct own_product_id)                                     as products,
    round(percentile_cont(0.5) within group (order by price_gap)::numeric, 4) as median_price_gap,
    round(min(price_gap), 4)                                           as min_price_gap,
    round(avg(price_gap), 4)                                           as avg_price_gap
from marts.price_comparison
where comparable
group by 1, 2, 3, 4, 5, 6;

-- Присутствие клиентов и конкурентов в регионах (слои карты)
create view marts.presence as
select
    c.kind,
    case c.kind when 'client' then 'Клиент' else 'Конкурент' end      as kind_name,
    c.id                                                              as company_id,
    c.name                                                            as company,
    c.inn,
    coalesce(ch.name, 'Не указан')                                    as channel,
    r.code                                                            as region_code,
    r.name                                                            as region,
    r.federal_district,
    r.map_code,
    cr.valid_from,
    cr.valid_to,
    (cr.valid_to is null or cr.valid_to >= a.as_of_date)              as is_active,
    (c.kind = 'competitor' and cr.valid_from > a.as_of_date - a.new_competitor_days
        and cr.valid_from > (select min(observed_at) from core.price_observation)) as is_new,
    c.registered_at,
    c.has_regional_warehouse,
    c.supply_scheme
from core.company c
join core.company_region cr on cr.company_id = c.id
join core.region r on r.code = cr.region_code
left join core.channel ch on ch.code = c.channel_code
cross join marts.as_of a;

-- Оценка долей рынка (provenance = estimate), если фактических объёмов рынка нет.
-- Метод: вес участника в регионе и группе = сумма по товарам, которые он продаёт,
-- (медианная цена товара на рынке / цена участника). Доля = вес / сумма весов.
-- Считается только за месяцы, когда в регионе велся мониторинг цен конкурентов.
-- Учитываются и ширина ассортимента, и ценовая привлекательность. Фактические доли из
-- core.market_share (provenance = fact) выводятся как есть.
create view marts.market_share as
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
)
select
    w.month,
    w.region_code,
    rg.name                                                         as region,
    rg.map_code,
    w.product_group,
    case when w.company_id is null then 'own' else 'competitor' end as player_kind,
    w.company_id,
    w.player,
    round((w.weight / sum(w.weight) over (partition by w.month, w.region_code, w.product_group))::numeric, 4) as share,
    'estimate'::core.provenance                                     as provenance,
    'ассортимент × медианная цена / цена'::text                     as method
from weights w
join core.region rg on rg.code = w.region_code
union all
select
    ms.period, ms.region_code, rg.name, rg.map_code, pg.name,
    case when ms.company_id is null then 'own' else 'competitor' end,
    ms.company_id, coalesce(c.name, 'Своя компания'), ms.share, ms.provenance, ms.method
from core.market_share ms
join core.region rg on rg.code = ms.region_code
join core.product_group pg on pg.id = ms.product_group_id
left join core.company c on c.id = ms.company_id
where ms.provenance = 'fact';

-- Сводка по регионам для карты: все 85 регионов, даже без присутствия
create view marts.region_summary as
with last_month as (
    select max(period) as month from core.sales_fact
),
sales as (
    select s.region_code, sum(s.revenue) as revenue, sum(s.margin) as margin
    from core.sales_fact s, last_month lm
    where s.period = lm.month
    group by 1
),
plan as (
    select v.region_code,
           sum(v.plan_revenue * v.plan_margin_pct) / nullif(sum(v.plan_revenue), 0) as plan_margin_pct
    from marts.margin_plan_fact v, last_month lm
    where v.month = lm.month
    group by 1
),
presence as (
    select region_code,
           count(distinct company_id) filter (where kind = 'client' and is_active)     as clients,
           count(distinct company_id) filter (where kind = 'competitor' and is_active) as competitors,
           count(distinct company_id) filter (where is_new and is_active)              as new_competitors
    from marts.presence group by 1
),
gap as (
    select pc.region_code,
           percentile_cont(0.5) within group (order by pc.price_gap) as median_price_gap
    from marts.price_comparison pc, marts.as_of a
    where pc.comparable and pc.match_type = 'identical' and pc.observed_at > a.as_of_date - 28
    group by 1
)
select
    r.code                                         as region_code,
    r.name                                         as region,
    r.federal_district,
    r.map_code,
    (select month from last_month)                 as month,
    coalesce(p.clients, 0)                         as clients,
    coalesce(p.competitors, 0)                     as competitors,
    coalesce(p.new_competitors, 0)                 as new_competitors,
    s.revenue,
    round(s.margin / nullif(s.revenue, 0), 4)      as margin_pct,
    round(pl.plan_margin_pct, 4)                   as plan_margin_pct,
    round(s.margin / nullif(s.revenue, 0) - pl.plan_margin_pct, 4) as margin_pct_deviation,
    round(g.median_price_gap::numeric, 4)          as median_price_gap
from core.region r
left join presence p on p.region_code = r.code
left join sales s on s.region_code = r.code
left join plan pl on pl.region_code = r.code
left join gap g on g.region_code = r.code;
