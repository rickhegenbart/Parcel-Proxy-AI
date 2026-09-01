create table if not exists
    public.construction_cost_indicators (
        id uuid primary key default gen_random_uuid(),
        created_at timestamptz not null default now(),

        period date not null,
        series_id text not null,
        metric_name text not null,
        metric_value numeric not null,
        metric_unit text,

        mom_change_pct numeric,
        yoy_change_pct numeric,
        cost_pressure_label text,

        source_name text,
        geography_name text,
        geography_level text,
        context_category text,
        confidence_level text,
        notes text
    );

create unique index if not exists
    idx_construction_cost_series_id
on public.construction_cost_indicators (series_id);

create index if not exists
    idx_construction_cost_period
on public.construction_cost_indicators (period);

create index if not exists
    idx_construction_cost_series
on public.construction_cost_indicators (series_id);