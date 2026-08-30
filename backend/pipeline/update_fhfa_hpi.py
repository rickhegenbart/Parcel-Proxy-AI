from datetime import datetime, timezone
from io import StringIO
import subprocess
from typing import Any, Dict, List, Tuple

import pandas as pd

from app.supabase_client import get_supabase


FHFA_URL = (
    "https://www.fhfa.gov/hpi/download/monthly/"
    "hpi_master.csv"
)
BATCH_SIZE = 500

TARGET_SERIES = {
    ("MT", "traditional", "purchase-only"),
    ("MT", "traditional", "all-transactions"),
    ("MT", "non-metro", "all-transactions"),
    ("MT", "traditional", "expanded-data"),
    ("13740", "traditional", "all-transactions"),
    ("13740", "traditional", "expanded-data"),
}

REQUIRED_COLUMNS = {
    "hpi_type",
    "hpi_flavor",
    "frequency",
    "level",
    "place_name",
    "place_id",
    "yr",
    "period",
    "index_nsa",
    "index_sa",
    "rstderr",
    "note",
}


def nullable_float(value):
    if pd.isna(value):
        return None

    return float(value)


def nullable_text(value):
    if pd.isna(value):
        return None

    text = str(value).strip()
    return text or None


def download_fhfa_master() -> pd.DataFrame:
    """Download the official FHFA master HPI CSV."""
    result = subprocess.run(
        [
            "curl",
            "-4",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--retry",
            "3",
            "--retry-delay",
            "5",
            "--connect-timeout",
            "30",
            "--max-time",
            "240",
            FHFA_URL,
        ],
        capture_output=True,
        text=True,
        timeout=260,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FHFA download failed: "
            f"{result.stderr.strip()}"
        )

    data = pd.read_csv(
        StringIO(result.stdout),
        dtype={"place_id": "string"},
        low_memory=False,
    )

    missing_columns = REQUIRED_COLUMNS.difference(data.columns)

    if missing_columns:
        raise RuntimeError(
            "FHFA CSV is missing required columns: "
            f"{sorted(missing_columns)}"
        )

    return data


def prepare_target_data(data: pd.DataFrame) -> pd.DataFrame:
    """Clean and select Montana and Billings quarterly series."""
    clean = data.copy()

    for column in (
        "hpi_type",
        "hpi_flavor",
        "frequency",
        "place_name",
        "place_id",
    ):
        clean[column] = (
            clean[column]
            .astype("string")
            .str.strip()
        )

    clean["frequency"] = clean["frequency"].str.lower()
    clean["hpi_type"] = clean["hpi_type"].str.lower()
    clean["hpi_flavor"] = clean["hpi_flavor"].str.lower()

    for column in (
        "yr",
        "period",
        "index_nsa",
        "index_sa",
        "rstderr",
    ):
        clean[column] = pd.to_numeric(
            clean[column],
            errors="coerce",
        )

    clean = clean.dropna(
        subset=[
            "place_id",
            "yr",
            "period",
            "index_nsa",
        ]
    )

    clean["yr"] = clean["yr"].astype(int)
    clean["period"] = clean["period"].astype(int)

    series_keys = list(
        zip(
            clean["place_id"],
            clean["hpi_type"],
            clean["hpi_flavor"],
        )
    )

    clean = clean[
        clean["frequency"].eq("quarterly")
        & pd.Series(
            [key in TARGET_SERIES for key in series_keys],
            index=clean.index,
        )
    ].copy()

    clean = clean[
        clean["period"].between(1, 4)
    ].copy()

    if clean.empty:
        raise RuntimeError(
            "FHFA contained no target Montana or Billings rows"
        )

    clean["period_start_date"] = pd.to_datetime(
        {
            "year": clean["yr"],
            "month": ((clean["period"] - 1) * 3) + 1,
            "day": 1,
        }
    )

    group_columns = [
        "place_id",
        "hpi_type",
        "hpi_flavor",
        "frequency",
    ]

    clean = clean.sort_values(
        group_columns + ["period_start_date"]
    ).reset_index(drop=True)

    grouped = clean.groupby(
        group_columns,
        dropna=False,
    )["index_nsa"]

    clean["hpi_period_change_pct"] = (
        grouped.pct_change(fill_method=None) * 100
    )

    clean["hpi_yoy_change_pct"] = (
        grouped.pct_change(
            periods=4,
            fill_method=None,
        )
        * 100
    )

    clean = clean.drop_duplicates(
        subset=group_columns + ["period_start_date"],
        keep="last",
    )

    return clean


