-- NOAA Storm Events storage and county-level API summary.
-- This migration is idempotent and can be safely rerun.

create table if not exists public.noaa_storm_events (
    id uuid primary key default gen_random_uuid(),

    event_id bigint not null,
    episode_id bigint,
    event_year integer not null,
    begin_yearmonth integer,
    end_yearmonth integer,

    state text,
    state_fips text,
    cz_type text,
    cz_fips text,
    county_fips text,
    cz_name text,
    event_type text,

    begin_date_time timestamp without time zone,
    end_date_time timestamp without time zone,
    timezone_code text,

    injuries_direct integer,
    injuries_indirect integer,
    deaths_direct integer,
    deaths_indirect integer,

    damage_property numeric,
    damage_crops numeric,

    event_source text,
    magnitude numeric,
    magnitude_type text,
    flood_cause text,
    tornado_scale text,

    begin_location text,
    end_location text,
    begin_latitude numeric,
    begin_longitude numeric,
    end_latitude numeric,
    end_longitude numeric,

    episode_narrative text,
    event_narrative text,
    data_source text,

    source_name text not null
        default 'NOAA Storm Events Database',
    source_url text,
    source_file text,
    source_revision_date date,
    raw_payload jsonb,

    created_at timestamp with time zone
        not null default now(),
    updated_at timestamp with time zone
        not null default now(),

    constraint noaa_storm_events_event_id_key
        unique (event_id)
);

create index if not exists
    idx_noaa_storm_events_county_date
on public.noaa_storm_events (
    county_fips,
    begin_date_time
);

create index if not exists
    idx_noaa_storm_events_type
on public.noaa_storm_events (
    event_type
);

-- Required for safe context-indicator upserts.
create unique index if not exists
    parcel_context_indicators_unique_idx
on public.parcel_context_indicators (
    parcel_id,
    geography_name,
    geography_level,
    context_category,
    metric_name,
    source_name
);

create or replace view public.noaa_storm_event_summary
with (security_invoker = true)
as
select
    county_fips,

    count(*) as total_event_count,

    count(*) filter (
        where begin_date_time
            >= current_date - interval '10 years'
    ) as recent_10_year_event_count,

    count(*) filter (
        where event_type = 'Hail'
    ) as hail_event_count,

    count(*) filter (
        where event_type = 'Thunderstorm Wind'
    ) as thunderstorm_wind_event_count,

    count(*) filter (
        where event_type in (
            'Flood',
            'Flash Flood'
        )
    ) as flood_event_count,

    count(*) filter (
        where event_type = 'Tornado'
    ) as tornado_event_count,

    sum(
        coalesce(injuries_direct, 0)
        + coalesce(injuries_indirect, 0)
    ) as total_injuries,

    sum(
        coalesce(deaths_direct, 0)
        + coalesce(deaths_indirect, 0)
    ) as total_deaths,

    sum(
        coalesce(damage_property, 0)
    ) as reported_property_damage,

    min(begin_date_time) as earliest_event_date,
    max(begin_date_time) as latest_event_date,
    max(source_revision_date)
        as latest_source_revision_date

from public.noaa_storm_events
group by county_fips;