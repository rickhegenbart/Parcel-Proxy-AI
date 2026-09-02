create table if not exists public.school_district_indicators (
    lea_id text not null,
    school_year text not null,
    district_name text not null,
    state_abbreviation text not null default 'MT',

    total_students numeric,
    teacher_fte numeric,
    student_teacher_ratio numeric,

    source_name text not null
        default 'NCES Common Core of Data',
    directory_source_file text,
    membership_source_file text,
    staff_source_file text,
    source_url text,
    source_date date,

    confidence_level text not null
        default 'context_only',
    notes text,
    updated_at timestamp with time zone not null
        default now(),

    constraint school_district_indicators_pkey
        primary key (lea_id, school_year),

    constraint school_district_indicators_lea_id_check
        check (lea_id ~ '^[0-9]{7}$'),

    constraint school_district_indicators_students_check
        check (
            total_students is null
            or total_students >= 0
        ),

    constraint school_district_indicators_teacher_fte_check
        check (
            teacher_fte is null
            or teacher_fte >= 0
        ),

    constraint school_district_indicators_ratio_check
        check (
            student_teacher_ratio is null
            or student_teacher_ratio >= 0
        )
);

create index if not exists
    idx_school_district_indicators_school_year
on public.school_district_indicators (school_year);

create index if not exists
    idx_school_district_indicators_lea_id
on public.school_district_indicators (lea_id);

comment on table public.school_district_indicators is
    'NCES Common Core of Data enrollment and staffing context for school districts assigned to current parcels.';

comment on column
    public.school_district_indicators.student_teacher_ratio
is
    'Total district students divided by teacher full-time-equivalent staff. This is contextual and is not a school quality score.';