def build_rows(data: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for _, row in data.iterrows():
        place_id = str(row["place_id"])

        if place_id == "13740":
            geo_scope = "billings_msa"
            geography_type = "msa"
            geography_name = "Billings, MT"
        else:
            geo_scope = "montana_state"
            geography_type = "state"
            geography_name = "Montana"

        rows.append({
            "geo_scope": geo_scope,
            "geography_type": geography_type,
            "geography_name": geography_name,
            "geography_id": place_id,
            "hpi_type": str(row["hpi_type"]),
            "hpi_flavor": str(row["hpi_flavor"]),
            "frequency": "quarterly",
            "year": int(row["yr"]),
            "period": str(int(row["period"])),
            "period_start_date":
                row["period_start_date"].date().isoformat(),
            "hpi_index": float(row["index_nsa"]),
            "hpi_index_nsa": float(row["index_nsa"]),
            "hpi_index_sa":
                nullable_float(row["index_sa"]),
            "hpi_period_change_pct":
                nullable_float(
                    row["hpi_period_change_pct"]
                ),
            "hpi_yoy_change_pct":
                nullable_float(
                    row["hpi_yoy_change_pct"]
                ),
            "standard_error":
                nullable_float(row["rstderr"]),
            "note": nullable_text(row["note"]),
            "source_name": "FHFA HPI master",
            "source_file": "hpi_master.csv",
        })

    return rows


def validate_rows(rows: List[Dict[str, Any]]) -> None:
    """Reject incomplete or suspicious FHFA results."""
    if len(rows) < 100:
        raise RuntimeError(
            "FHFA target history contains fewer than 100 rows"
        )

    found_series: set[Tuple[str, str, str]] = {
        (
            row["geography_id"],
            row["hpi_type"],
            row["hpi_flavor"],
        )
        for row in rows
    }

    missing_series = TARGET_SERIES.difference(found_series)

    if missing_series:
        raise RuntimeError(
            "FHFA target series are missing: "
            f"{sorted(missing_series)}"
        )

    latest_date = max(
        row["period_start_date"]
        for row in rows
    )

    latest_rows = [
        row
        for row in rows
        if row["period_start_date"] == latest_date
    ]

    if len(latest_rows) < len(TARGET_SERIES):
        raise RuntimeError(
            "Not all target FHFA series contain the latest quarter"
        )

    for row in latest_rows:
        if not 50 <= row["hpi_index"] <= 2000:
            raise RuntimeError(
                "Latest FHFA index is outside the expected range"
            )


def upsert_batches(client, rows):
    rows_written = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]

        (
            client
            .table("hpi_market_indicators")
            .upsert(
                batch,
                on_conflict=(
                    "geo_scope,"
                    "geography_id,"
                    "hpi_type,"
                    "hpi_flavor,"
                    "frequency,"
                    "period_start_date"
                ),
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def start_pipeline_run(client):
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name":
                "Federal Housing Finance Agency",
            "job_name": "update_fhfa_hpi",
            "status": "running",
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the FHFA pipeline audit record"
        )

    return result.data[0]["id"]


def finish_pipeline_run(
    client,
    run_id,
    status,
    **values,
):
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


def main():
    client = get_supabase()
    run_id = start_pipeline_run(client)

    try:
        print("Downloading FHFA HPI data...")
        source_data = download_fhfa_master()

        print("Preparing Montana and Billings series...")
        target_data = prepare_target_data(source_data)
        rows = build_rows(target_data)

        print("Validating FHFA rows...")
        validate_rows(rows)

        print("Updating HPI indicators...")
        rows_written = upsert_batches(client, rows)

        latest_date = max(
            row["period_start_date"]
            for row in rows
        )

        validation_summary = {
            "latest_hpi_date": latest_date,
            "target_series_count": len(TARGET_SERIES),
            "target_row_count": len(rows),
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=latest_date,
            rows_read=len(source_data),
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("FHFA HPI update succeeded.")
        print("Rows processed:", rows_written)
        print("Latest quarter:", latest_date)

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