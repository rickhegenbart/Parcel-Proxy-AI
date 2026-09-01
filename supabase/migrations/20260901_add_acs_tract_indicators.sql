create table if not exists public.acs_tract_indicators (
    id uuid primary key default gen_random_uuid(),

    tract_fips text not null,
    state_fips text not null,
    county_fips text not null,
    tract_code text not null,
    geography_name text,

    release_year integer not null,

    total_population integer,
    median_age numeric,
    median_household_income numeric,

    poverty_population integer,
    poverty_below integer,
    poverty_rate_pct numeric,

    civilian_labor_force integer,
    unemployed_population integer,
    unemployment_rate_pct numeric,

    housing_units integer,
    occupied_housing_units integer,
    vacant_housing_units integer,
    vacancy_rate_pct numeric,

    owner_occupied_units integer,
    renter_occupied_units integer,
    owner_occupancy_rate_pct numeric,

    median_home_value numeric,
    median_gross_rent numeric,

    source_name text not null
        default 'U.S. Census Bureau ACS 5-Year Estimates',

    source_url text,
    source_date date,
    raw_payload jsonb,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    constraint acs_tract_indicators_unique
        unique (tract_fips, release_year)
);

create index if not exists
    idx_acs_tract_indicators_tract_fips
on public.acs_tract_indicators (tract_fips);

create index if not exists
    idx_acs_tract_indicators_county_fips
on public.acs_tract_indicators (county_fips);

create index if not exists
    idx_acs_tract_indicators_release_year
on public.acs_tract_indicators (release_year);