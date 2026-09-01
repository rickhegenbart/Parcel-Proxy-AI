import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from app.supabase_client import get_supabase


load_dotenv()

CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "").strip()

CENSUS_DATASET = "acs/acs5"
CENSUS_STATE_FIPS = "30"
CENSUS_API_ROOT = "https://api.census.gov/data"

MAPPING_TABLE = "parcel_census_tract_map"
TARGET_TABLE = "acs_tract_indicators"
PIPELINE_RUNS_TABLE = "data_pipeline_runs"

PAGE_SIZE = 1000
UPSERT_BATCH_SIZE = 250


ACS_VARIABLES = {
    "NAME": "geography_name",
    "B01003_001E": "total_population",
    "B01002_001E": "median_age",
    "B19013_001E": "median_household_income",
    "B17001_001E": "poverty_population",
    "B17001_002E": "poverty_below",
    "B23025_003E": "civilian_labor_force",
    "B23025_005E": "unemployed_population",
    "B25001_001E": "housing_units",
    "B25002_002E": "occupied_housing_units",
    "B25002_003E": "vacant_housing_units",
    "B25003_002E": "owner_occupied_units",
    "B25003_003E": "renter_occupied_units",
    "B25077_001E": "median_home_value",
    "B25064_001E": "median_gross_rent",
}


INTEGER_FIELDS = {
    "total_population",
    "poverty_population",
    "poverty_below",
    "civilian_labor_force",
    "unemployed_population",
    "housing_units",
    "occupied_housing_units",
    "vacant_housing_units",
    "owner_occupied_units",
    "renter_occupied_units",
}


NUMERIC_FIELDS = {
    "median_age",
    "median_household_income",
    "median_home_value",
    "median_gross_rent",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_census_api_key() -> None:
    if not CENSUS_API_KEY:
        raise RuntimeError(
            "CENSUS_API_KEY is missing. Add it to backend/.env "
            "and to the GitHub Actions repository secrets."
        )


def chunked(
    values: List[Dict[str, Any]],
    size: int,
) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def normalize_fips(
    value: Any,
    width: int,
) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    if text.endswith(".0"):
        text = text[:-2]

    digits = "".join(
        character
        for character in text
        if character.isdigit()
    )

    if not digits:
        return None

    return digits.zfill(width)


def clean_numeric(value: Any) -> Optional[float]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    try:
        number = float(text)
    except (TypeError, ValueError):
        return None

    # ACS uses large negative sentinel values for unavailable,
    # suppressed, or statistically invalid estimates.
    if number <= -600000000:
        return None

    return number


def clean_integer(value: Any) -> Optional[int]:
    number = clean_numeric(value)

    if number is None:
        return None

    return int(round(number))


def safe_percentage(
    numerator: Optional[float],
    denominator: Optional[float],
) -> Optional[float]:
    if numerator is None or denominator is None:
        return None

    if denominator <= 0:
        return None

    return round((numerator / denominator) * 100, 4)


def census_request(
    release_year: int,
    variables: List[str],
) -> List[List[str]]:
    parameters = {
        "get": ",".join(variables),
        "for": "tract:*",
        "in": f"state:{CENSUS_STATE_FIPS}",
        "key": CENSUS_API_KEY,
    }

    url = (
        f"{CENSUS_API_ROOT}/{release_year}/"
        f"{CENSUS_DATASET}?{urlencode(parameters)}"
    )

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "(ACS demographic data updater)"
            )
        },
    )

    try:
        with urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
    except Exception as error:
        raise RuntimeError(
            f"Census API request failed for {release_year}: {error}"
        ) from error

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "The Census API returned a non-JSON response for "
            f"{release_year}: {body[:300]}"
        ) from error

    if not isinstance(payload, list) or len(payload) < 2:
        raise RuntimeError(
            f"The Census API returned no tract rows for {release_year}."
        )

    return payload


def discover_latest_release() -> int:
    current_year = datetime.now(timezone.utc).year

    # ACS releases normally lag the current calendar year.
    # Check recent candidate years until a working release is found.
    for release_year in range(
        current_year - 1,
        current_year - 6,
        -1,
    ):
        try:
            payload = census_request(
                release_year,
                ["NAME", "B01003_001E"],
            )

            if len(payload) > 1:
                return release_year
        except RuntimeError:
            continue

    raise RuntimeError(
        "No usable ACS 5-year release was found in the "
        "five most recent candidate years."
    )


