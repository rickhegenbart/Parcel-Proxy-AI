from datetime import datetime, timezone
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List

import pandas as pd

from app.supabase_client import get_supabase


REALTOR_URL = (
    "https://econdata.s3-us-west-2.amazonaws.com/"
    "Reports/Core/"
    "RDC_Inventory_Core_Metrics_County_History.csv"
)
BATCH_SIZE = 500
YELLOWSTONE_FIPS = "30111"

DIRECT_NUMERIC_COLUMNS = [
    "median_listing_price",
    "median_listing_price_mm",
    "median_listing_price_yy",
    "active_listing_count",
    "active_listing_count_mm",
    "active_listing_count_yy",
    "median_days_on_market",
    "median_days_on_market_mm",
    "median_days_on_market_yy",
    "new_listing_count",
    "new_listing_count_mm",
    "new_listing_count_yy",
    "pending_listing_count",
    "pending_listing_count_mm",
    "pending_listing_count_yy",
    "price_increased_count",
    "price_increased_count_mm",
    "price_increased_count_yy",
    "price_reduced_count",
    "price_reduced_count_mm",
    "price_reduced_count_yy",
    "median_listing_price_per_square_foot",
    "median_listing_price_per_square_foot_mm",
    "median_listing_price_per_square_foot_yy",
    "median_square_feet",
    "median_square_feet_mm",
    "median_square_feet_yy",
    "average_listing_price",
    "total_listing_count",
    "price_reduced_share",
    "pending_ratio",
    "quality_flag",
]

REQUIRED_COLUMNS = {
    "month_date_yyyymm",
    "county_fips",
    "county_name",
    *DIRECT_NUMERIC_COLUMNS,
}


def nullable_float(value):
    """Convert a pandas value to a JSON-compatible float."""
    if pd.isna(value):
        return None

    return float(value)


def download_realtor_history() -> pd.DataFrame:
    """
    Download the official county-history file in a temporary file.

    Read it in chunks and retain only Yellowstone County records to
    avoid loading the entire national dataset into memory.
    """
    with NamedTemporaryFile(suffix=".csv") as temp_file:
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
                "300",
                "--output",
                temp_file.name,
                REALTOR_URL,
            ],
            capture_output=True,
            text=True,
            timeout=320,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "Realtor.com download failed: "
                f"{result.stderr.strip()}"
            )

        header = pd.read_csv(
            temp_file.name,
            nrows=0,
        )

        missing_columns = REQUIRED_COLUMNS.difference(
            header.columns
        )

        if missing_columns:
            raise RuntimeError(
                "Realtor.com CSV is missing required columns: "
                f"{sorted(missing_columns)}"
            )

        yellowstone_chunks = []

        for chunk in pd.read_csv(
            temp_file.name,
            usecols=sorted(REQUIRED_COLUMNS),
            dtype={"county_fips": "string"},
            chunksize=50_000,
            low_memory=False,
        ):
            county_fips = (
                chunk["county_fips"]
                .astype("string")
                .str.strip()
                .str.zfill(5)
            )

            selected = chunk[
                county_fips.eq(YELLOWSTONE_FIPS)
            ].copy()

            if not selected.empty:
                selected["county_fips"] = YELLOWSTONE_FIPS
                yellowstone_chunks.append(selected)

        if not yellowstone_chunks:
            raise RuntimeError(
                "Yellowstone County was not found "
                "in Realtor.com data"
            )

        return pd.concat(
            yellowstone_chunks,
            ignore_index=True,
        )


def safe_ratio(
    numerator: pd.Series,
    denominator: pd.Series,
) -> pd.Series:
    """Divide two numeric series while treating zero as missing."""
    valid_denominator = denominator.where(
        denominator.ne(0)
    )

    ratio = numerator.div(valid_denominator)

    return pd.to_numeric(
        ratio,
        errors="coerce",
    )


