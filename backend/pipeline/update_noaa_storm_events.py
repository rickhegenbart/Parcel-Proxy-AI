"""Update Yellowstone County NOAA Storm Events records."""

import argparse
import gzip
import io
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

import pandas as pd

from app.supabase_client import get_supabase


NOAA_DIRECTORY_URL = (
    "https://www.ncei.noaa.gov/pub/data/"
    "swdi/stormevents/csvfiles/"
)
SOURCE_NAME = "NOAA Storm Events Database"
TARGET_TABLE = "noaa_storm_events"

MONTANA_STATE_FIPS = "30"
YELLOWSTONE_COUNTY_FIPS = "111"
YELLOWSTONE_FULL_FIPS = "30111"

UPSERT_BATCH_SIZE = 100
DEFAULT_HISTORY_START_YEAR = 1996

DETAILS_FILENAME_PATTERN = re.compile(
    r"StormEvents_details-ftp_v1\.0_"
    r"d(?P<year>\d{4})_"
    r"c(?P<revision>\d{8})\.csv\.gz"
)

RAW_PAYLOAD_FIELDS = [
    "BEGIN_YEARMONTH",
    "BEGIN_DAY",
    "BEGIN_TIME",
    "END_YEARMONTH",
    "END_DAY",
    "END_TIME",
    "EPISODE_ID",
    "EVENT_ID",
    "STATE",
    "STATE_FIPS",
    "YEAR",
    "MONTH_NAME",
    "EVENT_TYPE",
    "CZ_TYPE",
    "CZ_FIPS",
    "CZ_NAME",
    "WFO",
    "BEGIN_DATE_TIME",
    "CZ_TIMEZONE",
    "END_DATE_TIME",
    "INJURIES_DIRECT",
    "INJURIES_INDIRECT",
    "DEATHS_DIRECT",
    "DEATHS_INDIRECT",
    "DAMAGE_PROPERTY",
    "DAMAGE_CROPS",
    "SOURCE",
    "MAGNITUDE",
    "MAGNITUDE_TYPE",
    "FLOOD_CAUSE",
    "CATEGORY",
    "TOR_F_SCALE",
    "TOR_LENGTH",
    "TOR_WIDTH",
    "TOR_OTHER_WFO",
    "TOR_OTHER_CZ_STATE",
    "TOR_OTHER_CZ_FIPS",
    "TOR_OTHER_CZ_NAME",
    "BEGIN_RANGE",
    "BEGIN_AZIMUTH",
    "BEGIN_LOCATION",
    "END_RANGE",
    "END_AZIMUTH",
    "END_LOCATION",
    "BEGIN_LAT",
    "BEGIN_LON",
    "END_LAT",
    "END_LON",
    "EPISODE_NARRATIVE",
    "EVENT_NARRATIVE",
    "DATA_SOURCE",
]


def request_bytes(
    url: str,
    timeout: int = 120,
) -> bytes:
    """Download bytes with retries and a user agent."""
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "NOAA-Storm-Events-Updater"
            )
        },
    )

    last_error = None

    for attempt in range(1, 4):
        try:
            with urlopen(
                request,
                timeout=timeout,
            ) as response:
                return response.read()

        except Exception as error:
            last_error = error

            if attempt < 3:
                time.sleep(attempt * 2)

    raise RuntimeError(
        f"Unable to download {url}: {last_error}"
    )


def discover_latest_files(
    start_year: int,
    end_year: int,
) -> Dict[int, str]:
    """Find the newest NOAA details revision for each year."""
    html = request_bytes(
        NOAA_DIRECTORY_URL,
        timeout=60,
    ).decode("utf-8", errors="replace")

    latest_by_year: Dict[int, Tuple[str, str]] = {}

    for match in DETAILS_FILENAME_PATTERN.finditer(html):
        year = int(match.group("year"))

        if not start_year <= year <= end_year:
            continue

        filename = match.group(0)
        revision = match.group("revision")
        current = latest_by_year.get(year)

        if current is None or revision > current[0]:
            latest_by_year[year] = (
                revision,
                filename,
            )

    missing_years = [
        year
        for year in range(
            start_year,
            end_year + 1,
        )
        if year not in latest_by_year
    ]

    if missing_years:
        raise RuntimeError(
            "NOAA details files were not found for years: "
            f"{missing_years}"
        )

    return {
        year: latest_by_year[year][1]
        for year in sorted(latest_by_year)
    }