def fetch_mapped_tract_ids(client) -> Set[str]:
    tract_ids: Set[str] = set()
    offset = 0

    while True:
        result = (
            client
            .table(MAPPING_TABLE)
            .select("tract_fips")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )

        rows = result.data or []

        for row in rows:
            tract_fips = normalize_fips(
                row.get("tract_fips"),
                11,
            )

            if tract_fips:
                tract_ids.add(tract_fips)

        if len(rows) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    if not tract_ids:
        raise RuntimeError(
            "No mapped tract IDs were found in "
            "parcel_census_tract_map."
        )

    return tract_ids


def download_acs_data(
    release_year: int,
) -> Tuple[List[str], List[List[str]]]:
    variables = list(ACS_VARIABLES.keys())
    payload = census_request(release_year, variables)

    header = payload[0]
    source_rows = payload[1:]

    required_geography_fields = {
        "state",
        "county",
        "tract",
    }

    missing_fields = (
        set(variables)
        .union(required_geography_fields)
        .difference(header)
    )

    if missing_fields:
        raise RuntimeError(
            "ACS response is missing required fields: "
            f"{sorted(missing_fields)}"
        )

    return header, source_rows


def build_rows(
    header: List[str],
    source_rows: List[List[str]],
    mapped_tract_ids: Set[str],
    release_year: int,
) -> List[Dict[str, Any]]:
    prepared_rows: List[Dict[str, Any]] = []

    for source_values in source_rows:
        source = dict(zip(header, source_values))

        state_fips = normalize_fips(
            source.get("state"),
            2,
        )
        county_fips = normalize_fips(
            source.get("county"),
            3,
        )
        tract_code = normalize_fips(
            source.get("tract"),
            6,
        )

        if not state_fips or not county_fips or not tract_code:
            continue

        tract_fips = (
            f"{state_fips}{county_fips}{tract_code}"
        )

        if tract_fips not in mapped_tract_ids:
            continue

        row: Dict[str, Any] = {
            "tract_fips": tract_fips,
            "state_fips": state_fips,
            "county_fips": county_fips,
            "tract_code": tract_code,
            "release_year": release_year,
            "source_name": (
                "U.S. Census Bureau ACS 5-Year Estimates"
            ),
            "source_url": (
                f"{CENSUS_API_ROOT}/{release_year}/"
                f"{CENSUS_DATASET}"
            ),
            "source_date": None,
            "raw_payload": source,
            "updated_at": utc_now(),
        }

        for variable, destination in ACS_VARIABLES.items():
            value = source.get(variable)

            if destination == "geography_name":
                row[destination] = (
                    str(value).strip()
                    if value is not None
                    else None
                )
            elif destination in INTEGER_FIELDS:
                row[destination] = clean_integer(value)
            elif destination in NUMERIC_FIELDS:
                row[destination] = clean_numeric(value)

        row["poverty_rate_pct"] = safe_percentage(
            row.get("poverty_below"),
            row.get("poverty_population"),
        )

        row["unemployment_rate_pct"] = safe_percentage(
            row.get("unemployed_population"),
            row.get("civilian_labor_force"),
        )

        row["vacancy_rate_pct"] = safe_percentage(
            row.get("vacant_housing_units"),
            row.get("housing_units"),
        )

        row["owner_occupancy_rate_pct"] = safe_percentage(
            row.get("owner_occupied_units"),
            row.get("occupied_housing_units"),
        )

        prepared_rows.append(row)

    prepared_rows.sort(
        key=lambda row: row["tract_fips"]
    )

    return prepared_rows


def validate_rows(
    rows: List[Dict[str, Any]],
    mapped_tract_ids: Set[str],
    release_year: int,
) -> None:
    if not rows:
        raise RuntimeError(
            "No mapped ACS rows were prepared."
        )

    prepared_ids = {
        row["tract_fips"]
        for row in rows
    }

    missing_ids = mapped_tract_ids.difference(
        prepared_ids
    )

    if missing_ids:
        raise RuntimeError(
            "ACS data is missing mapped tract IDs: "
            f"{sorted(missing_ids)}"
        )

    if len(rows) != len(prepared_ids):
        raise RuntimeError(
            "Duplicate tract IDs were prepared."
        )

    for row in rows:
        if row["release_year"] != release_year:
            raise RuntimeError(
                "Prepared ACS rows contain inconsistent "
                "release years."
            )

        population = row.get("total_population")

        if population is not None and population < 0:
            raise RuntimeError(
                "A negative population value was prepared "
                f"for tract {row['tract_fips']}."
            )

        for percentage_field in [
            "poverty_rate_pct",
            "unemployment_rate_pct",
            "vacancy_rate_pct",
            "owner_occupancy_rate_pct",
        ]:
            value = row.get(percentage_field)

            if value is not None and not 0 <= value <= 100:
                raise RuntimeError(
                    f"Invalid {percentage_field} for tract "
                    f"{row['tract_fips']}: {value}"
                )