def prepare_yellowstone_data(
    data: pd.DataFrame,
) -> pd.DataFrame:
    """Clean Yellowstone history and calculate model fields."""
    clean = data.copy()

    clean["county_fips"] = (
        clean["county_fips"]
        .astype("string")
        .str.strip()
        .str.zfill(5)
    )

    clean = clean[
        clean["county_fips"].eq(YELLOWSTONE_FIPS)
    ].copy()

    if clean.empty:
        raise RuntimeError(
            "Yellowstone County was not found "
            "in Realtor.com data"
        )

    clean["month_date"] = pd.to_datetime(
        clean["month_date_yyyymm"]
        .astype("string")
        .str.strip(),
        format="%Y%m",
        errors="coerce",
    )

    for column in DIRECT_NUMERIC_COLUMNS:
        clean[column] = pd.to_numeric(
            clean[column],
            errors="coerce",
        )

    clean = (
        clean
        .dropna(subset=["month_date"])
        .sort_values("month_date")
        .drop_duplicates(
            subset=["county_fips", "month_date"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    if clean.empty:
        raise RuntimeError(
            "Yellowstone County contained no valid monthly rows"
        )

    price = clean["median_listing_price"]
    price_per_sqft = (
        clean["median_listing_price_per_square_foot"]
    )
    active = clean["active_listing_count"]

    clean["median_listing_price_change_monthly"] = (
        price.diff(1)
    )
    clean["median_listing_price_change_3month"] = (
        price.diff(3)
    )
    clean["median_listing_price_change_12month"] = (
        price.diff(12)
    )
    clean["median_listing_price_3month_avg"] = (
        price.rolling(3).mean()
    )
    clean["median_listing_price_12month_avg"] = (
        price.rolling(12).mean()
    )

    clean["price_per_sqft_change_12month"] = (
        price_per_sqft.diff(12)
    )
    clean["active_listing_count_change_3month"] = (
        active.diff(3)
    )
    clean["active_listing_count_change_12month"] = (
        active.diff(12)
    )

    calculated_price_reduction_share = safe_ratio(
        clean["price_reduced_count"],
        active,
    )

    # Preserve the feature definition used by the trained model:
    # price-reduced listings divided by active listings. Realtor.com's
    # price_reduced_share field uses a different denominator.
    clean["price_reduction_share"] = (
        calculated_price_reduction_share
    )

    clean["pending_to_active_ratio"] = safe_ratio(
        clean["pending_listing_count"],
        active,
    )

    clean["new_listing_to_active_ratio"] = safe_ratio(
        clean["new_listing_count"],
        active,
    )

    # Realtor.com does not provide this custom field in the
    # inventory file. Use a stable and documented composite.
    clean["market_heat_score"] = (
        50
        + (
            25
            * clean["pending_to_active_ratio"].fillna(0)
        )
        + (
            25
            * clean["new_listing_to_active_ratio"].fillna(0)
        )
        - (
            15
            * clean["price_reduction_share"].fillna(0)
        )
    ).clip(lower=0, upper=100)

    return clean


def build_rows(
    data: pd.DataFrame,
) -> List[Dict[str, Any]]:
    """Convert the cleaned dataframe to Supabase records."""
    rows: List[Dict[str, Any]] = []

    mapped_columns = [
        "median_listing_price",
        "median_listing_price_mm",
        "median_listing_price_yy",
        "active_listing_count",
        "active_listing_count_mm",
        "active_listing_count_yy",
        "median_days_on_market",
        "median_days_on_market_mm",
        "median_days_on_market_yy",
        "new_listing_count",
        "new_listing_count_mm",
        "new_listing_count_yy",
        "pending_listing_count",
        "pending_listing_count_mm",
        "pending_listing_count_yy",
        "price_increased_count",
        "price_increased_count_mm",
        "price_increased_count_yy",
        "price_reduced_count",
        "price_reduced_count_mm",
        "price_reduced_count_yy",
        "median_listing_price_per_square_foot",
        "median_listing_price_per_square_foot_mm",
        "median_listing_price_per_square_foot_yy",
        "median_square_feet",
        "median_square_feet_mm",
        "median_square_feet_yy",
        "average_listing_price",
        "total_listing_count",
        "median_listing_price_change_monthly",
        "median_listing_price_change_3month",
        "median_listing_price_change_12month",
        "median_listing_price_3month_avg",
        "median_listing_price_12month_avg",
        "price_per_sqft_change_12month",
        "active_listing_count_change_3month",
        "active_listing_count_change_12month",
        "price_reduction_share",
        "pending_to_active_ratio",
        "new_listing_to_active_ratio",
        "market_heat_score",
    ]

    for _, row in data.iterrows():
        output: Dict[str, Any] = {
            "month_date":
                row["month_date"].date().isoformat(),
            "county_fips": YELLOWSTONE_FIPS,
            "county_name": "Yellowstone County",
            "state": "MT",
            "state_name": "Montana",
            "source_name": (
                "Realtor.com RDC Inventory Core "
                "Metrics County"
            ),
            "source_file": (
                "RDC_Inventory_Core_Metrics_"
                "County_History.csv"
            ),
        }

        for column in mapped_columns:
            output[column] = nullable_float(
                row[column]
            )

        rows.append(output)

    return rows


def validate_rows(
    source_data: pd.DataFrame,
    yellowstone_data: pd.DataFrame,
    rows: List[Dict[str, Any]],
) -> None:
    """Reject incomplete or suspicious source data."""
    if len(source_data) < 60:
        raise RuntimeError(
            "Yellowstone source contains fewer than 60 months"
        )

    if len(rows) < 60:
        raise RuntimeError(
            "Yellowstone history contains fewer than 60 months"
        )

    latest_source = yellowstone_data.iloc[-1]
    latest_row = rows[-1]

    if nullable_float(
        latest_source["quality_flag"]
    ) == 1:
        raise RuntimeError(
            "Latest Yellowstone County row has quality_flag=1"
        )

    required_latest_values = [
        "median_listing_price",
        "active_listing_count",
        "median_days_on_market",
    ]

    for field in required_latest_values:
        if latest_row.get(field) is None:
            raise RuntimeError(
                f"Latest Realtor.com row is missing {field}"
            )

    price = latest_row["median_listing_price"]
    active = latest_row["active_listing_count"]

    if not 50_000 <= price <= 5_000_000:
        raise RuntimeError(
            "Latest median listing price is outside "
            "the expected range"
        )

    if not 1 <= active <= 100_000:
        raise RuntimeError(
            "Latest active listing count is outside "
            "the expected range"
        )

    month_dates = [
        row["month_date"]
        for row in rows
    ]

    if len(month_dates) != len(set(month_dates)):
        raise RuntimeError(
            "Yellowstone history contains duplicate months"
        )


def upsert_batches(client, rows):
    """Upsert monthly records using the existing unique index."""
    rows_written = 0

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]

        (
            client
            .table("realtor_county_market_indicators")
            .upsert(
                batch,
                on_conflict=(
                    "county_fips,"
                    "county_name,"
                    "state,"
                    "month_date"
                ),
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def start_pipeline_run(client):
    """Create an audit record for the pipeline execution."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name":
                "Realtor.com Economic Research",
            "job_name": "update_realtor_inventory",
            "status": "running",
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the Realtor.com audit record"
        )

    return result.data[0]["id"]


def finish_pipeline_run(
    client,
    run_id,
    status,
    **values,
):
    """Record the final pipeline status."""
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
        print("Downloading Realtor.com county history...")
        source_data = download_realtor_history()

        print("Preparing Yellowstone County data...")
        yellowstone_data = prepare_yellowstone_data(
            source_data
        )
        rows = build_rows(yellowstone_data)

        print("Validating Realtor.com rows...")
        validate_rows(
            source_data,
            yellowstone_data,
            rows,
        )

        print("Updating county market indicators...")
        rows_written = upsert_batches(
            client,
            rows,
        )

        latest_date = rows[-1]["month_date"]

        validation_summary = {
            "latest_realtor_date": latest_date,
            "yellowstone_row_count": len(rows),
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
        print("Realtor.com update succeeded.")
        print("Rows processed:", rows_written)
        print("Latest month:", latest_date)

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