def download_dataframe(filename: str) -> pd.DataFrame:
    """Download and read one compressed NOAA details file."""
    compressed = request_bytes(
        f"{NOAA_DIRECTORY_URL}{filename}"
    )

    try:
        csv_bytes = gzip.decompress(compressed)
    except gzip.BadGzipFile as error:
        raise RuntimeError(
            f"NOAA file is not valid gzip: {filename}"
        ) from error

    return pd.read_csv(
        io.BytesIO(csv_bytes),
        dtype={
            "EVENT_ID": "string",
            "EPISODE_ID": "string",
            "STATE_FIPS": "string",
            "CZ_FIPS": "string",
        },
        low_memory=False,
    )


def clean_text(value: Any) -> Optional[str]:
    """Return stripped text or None."""
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    return text or None


def clean_integer(value: Any) -> Optional[int]:
    """Return a native integer or None."""
    if value is None or pd.isna(value):
        return None

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def clean_number(value: Any) -> Optional[float]:
    """Return a finite float or None."""
    if value is None or pd.isna(value):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


def normalize_fips(
    value: Any,
    width: int,
) -> Optional[str]:
    """Normalize a FIPS component and preserve leading zeros."""
    integer = clean_integer(value)

    if integer is None:
        return None

    return str(integer).zfill(width)


def parse_noaa_datetime(
    value: Any,
) -> Optional[str]:
    """Parse NOAA local date-time text for PostgreSQL."""
    text = clean_text(value)

    if text is None:
        return None

    parsed = pd.to_datetime(
        text,
        format="%d-%b-%y %H:%M:%S",
        errors="coerce",
    )

    if pd.isna(parsed):
        raise RuntimeError(
            f"Unable to parse NOAA date-time: {text}"
        )

    return parsed.to_pydatetime().isoformat()


def parse_damage(value: Any) -> Optional[float]:
    """Convert NOAA damage strings such as 10.00K to dollars."""
    text = clean_text(value)

    if text is None:
        return None

    text = text.upper().replace(",", "")

    match = re.fullmatch(
        r"([+-]?\d+(?:\.\d+)?)\s*([KMB]?)",
        text,
    )

    if not match:
        raise RuntimeError(
            f"Unrecognized NOAA damage value: {text}"
        )

    number = float(match.group(1))
    suffix = match.group(2)

    multipliers = {
        "": 1.0,
        "K": 1_000.0,
        "M": 1_000_000.0,
        "B": 1_000_000_000.0,
    }

    return number * multipliers[suffix]


def json_value(value: Any) -> Any:
    """Convert pandas and NumPy values to strict JSON values."""
    if value is None or pd.isna(value):
        return None

    if hasattr(value, "item"):
        value = value.item()

    if isinstance(value, float):
        if not math.isfinite(value):
            return None

    return value


def build_raw_payload(
    source_row: pd.Series,
) -> Dict[str, Any]:
    """Build a JSON-safe copy of relevant NOAA source fields."""
    payload = {
        field: json_value(source_row.get(field))
        for field in RAW_PAYLOAD_FIELDS
    }

    json.dumps(payload, allow_nan=False)
    return payload


def filter_yellowstone(
    source_data: pd.DataFrame,
) -> pd.DataFrame:
    """Keep exact Yellowstone County-coded Montana events."""
    required = set(RAW_PAYLOAD_FIELDS)
    missing = required.difference(source_data.columns)

    if missing:
        raise RuntimeError(
            "NOAA details file is missing columns: "
            f"{sorted(missing)}"
        )

    state_fips = source_data["STATE_FIPS"].map(
        lambda value: normalize_fips(value, 2)
    )
    county_fips = source_data["CZ_FIPS"].map(
        lambda value: normalize_fips(value, 3)
    )
    state = (
        source_data["STATE"]
        .astype("string")
        .str.strip()
        .str.upper()
    )
    county_type = (
        source_data["CZ_TYPE"]
        .astype("string")
        .str.strip()
        .str.upper()
    )

    mask = (
        state.eq("MONTANA")
        & state_fips.eq(MONTANA_STATE_FIPS)
        & county_type.eq("C")
        & county_fips.eq(
            YELLOWSTONE_COUNTY_FIPS
        )
    )

    return source_data.loc[mask].copy()