def start_pipeline_run(client) -> Optional[str]:
    try:
        result = (
            client
            .table(PIPELINE_RUNS_TABLE)
            .insert({
    "source_name": (
        "U.S. Census Bureau ACS 5-Year Estimates"
    ),
    "job_name": "update_acs_demographics",
    "status": "running",
    "started_at": utc_now(),
    "rows_read": 0,
    "rows_written": 0,
})
            .execute()
        )

        rows = result.data or []

        if rows:
            return rows[0].get("id")
    except Exception as error:
        print(
            "Warning: could not create pipeline run record:",
            error,
        )

    return None


def finish_pipeline_run(
    client,
    run_id: Optional[str],
    status: str,
    *,
    source_period: Optional[str] = None,
    rows_read: int = 0,
    rows_written: int = 0,
    validation_summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return

    values = {
        "status": status,
        "completed_at": utc_now(),
        "source_period": source_period,
        "rows_read": rows_read,
        "rows_written": rows_written,
        "validation_summary": validation_summary,
        "error_message": error_message,
    }

    try:
        (
            client
            .table(PIPELINE_RUNS_TABLE)
            .update(values)
            .eq("id", run_id)
            .execute()
        )
    except Exception as error:
        print(
            "Warning: could not finish pipeline run record:",
            error,
        )


def upsert_rows(
    client,
    rows: List[Dict[str, Any]],
) -> int:
    written = 0

    for batch in chunked(rows, UPSERT_BATCH_SIZE):
        (
            client
            .table(TARGET_TABLE)
            .upsert(
                batch,
                on_conflict="tract_fips,release_year",
            )
            .execute()
        )

        written += len(batch)

        print(
            f"  Uploaded {written:,} of "
            f"{len(rows):,} tract indicators"
        )

    return written


def main() -> None:
    require_census_api_key()

    client = get_supabase()
    run_id = start_pipeline_run(client)

    source_row_count = 0
    rows_written = 0
    release_year: Optional[int] = None

    try:
        print("Loading mapped Census tract IDs...")
        mapped_tract_ids = fetch_mapped_tract_ids(client)

        print(
            "Mapped tract IDs:",
            len(mapped_tract_ids),
        )

        print("Discovering latest ACS 5-year release...")
        release_year = discover_latest_release()

        print("Latest ACS release:", release_year)

        print("Downloading Montana ACS tract data...")
        header, source_rows = download_acs_data(
            release_year
        )
        source_row_count = len(source_rows)

        print(
            "Montana source tract rows:",
            source_row_count,
        )

        print("Preparing mapped tract indicators...")
        rows = build_rows(
            header,
            source_rows,
            mapped_tract_ids,
            release_year,
        )

        print("Validating ACS indicators...")
        validate_rows(
            rows,
            mapped_tract_ids,
            release_year,
        )

        print("Updating ACS tract indicators...")
        rows_written = upsert_rows(client, rows)

        counties = sorted({
            row["county_fips"]
            for row in rows
        })

        validation_summary = {
            "release_year": release_year,
            "mapped_tract_count": len(mapped_tract_ids),
            "prepared_tract_count": len(rows),
            "county_fips": counties,
            "population_total": sum(
                row["total_population"] or 0
                for row in rows
            ),
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=str(release_year),
            rows_read=source_row_count,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("ACS demographic update succeeded.")
        print("Release year:", release_year)
        print(
            "Montana source tracts:",
            source_row_count,
        )
        print(
            "Mapped tracts processed:",
            rows_written,
        )
        print("Mapped county FIPS:", counties)

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            source_period=(
                str(release_year)
                if release_year is not None
                else None
            ),
            rows_read=source_row_count,
            rows_written=rows_written,
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()