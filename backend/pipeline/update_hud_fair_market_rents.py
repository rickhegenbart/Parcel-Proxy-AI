import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from app.supabase_client import get_supabase


load_dotenv()

HUD_API_TOKEN = os.getenv("HUD_API_TOKEN", "").strip()

HUD_API_ROOT = "https://www.huduser.gov/hudapi/public/fmr"
HUD_ENTITY_ID = "3011199999"
COUNTY_FIPS = "30111"
STATE_CODE = "MT"

TARGET_TABLE = "hud_fair_market_rents"
PIPELINE_RUNS_TABLE = "data_pipeline_runs"
SOURCE_NAME = "HUD Fair Market Rents"


BEDROOM_FIELDS = {
    "Efficiency": "efficiency_rent",
    "One-Bedroom": "one_bedroom_rent",
    "Two-Bedroom": "two_bedroom_rent",
    "Three-Bedroom": "three_bedroom_rent",
    "Four-Bedroom": "four_bedroom_rent",
}


YOY_FIELDS = {
    "Efficiency": "efficiency_yoy_change_pct",
    "One-Bedroom": "one_bedroom_yoy_change_pct",
    "Two-Bedroom": "two_bedroom_yoy_change_pct",
    "Three-Bedroom": "three_bedroom_yoy_change_pct",
    "Four-Bedroom": "four_bedroom_yoy_change_pct",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_hud_token() -> None:
    if not HUD_API_TOKEN:
        raise RuntimeError(
            "HUD_API_TOKEN is missing. Add it to backend/.env "
            "and to the GitHub Actions repository secrets."
        )


def build_data_url(
    entity_id: str,
    fiscal_year: Optional[int] = None,
) -> str:
    url = f"{HUD_API_ROOT}/data/{entity_id}"

    if fiscal_year is not None:
        url = f"{url}?{urlencode({'year': fiscal_year})}"

    return url


def hud_request(
    entity_id: str,
    fiscal_year: Optional[int] = None,
) -> Dict[str, Any]:
    url = build_data_url(
        entity_id,
        fiscal_year,
    )

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {HUD_API_TOKEN}",
            "Accept": "application/json",
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "(HUD Fair Market Rent updater)"
            ),
        },
    )

    try:
        with urlopen(request, timeout=90) as response:
            body = response.read().decode("utf-8")
    except HTTPError as error:
        raise RuntimeError(
            f"HUD API returned HTTP {error.code} "
            f"for fiscal year {fiscal_year}."
        ) from error
    except URLError as error:
        raise RuntimeError(
            f"HUD API request failed: {error}"
        ) from error

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "HUD API returned a non-JSON response: "
            f"{body[:300]}"
        ) from error

    data = payload.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            "HUD API response did not contain a data object."
        )

    basic_data = data.get("basicdata")

    if not isinstance(basic_data, dict):
        raise RuntimeError(
            "HUD API response did not contain basicdata."
        )

    return data


def discover_latest_fiscal_year() -> Tuple[int, Dict[str, Any]]:
    current_year = datetime.now(timezone.utc).year

    # HUD fiscal-year data may be published before the matching
    # calendar year begins. Start with next year and fall back.
    for fiscal_year in range(
        current_year + 1,
        current_year - 5,
        -1,
    ):
        try:
            data = hud_request(
                HUD_ENTITY_ID,
                fiscal_year,
            )

            returned_year = clean_integer(
                data.get("basicdata", {}).get("year")
            )

            if returned_year == fiscal_year:
                return fiscal_year, data

        except RuntimeError:
            continue

    raise RuntimeError(
        "No usable HUD Fair Market Rent release was found "
        "among the recent fiscal years."
    )


def clean_numeric(
    value: Any,
) -> Optional[float]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    try:
        number = float(text)
    except (TypeError, ValueError):
        return None

    if number < 0:
        return None

    return number


def clean_integer(
    value: Any,
) -> Optional[int]:
    number = clean_numeric(value)

    if number is None:
        return None

    return int(round(number))


def calculate_change_pct(
    current_value: Optional[float],
    previous_value: Optional[float],
) -> Optional[float]:
    if current_value is None or previous_value is None:
        return None

    if previous_value <= 0:
        return None

    return round(
        ((current_value / previous_value) - 1) * 100,
        2,
    )


def parse_metro_status(
    value: Any,
) -> Optional[bool]:
    if value is None:
        return None

    text = str(value).strip().lower()

    if text in {"1", "1.0", "true", "yes"}:
        return True

    if text in {"0", "0.0", "false", "no"}:
        return False

    return None


