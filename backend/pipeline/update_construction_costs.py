from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import pandas as pd
from dotenv import load_dotenv

from app.supabase_client import get_supabase


load_dotenv()

TARGET_TABLE = "construction_cost_indicators"
PIPELINE_RUNS_TABLE = "data_pipeline_runs"

FRED_SOURCE_NAME = "BLS Producer Price Index via FRED"
FRED_GRAPH_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

SERIES_CONFIG = {
    "WPUIP2310001": {
        "metric_name": "New construction goods index",
        "notes": (
            "National goods input index for new construction."
        ),
    },
    "WPUIP231100": {
        "metric_name": (
            "New residential construction input cost index"
        ),
        "notes": (
            "Broad national input cost index for new "
            "residential construction."
        ),
    },
    "WPUIP2311001": {
        "metric_name": "Residential construction goods index",
        "notes": (
            "National goods input index for residential "
            "construction."
        ),
    },
    "WPUIP231110": {
        "metric_name": (
            "Single-family residential construction input "
            "cost index"
        ),
        "notes": (
            "National input cost index for single-family "
            "residential construction."
        ),
    },
    "WPUIP2311101": {
        "metric_name": (
            "Single-family residential construction goods index"
        ),
        "notes": (
            "National goods input index for single-family "
            "residential construction."
        ),
    },
    "WPUIP2311201": {
        "metric_name": (
            "Multifamily residential construction goods index"
        ),
        "notes": (
            "National goods input index for multifamily "
            "residential construction."
        ),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_fred_url() -> str:
    parameters = {
        "id": ",".join(SERIES_CONFIG.keys()),
    }

    return f"{FRED_GRAPH_URL}?{urlencode(parameters)}"


def download_fred_data() -> pd.DataFrame:
    url = build_fred_url()

    try:
        data = pd.read_csv(
            url,
            dtype={
                series_id: "float64"
                for series_id in SERIES_CONFIG
            },
            low_memory=False,
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to download construction-cost data: {error}"
        ) from error

    if "observation_date" not in data.columns:
        raise RuntimeError(
            "FRED construction CSV is missing observation_date."
        )

    missing_series = set(SERIES_CONFIG).difference(
        data.columns
    )

    if missing_series:
        raise RuntimeError(
            "FRED construction CSV is missing series: "
            f"{sorted(missing_series)}"
        )

    data["observation_date"] = pd.to_datetime(
        data["observation_date"],
        errors="coerce",
    )

    data = (
        data.dropna(subset=["observation_date"])
        .sort_values("observation_date")
        .reset_index(drop=True)
    )

    if data.empty:
        raise RuntimeError(
            "FRED returned no usable construction observations."
        )

    return data


def calculate_change_pct(
    current_value: float,
    comparison_value: Optional[float],
) -> Optional[float]:
    if comparison_value is None:
        return None

    if pd.isna(comparison_value) or comparison_value == 0:
        return None

    change = (
        (current_value / float(comparison_value)) - 1
    ) * 100

    return round(change, 2)


def classify_cost_pressure(
    mom_change_pct: Optional[float],
    yoy_change_pct: Optional[float],
) -> str:
    if yoy_change_pct is None:
        return "Not enough history"

    if yoy_change_pct >= 8:
        return "High upward cost pressure"

    if yoy_change_pct >= 3:
        return "Moderate upward cost pressure"

    if yoy_change_pct >= 1:
        return "Mild upward cost pressure"

    if yoy_change_pct > -1:
        if (
            mom_change_pct is not None
            and mom_change_pct >= 1
        ):
            return "Recent upward cost pressure"

        if (
            mom_change_pct is not None
            and mom_change_pct <= -1
        ):
            return "Recent downward cost pressure"

        return "Stable construction costs"

    if yoy_change_pct > -3:
        return "Mild downward cost pressure"

    return "Downward cost pressure"


def build_series_row(
    data: pd.DataFrame,
    series_id: str,
    config: Dict[str, str],
) -> Dict[str, Any]:
    series = (
        data[["observation_date", series_id]]
        .dropna(subset=[series_id])
        .copy()
        .sort_values("observation_date")
        .reset_index(drop=True)
    )

    if len(series) < 13:
        raise RuntimeError(
            f"{series_id} has only {len(series)} usable "
            "observations; at least 13 are required."
        )

    latest = series.iloc[-1]
    latest_date = latest["observation_date"]
    latest_value = float(latest[series_id])

    previous_rows = series.loc[
        series["observation_date"] < latest_date
    ]

    previous_value = (
        float(previous_rows.iloc[-1][series_id])
        if not previous_rows.empty
        else None
    )

    year_ago_target = latest_date - pd.DateOffset(
        months=12
    )

    exact_year_ago = series.loc[
        series["observation_date"] == year_ago_target
    ]

    if not exact_year_ago.empty:
        year_ago_value = float(
            exact_year_ago.iloc[-1][series_id]
        )
    else:
        earlier_rows = series.loc[
            series["observation_date"] <= year_ago_target
        ]

        year_ago_value = (
            float(earlier_rows.iloc[-1][series_id])
            if not earlier_rows.empty
            else None
        )

    mom_change_pct = calculate_change_pct(
        latest_value,
        previous_value,
    )
    yoy_change_pct = calculate_change_pct(
        latest_value,
        year_ago_value,
    )

    return {
        "period": latest_date.date().isoformat(),
        "series_id": series_id,
        "metric_name": config["metric_name"],
        "metric_value": round(latest_value, 3),
        "metric_unit": "index",
        "mom_change_pct": mom_change_pct,
        "yoy_change_pct": yoy_change_pct,
        "cost_pressure_label": classify_cost_pressure(
            mom_change_pct,
            yoy_change_pct,
        ),
        "source_name": FRED_SOURCE_NAME,
        "geography_name": "United States",
        "geography_level": "national",
        "context_category": "construction_cost",
        "confidence_level": "context_only",
        "notes": config["notes"],
    }


def build_rows(
    data: pd.DataFrame,
) -> List[Dict[str, Any]]:
    return [
        build_series_row(
            data,
            series_id,
            config,
        )
        for series_id, config in SERIES_CONFIG.items()
    ]


def validate_rows(
    data: pd.DataFrame,
    rows: List[Dict[str, Any]],
) -> None:
    expected_series = set(SERIES_CONFIG)
    prepared_series = {
        row["series_id"]
        for row in rows
    }

    if prepared_series != expected_series:
        raise RuntimeError(
            "Prepared construction series do not match the "
            f"configuration. Expected {sorted(expected_series)}, "
            f"received {sorted(prepared_series)}."
        )

    if len(rows) != len(expected_series):
        raise RuntimeError(
            "Duplicate construction series were prepared."
        )

    latest_dates = {
        row["period"]
        for row in rows
    }

    if len(latest_dates) != 1:
        raise RuntimeError(
            "Construction series do not share the same latest "
            f"period: {sorted(latest_dates)}"
        )

    latest_date = pd.to_datetime(
        next(iter(latest_dates))
    )

    if latest_date < (
        pd.Timestamp.now().normalize()
        - pd.DateOffset(months=6)
    ):
        raise RuntimeError(
            "The latest construction-cost observation is more "
            f"than six months old: {latest_date.date()}."
        )

    for row in rows:
        value = row["metric_value"]

        if value is None or value <= 0:
            raise RuntimeError(
                "Invalid metric value for "
                f"{row['series_id']}: {value}"
            )

        mom = row["mom_change_pct"]
        yoy = row["yoy_change_pct"]

        if mom is not None and abs(mom) > 25:
            raise RuntimeError(
                "Implausible monthly change for "
                f"{row['series_id']}: {mom}%"
            )

        if yoy is not None and abs(yoy) > 50:
            raise RuntimeError(
                "Implausible annual change for "
                f"{row['series_id']}: {yoy}%"
            )

    if len(data) < 100:
        raise RuntimeError(
            "FRED returned unexpectedly little historical data."
        )


def start_pipeline_run(client) -> Optional[str]:
    try:
        result = (
            client
            .table(PIPELINE_RUNS_TABLE)
            .insert({
                "source_name": FRED_SOURCE_NAME,
                "job_name": "update_construction_costs",
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
    (
        client
        .table(TARGET_TABLE)
        .upsert(
            rows,
            on_conflict="series_id",
        )
        .execute()
    )

    return len(rows)


def main() -> None:
    client = get_supabase()
    run_id = start_pipeline_run(client)

    source_row_count = 0
    rows_written = 0
    source_period: Optional[str] = None

    try:
        print("Downloading FRED construction-cost data...")
        data = download_fred_data()
        source_row_count = len(data)

        print("Preparing latest construction indicators...")
        rows = build_rows(data)

        print("Validating construction indicators...")
        validate_rows(data, rows)

        source_period = rows[0]["period"]

        print("Updating construction-cost indicators...")
        rows_written = upsert_rows(client, rows)

        validation_summary = {
            "latest_period": source_period,
            "series_count": len(rows),
            "series_ids": sorted(
                row["series_id"]
                for row in rows
            ),
            "latest_values": {
                row["series_id"]: row["metric_value"]
                for row in rows
            },
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=source_period,
            rows_read=source_row_count,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("Construction-cost update succeeded.")
        print("Historical periods read:", source_row_count)
        print("Series processed:", rows_written)
        print("Latest period:", source_period)

        for row in rows:
            print(
                row["series_id"],
                "=>",
                row["metric_value"],
                "| MoM:",
                row["mom_change_pct"],
                "| YoY:",
                row["yoy_change_pct"],
                "|",
                row["cost_pressure_label"],
            )

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            source_period=source_period,
            rows_read=source_row_count,
            rows_written=rows_written,
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()