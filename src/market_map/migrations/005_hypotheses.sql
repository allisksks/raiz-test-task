-- Аналитические гипотезы о причинах ценовых различий и ценовая архитектура товара
-- конкурента. Производные данные: пересчитываются вслед за сигналами.

create table core.hypothesis (
    id              bigserial primary key,
    signal_id       bigint not null references core.signal on delete cascade,
    hypothesis_type text not null,
    title           text not null,
    score           numeric(3, 2) not null check (score between 0 and 1),
    rationale       text not null,
    -- доводы: [{"text": ..., "kind": "fact" | "estimate", "weight": 0.3, "present": true}]
    evidence        jsonb not null default '[]',
    provenance      core.provenance not null default 'hypothesis'
);
create index hypothesis_signal on core.hypothesis (signal_id);

-- Ценовая архитектура: из чего может складываться цена конкурента
create table core.price_breakdown (
    signal_id  bigint not null references core.signal on delete cascade,
    step       int not null,
    component  text not null,
    value      numeric(12, 4) not null,
    unit       text not null,           -- rub | share
    provenance core.provenance not null,
    note       text,
    primary key (signal_id, step)
);

create view marts.hypotheses as
select
    h.id,
    s.id                                                  as signal_id,
    s.signal_name,
    s.severity_name,
    s.event_date,
    s.region_code,
    s.region,
    s.map_code,
    s.client,
    s.competitor,
    s.product_group,
    s.title                                               as signal_title,
    h.hypothesis_type,
    h.title,
    h.score,
    rank() over (partition by h.signal_id order by h.score desc) as rank,
    h.rationale,
    h.evidence,
    h.provenance
from core.hypothesis h
join marts.signals s on s.id = h.signal_id;

create view marts.price_breakdown as
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
    b.note
from core.price_breakdown b
join marts.signals s on s.id = b.signal_id;