def build_row(
    current_data: Dict[str, Any],
    previous_data: Optional[Dict[str, Any]],
    fiscal_year: int,
) -> Dict[str, Any]:
    current_basic = current_data["basicdata"]
    previous_basic = (
        previous_data.get("basicdata", {})
        if previous_data
        else {}
    )

    row: Dict[str, Any] = {
        "hud_entity_id": HUD_ENTITY_ID,
        "county_fips": COUNTY_FIPS,
        "county_name": (
            current_data.get("county_name")
            or "Yellowstone County, MT"
        ),
        "state_code": STATE_CODE,
        "metro_status": parse_metro_status(
            current_data.get("metro_status")
        ),
        "metro_name": current_data.get("metro_name"),
        "area_name": current_data.get("area_name"),
        "fiscal_year": fiscal_year,
        "source_name": SOURCE_NAME,
        "source_url": build_data_url(
            HUD_ENTITY_ID,
            fiscal_year,
        ),
        "raw_payload": current_data,
        "updated_at": utc_now(),
    }

    for hud_field, destination_field in BEDROOM_FIELDS.items():
        row[destination_field] = clean_numeric(
            current_basic.get(hud_field)
        )

    for hud_field, destination_field in YOY_FIELDS.items():
        current_value = clean_numeric(
            current_basic.get(hud_field)
        )
        previous_value = clean_numeric(
            previous_basic.get(hud_field)
        )

        row[destination_field] = calculate_change_pct(
            current_value,
            previous_value,
        )

    return row


def validate_row(
    row: Dict[str, Any],
    fiscal_year: int,
) -> None:
    if row["fiscal_year"] != fiscal_year:
        raise RuntimeError(
            "Prepared HUD row has an incorrect fiscal year."
        )

    if row["county_fips"] != COUNTY_FIPS:
        raise RuntimeError(
            "Prepared HUD row has an incorrect county FIPS."
        )

    rents = [
        row[field]
        for field in BEDROOM_FIELDS.values()
    ]

    if any(value is None for value in rents):
        raise RuntimeError(
            "One or more HUD bedroom rent values are missing."
        )

    if any(value <= 0 for value in rents):
        raise RuntimeError(
            "One or more HUD bedroom rent values are invalid."
        )

    # FMRs should normally increase with additional bedrooms.
    if rents != sorted(rents):
        raise RuntimeError(
            "HUD bedroom rents are not in ascending order."
        )

    for field in YOY_FIELDS.values():
        value = row.get(field)

        if value is not None and abs(value) > 50:
            raise RuntimeError(
                f"Implausible HUD annual change in {field}: "
                f"{value}%"
            )


def start_pipeline_run(client) -> Optional[str]:
    try:
        result = (
            client
            .table(PIPELINE_RUNS_TABLE)
            .insert({
                "source_name": SOURCE_NAME,
                "job_name": (
                    "update_hud_fair_market_rents"
                ),
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


def upsert_row(
    client,
    row: Dict[str, Any],
) -> int:
    (
        client
        .table(TARGET_TABLE)
        .upsert(
            row,
            on_conflict="county_fips,fiscal_year",
        )
        .execute()
    )

    return 1


def main() -> None:
    require_hud_token()

    client = get_supabase()
    run_id = start_pipeline_run(client)

    fiscal_year: Optional[int] = None
    rows_read = 0
    rows_written = 0

    try:
        print("Discovering latest HUD FMR release...")
        fiscal_year, current_data = (
            discover_latest_fiscal_year()
        )
        rows_read += 1

        print("Latest HUD fiscal year:", fiscal_year)

        previous_year = fiscal_year - 1
        previous_data: Optional[Dict[str, Any]] = None

        print(
            "Downloading prior HUD fiscal year:",
            previous_year,
        )

        try:
            previous_data = hud_request(
                HUD_ENTITY_ID,
                previous_year,
            )
            rows_read += 1
        except RuntimeError as error:
            print(
                "Warning: prior-year HUD data unavailable:",
                error,
            )

        print("Preparing HUD Fair Market Rent row...")
        row = build_row(
            current_data,
            previous_data,
            fiscal_year,
        )

        print("Validating HUD Fair Market Rents...")
        validate_row(row, fiscal_year)

        print("Updating HUD Fair Market Rents...")
        rows_written = upsert_row(client, row)

        validation_summary = {
            "fiscal_year": fiscal_year,
            "county_fips": COUNTY_FIPS,
            "area_name": row["area_name"],
            "efficiency_rent":
                row["efficiency_rent"],
            "one_bedroom_rent":
                row["one_bedroom_rent"],
            "two_bedroom_rent":
                row["two_bedroom_rent"],
            "three_bedroom_rent":
                row["three_bedroom_rent"],
            "four_bedroom_rent":
                row["four_bedroom_rent"],
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=f"FY {fiscal_year}",
            rows_read=rows_read,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("HUD Fair Market Rent update succeeded.")
        print("Fiscal year:", fiscal_year)
        print("Area:", row["area_name"])
        print(
            "Efficiency:",
            f"${row['efficiency_rent']:,.0f}",
        )
        print(
            "One bedroom:",
            f"${row['one_bedroom_rent']:,.0f}",
        )
        print(
            "Two bedroom:",
            f"${row['two_bedroom_rent']:,.0f}",
        )
        print(
            "Three bedroom:",
            f"${row['three_bedroom_rent']:,.0f}",
        )
        print(
            "Four bedroom:",
            f"${row['four_bedroom_rent']:,.0f}",
        )

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            source_period=(
                f"FY {fiscal_year}"
                if fiscal_year is not None
                else None
            ),
            rows_read=rows_read,
            rows_written=rows_written,
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()