def build_rows(
    yellowstone_data: pd.DataFrame,
    filename: str,
) -> List[Dict[str, Any]]:
    """Convert filtered NOAA records into Supabase rows."""
    filename_match = DETAILS_FILENAME_PATTERN.fullmatch(
        filename
    )

    if not filename_match:
        raise RuntimeError(
            f"Unexpected NOAA filename: {filename}"
        )

    revision = filename_match.group("revision")
    revision_date = datetime.strptime(
        revision,
        "%Y%m%d",
    ).date().isoformat()

    rows: List[Dict[str, Any]] = []

    for _, source_row in yellowstone_data.iterrows():
        event_id = clean_integer(
            source_row.get("EVENT_ID")
        )

        if event_id is None:
            raise RuntimeError(
                "NOAA event is missing EVENT_ID"
            )

        row = {
            "event_id": event_id,
            "episode_id": clean_integer(
                source_row.get("EPISODE_ID")
            ),
            "event_year": clean_integer(
                source_row.get("YEAR")
            ),
            "begin_yearmonth": clean_integer(
                source_row.get("BEGIN_YEARMONTH")
            ),
            "end_yearmonth": clean_integer(
                source_row.get("END_YEARMONTH")
            ),
            "state": clean_text(
                source_row.get("STATE")
            ),
            "state_fips": MONTANA_STATE_FIPS,
            "cz_type": clean_text(
                source_row.get("CZ_TYPE")
            ),
            "cz_fips": YELLOWSTONE_COUNTY_FIPS,
            "county_fips": YELLOWSTONE_FULL_FIPS,
            "cz_name": clean_text(
                source_row.get("CZ_NAME")
            ),
            "event_type": clean_text(
                source_row.get("EVENT_TYPE")
            ),
            "begin_date_time": parse_noaa_datetime(
                source_row.get("BEGIN_DATE_TIME")
            ),
            "end_date_time": parse_noaa_datetime(
                source_row.get("END_DATE_TIME")
            ),
            "timezone_code": clean_text(
                source_row.get("CZ_TIMEZONE")
            ),
            "injuries_direct": clean_integer(
                source_row.get("INJURIES_DIRECT")
            ),
            "injuries_indirect": clean_integer(
                source_row.get(
                    "INJURIES_INDIRECT"
                )
            ),
            "deaths_direct": clean_integer(
                source_row.get("DEATHS_DIRECT")
            ),
            "deaths_indirect": clean_integer(
                source_row.get("DEATHS_INDIRECT")
            ),
            "damage_property": parse_damage(
                source_row.get("DAMAGE_PROPERTY")
            ),
            "damage_crops": parse_damage(
                source_row.get("DAMAGE_CROPS")
            ),
            "event_source": clean_text(
                source_row.get("SOURCE")
            ),
            "magnitude": clean_number(
                source_row.get("MAGNITUDE")
            ),
            "magnitude_type": clean_text(
                source_row.get("MAGNITUDE_TYPE")
            ),
            "flood_cause": clean_text(
                source_row.get("FLOOD_CAUSE")
            ),
            "tornado_scale": clean_text(
                source_row.get("TOR_F_SCALE")
            ),
            "begin_location": clean_text(
                source_row.get("BEGIN_LOCATION")
            ),
            "end_location": clean_text(
                source_row.get("END_LOCATION")
            ),
            "begin_latitude": clean_number(
                source_row.get("BEGIN_LAT")
            ),
            "begin_longitude": clean_number(
                source_row.get("BEGIN_LON")
            ),
            "end_latitude": clean_number(
                source_row.get("END_LAT")
            ),
            "end_longitude": clean_number(
                source_row.get("END_LON")
            ),
            "episode_narrative": clean_text(
                source_row.get(
                    "EPISODE_NARRATIVE"
                )
            ),
            "event_narrative": clean_text(
                source_row.get("EVENT_NARRATIVE")
            ),
            "data_source": clean_text(
                source_row.get("DATA_SOURCE")
            ),
            "source_name": SOURCE_NAME,
            "source_url": NOAA_DIRECTORY_URL,
            "source_file": filename,
            "source_revision_date": revision_date,
            "raw_payload": build_raw_payload(
                source_row
            ),
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        if row["event_year"] is None:
            raise RuntimeError(
                f"NOAA event {event_id} has no year"
            )

        rows.append(row)

    rows.sort(
        key=lambda row: (
            row["begin_date_time"] or "",
            row["event_id"],
        )
    )

    return rows


def validate_rows(
    rows: List[Dict[str, Any]],
    start_year: int,
    end_year: int,
) -> None:
    """Validate all prepared Yellowstone events."""
    event_ids = [
        row["event_id"]
        for row in rows
    ]

    if len(event_ids) != len(set(event_ids)):
        raise RuntimeError(
            "Duplicate NOAA event IDs found"
        )

    invalid_years = [
        row["event_id"]
        for row in rows
        if not (
            start_year
            <= row["event_year"]
            <= end_year
        )
    ]

    if invalid_years:
        raise RuntimeError(
            "NOAA events fall outside the requested "
            f"year range: {invalid_years[:10]}"
        )

    invalid_counties = [
        row["event_id"]
        for row in rows
        if row["county_fips"]
        != YELLOWSTONE_FULL_FIPS
    ]

    if invalid_counties:
        raise RuntimeError(
            "Prepared NOAA events include an "
            "unexpected county"
        )

    missing_types = [
        row["event_id"]
        for row in rows
        if not row["event_type"]
    ]

    if missing_types:
        raise RuntimeError(
            "NOAA events are missing event types: "
            f"{missing_types[:10]}"
        )


def upsert_batches(
    client,
    rows: List[Dict[str, Any]],
) -> int:
    """Upsert NOAA events in bounded batches."""
    rows_written = 0

    for start in range(
        0,
        len(rows),
        UPSERT_BATCH_SIZE,
    ):
        batch = rows[
            start:start + UPSERT_BATCH_SIZE
        ]

        (
            client
            .table(TARGET_TABLE)
            .upsert(
                batch,
                on_conflict="event_id",
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def start_pipeline_run(
    client,
    start_year: int,
    end_year: int,
):
    """Create a pipeline audit record."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name": SOURCE_NAME,
            "job_name": "update_noaa_storm_events",
            "status": "running",
            "source_period": (
                f"{start_year}-{end_year}"
            ),
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the NOAA pipeline "
            "audit record"
        )

    return result.data[0]["id"]


def finish_pipeline_run(
    client,
    run_id,
    status,
    **values,
):
    """Complete a pipeline audit record."""
    values["status"] = status
    values["completed_at"] = datetime.now(
        timezone.utc
    ).isoformat()

    (
        client
        .table("data_pipeline_runs")
        .update(values)
        .eq("id", run_id)
        .execute()
    )


def parse_arguments():
    """Parse scheduled-update or historical-backfill options."""
    current_year = datetime.now(
        timezone.utc
    ).year

    parser = argparse.ArgumentParser(
        description=(
            "Update Yellowstone County NOAA "
            "Storm Events records"
        )
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=current_year - 1,
        help=(
            "First NOAA event year to process. "
            "Defaults to the previous year."
        ),
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=current_year,
        help=(
            "Last NOAA event year to process. "
            "Defaults to the current year."
        ),
    )

    arguments = parser.parse_args()

    if arguments.start_year < DEFAULT_HISTORY_START_YEAR:
        parser.error(
            "Start year must be 1996 or later"
        )

    if arguments.end_year < arguments.start_year:
        parser.error(
            "End year must not precede start year"
        )

    if arguments.end_year > current_year:
        parser.error(
            "End year must not be in the future"
        )

    return arguments


def main():
    arguments = parse_arguments()
    start_year = arguments.start_year
    end_year = arguments.end_year

    client = get_supabase()
    run_id = start_pipeline_run(
        client,
        start_year,
        end_year,
    )

    try:
        print(
            "Discovering latest NOAA yearly files..."
        )
        files = discover_latest_files(
            start_year,
            end_year,
        )

        all_rows: List[Dict[str, Any]] = []
        total_source_rows = 0
        yearly_summary = {}

        for year, filename in files.items():
            print(
                f"Downloading NOAA {year}: "
                f"{filename}"
            )

            source_data = download_dataframe(
                filename
            )
            total_source_rows += len(source_data)

            yellowstone_data = filter_yellowstone(
                source_data
            )
            rows = build_rows(
                yellowstone_data,
                filename,
            )

            yearly_summary[str(year)] = {
                "source_rows": len(source_data),
                "yellowstone_events": len(rows),
                "source_file": filename,
            }

            print(
                f"  Yellowstone County events: "
                f"{len(rows)}"
            )

            all_rows.extend(rows)

        print("Validating NOAA storm events...")
        validate_rows(
            all_rows,
            start_year,
            end_year,
        )

        print("Updating NOAA storm events...")
        rows_written = upsert_batches(
            client,
            all_rows,
        )

        event_types = sorted({
            row["event_type"]
            for row in all_rows
            if row["event_type"]
        })

        latest_event_date = max(
            (
                row["begin_date_time"]
                for row in all_rows
                if row["begin_date_time"]
            ),
            default=None,
        )

        validation_summary = {
            "start_year": start_year,
            "end_year": end_year,
            "yellowstone_event_count": len(
                all_rows
            ),
            "event_types": event_types,
            "latest_event_date": latest_event_date,
            "yearly_summary": yearly_summary,
            "stale_events_deleted": 0,
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            rows_read=total_source_rows,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("NOAA Storm Events update succeeded.")
        print(
            "Years processed:",
            f"{start_year}-{end_year}",
        )
        print(
            "National source rows:",
            total_source_rows,
        )
        print(
            "Yellowstone events processed:",
            rows_written,
        )
        print(
            "Latest Yellowstone event:",
            latest_event_date,
        )
        print(
            "Note: events removed from later NOAA "
            "revisions were not automatically deleted."
        )

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()