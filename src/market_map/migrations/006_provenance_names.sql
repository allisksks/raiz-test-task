-- Русские подписи происхождения данных для BI: факт / оценка / гипотеза.

create function core.provenance_name(p core.provenance) returns text
language sql immutable as $$
    select case p when 'fact' then 'Факт' when 'estimate' then 'Оценка' else 'Гипотеза' end
$$;

create or replace view marts.price_breakdown as
select
    s.id          as signal_id,
    s.signal_name,
    s.event_date,
    s.region,
    s.competitor,
    s.product_group,
    b.step,
    b.component,
    b.value,
    b.unit,
    b.provenance,
    b.note,
    core.provenance_name(b.provenance) as provenance_name
from core.price_breakdown b
join marts.signals s on s.id = b.signal_id;
