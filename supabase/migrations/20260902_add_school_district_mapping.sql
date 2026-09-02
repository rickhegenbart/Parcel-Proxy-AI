create table if not exists public.parcel_school_district_map (
    parcel_id text not null,
    district_type text not null,
    district_geoid text not null,
    lea_id text not null,
    district_name text not null,
    low_grade text,
    high_grade text,
    latitude double precision,
    longitude double precision,
    tiger_year integer not null,
    updated_at timestamp with time zone not null default now(),

    constraint parcel_school_district_map_pkey
        primary key (parcel_id, district_type),

    constraint parcel_school_district_map_district_type_check
        check (
            district_type in (
                'elementary',
                'secondary',
                'unified'
            )
        )
);

create index if not exists
    idx_parcel_school_district_map_parcel_id
on public.parcel_school_district_map (parcel_id);

create index if not exists
    idx_parcel_school_district_map_lea_id
on public.parcel_school_district_map (lea_id);

create index if not exists
    idx_parcel_school_district_map_district_type
on public.parcel_school_district_map (district_type);

create index if not exists
    idx_parcel_school_district_map_tiger_year
on public.parcel_school_district_map (tiger_year);

comment on table public.parcel_school_district_map is
    'Coordinate-derived parcel assignments to Census TIGER elementary, secondary, or unified school districts.';

comment on column public.parcel_school_district_map.lea_id is
    'Seven-digit NCES local education agency identifier derived from the TIGER district GEOID.';

comment on column public.parcel_school_district_map.district_type is
    'TIGER school-district layer: elementary, secondary, or unified.';

comment on column public.parcel_school_district_map.tiger_year is
    'Publication year of the Census TIGER school-district boundaries used for the assignment.';