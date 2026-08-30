from datetime import datetime, timezone
from io import StringIO
import subprocess

import pandas as pd

from app.supabase_client import get_supabase


FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
BATCH_SIZE = 500


def nullable_float(value):
    """Convert a pandas numeric value to a JSON-compatible float."""
    if pd.isna(value):
        return None

    return float(value)


def download_fred_series(series_id):
    """Download and clean one complete FRED series."""
    url = FRED_URL.format(series_id=series_id)

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
            "180",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=200,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FRED download failed for {series_id}: "
            f"{result.stderr.strip()}"
        )

    content = result.stdout

    data = pd.read_csv(StringIO(content))

    if len(data.columns) != 2:
        raise RuntimeError(
            f"Unexpected FRED columns for {series_id}: "
            f"{list(data.columns)}"
        )

    data.columns = ["indicator_date", "value"]

    data["indicator_date"] = pd.to_datetime(
        data["indicator_date"],
        errors="coerce",
    )

    data["value"] = pd.to_numeric(
        data["value"],
        errors="coerce",
    )

    data = (
        data.dropna(subset=["indicator_date", "value"])
        .sort_values("indicator_date")
        .drop_duplicates(subset=["indicator_date"], keep="last")
        .reset_index(drop=True)
    )

    if data.empty:
        raise RuntimeError(
            f"FRED returned no usable data for {series_id}"
        )

    return data


def build_mortgage_rows():
    """Create calculated mortgage-rate indicator records."""
    data = download_fred_series("MORTGAGE30US")

    data["weekly_change"] = data["value"].diff(1)
    data["four_week_change"] = data["value"].diff(4)
    data["annual_change"] = data["value"].diff(52)
    data["four_week_average"] = data["value"].rolling(4).mean()
    data["thirteen_week_average"] = data["value"].rolling(13).mean()

    rows = []

    for _, row in data.iterrows():
        rows.append({
            "series_id": "MORTGAGE30US",
            "indicator_name":
                "30-Year Fixed Rate Mortgage Average",
            "indicator_date":
                row["indicator_date"].date().isoformat(),
            "mortgage_rate":
                float(row["value"]),
            "mortgage_rate_change_weekly":
                nullable_float(row["weekly_change"]),
            "mortgage_rate_change_4week":
                nullable_float(row["four_week_change"]),
            "mortgage_rate_change_52week":
                nullable_float(row["annual_change"]),
            "mortgage_rate_4week_avg":
                nullable_float(row["four_week_average"]),
            "mortgage_rate_13week_avg":
                nullable_float(row["thirteen_week_average"]),
            "frequency": "weekly",
            "geography_type": "national",
            "geography_name": "United States",
            "source_name": "FRED MORTGAGE30US",
            "source_file": "MORTGAGE30US.csv",
        })

    return rows


def build_unemployment_rows():
    """Create calculated unemployment indicator records."""
    data = download_fred_series("UNRATE")

    data["monthly_change"] = data["value"].diff(1)
    data["three_month_change"] = data["value"].diff(3)
    data["annual_change"] = data["value"].diff(12)
    data["three_month_average"] = data["value"].rolling(3).mean()
    data["annual_average"] = data["value"].rolling(12).mean()
    data["pressure_score"] = data["value"] * 10

    rows = []

    for _, row in data.iterrows():
        rows.append({
            "series_id": "UNRATE",
            "indicator_name": "Unemployment Rate",
            "indicator_date":
                row["indicator_date"].date().isoformat(),
            "unemployment_rate":
                float(row["value"]),
            "unemployment_rate_change_monthly":
                nullable_float(row["monthly_change"]),
            "unemployment_rate_change_3month":
                nullable_float(row["three_month_change"]),
            "unemployment_rate_change_12month":
                nullable_float(row["annual_change"]),
            "unemployment_rate_3month_avg":
                nullable_float(row["three_month_average"]),
            "unemployment_rate_12month_avg":
                nullable_float(row["annual_average"]),
            "unemployment_pressure_score":
                nullable_float(row["pressure_score"]),
            "frequency": "monthly",
            "geography_type": "national",
            "geography_name": "United States",
            "source_name": "FRED UNRATE",
            "source_file": "UNRATE.csv",
        })

    return rows


def validate_rows(mortgage_rows, unemployment_rows):
    """Stop the pipeline before writing suspicious data."""
    if len(mortgage_rows) < 52:
        raise RuntimeError(
            "Mortgage history contains fewer than 52 rows"
        )

    if len(unemployment_rows) < 12:
        raise RuntimeError(
            "Unemployment history contains fewer than 12 rows"
        )

    latest_mortgage = mortgage_rows[-1]["mortgage_rate"]
    latest_unemployment = (
        unemployment_rows[-1]["unemployment_rate"]
    )

    if not 0 <= latest_mortgage <= 25:
        raise RuntimeError(
            "Latest mortgage rate is outside the 0-25% range"
        )

    if not 0 <= latest_unemployment <= 30:
        raise RuntimeError(
            "Latest unemployment rate is outside the 0-30% range"
        )


def upsert_batches(client, table, rows, conflict_columns):
    """Upsert records in batches using the table's unique index."""
    rows_written = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]

        (
            client
            .table(table)
            .upsert(
                batch,
                on_conflict=conflict_columns,
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def start_pipeline_run(client):
    """Create an audit record for this pipeline execution."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name": "Federal Reserve Economic Data",
            "job_name": "update_fred_indicators",
            "status": "running",
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the pipeline audit record"
        )

    return result.data[0]["id"]


def finish_pipeline_run(client, run_id, status, **values):
    """Record the final pipeline result."""
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
        print("Downloading FRED data...")

        mortgage_rows = build_mortgage_rows()
        unemployment_rows = build_unemployment_rows()

        print("Validating downloaded data...")

        validate_rows(
            mortgage_rows,
            unemployment_rows,
        )

        print("Updating mortgage indicators...")

        mortgage_written = upsert_batches(
            client,
            "mortgage_rate_indicators",
            mortgage_rows,
            "series_id,indicator_date",
        )

        print("Updating unemployment indicators...")

        unemployment_written = upsert_batches(
            client,
            "unemployment_rate_indicators",
            unemployment_rows,
            "series_id,indicator_date",
        )

        latest_period = max(
            mortgage_rows[-1]["indicator_date"],
            unemployment_rows[-1]["indicator_date"],
        )

        validation_summary = {
            "latest_mortgage_date":
                mortgage_rows[-1]["indicator_date"],
            "latest_mortgage_rate":
                mortgage_rows[-1]["mortgage_rate"],
            "latest_unemployment_date":
                unemployment_rows[-1]["indicator_date"],
            "latest_unemployment_rate":
                unemployment_rows[-1]["unemployment_rate"],
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=latest_period,
            rows_read=(
                len(mortgage_rows)
                + len(unemployment_rows)
            ),
            rows_written=(
                mortgage_written
                + unemployment_written
            ),
            validation_summary=validation_summary,
        )

        print()
        print("FRED update succeeded.")
        print(
            "Mortgage rows processed:",
            mortgage_written,
        )
        print(
            "Unemployment rows processed:",
            unemployment_written,
        )
        print(
            "Latest mortgage date:",
            mortgage_rows[-1]["indicator_date"],
        )
        print(
            "Latest mortgage rate:",
            mortgage_rows[-1]["mortgage_rate"],
        )
        print(
            "Latest unemployment date:",
            unemployment_rows[-1]["indicator_date"],
        )
        print(
            "Latest unemployment rate:",
            unemployment_rows[-1]["unemployment_rate"],
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

