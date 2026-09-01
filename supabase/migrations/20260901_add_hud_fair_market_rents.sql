create table if not exists
    public.hud_fair_market_rents (
        id uuid primary key default gen_random_uuid(),

        hud_entity_id text not null,
        county_fips text not null,
        county_name text not null,
        state_code text not null,

        metro_status boolean,
        metro_name text,
        area_name text,

        fiscal_year integer not null,

        efficiency_rent numeric,
        one_bedroom_rent numeric,
        two_bedroom_rent numeric,
        three_bedroom_rent numeric,
        four_bedroom_rent numeric,

        efficiency_yoy_change_pct numeric,
        one_bedroom_yoy_change_pct numeric,
        two_bedroom_yoy_change_pct numeric,
        three_bedroom_yoy_change_pct numeric,
        four_bedroom_yoy_change_pct numeric,

        source_name text not null
            default 'HUD Fair Market Rents',
        source_url text,
        raw_payload jsonb,

        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),

        constraint hud_fair_market_rents_unique
            unique (county_fips, fiscal_year)
    );

create index if not exists
    idx_hud_fair_market_rents_county
on public.hud_fair_market_rents (county_fips);

create index if not exists
    idx_hud_fair_market_rents_year
on public.hud_fair_market_rents (fiscal_year);