"""Update FEMA National Risk Index context indicators."""

import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.supabase_client import get_supabase


TRACT_SERVICE = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/"
    "arcgis/rest/services/"
    "National_Risk_Index_Census_Tracts/"
    "FeatureServer/0"
)
COUNTY_SERVICE = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/"
    "arcgis/rest/services/"
    "National_Risk_Index_Counties/"
    "FeatureServer/0"
)

SOURCE_NAME = "FEMA National Risk Index"
SOURCE_URL = (
    "https://www.fema.gov/about/openfema/"
    "data-sets/national-risk-index-data"
)
TARGET_TABLE = "parcel_context_indicators"
CONTEXT_CATEGORY = "environmental_risk"

YELLOWSTONE_STCOFIPS = "30111"
YELLOWSTONE_CONTEXT_ID = "county:yellowstone_mt"

PAGE_SIZE = 1_000
UPSERT_BATCH_SIZE = 100

EXPECTED_TRACT_METRICS = 10
EXPECTED_COUNTY_METRICS = 11


TRACT_METRICS = [
    (
        "Overall natural-hazard risk",
        "RISK_SCORE",
        "RISK_RATNG",
    ),
    (
        "Expected annual loss",
        "EAL_SCORE",
        "EAL_RATNG",
    ),
    (
        "Social vulnerability",
        "SOVI_SCORE",
        "SOVI_RATNG",
    ),
    (
        "Community resilience",
        "RESL_SCORE",
        "RESL_RATNG",
    ),
    (
        "Wildfire risk",
        "WFIR_RISKS",
        "WFIR_RISKR",
    ),
    (
        "Hail risk",
        "HAIL_RISKS",
        "HAIL_RISKR",
    ),
    (
        "Drought risk",
        "DRGT_RISKS",
        "DRGT_RISKR",
    ),
    (
        "Heat-wave risk",
        "HWAV_RISKS",
        "HWAV_RISKR",
    ),
    (
        "Ice-storm risk",
        "ISTM_RISKS",
        "ISTM_RISKR",
    ),
    (
        "Winter-weather risk",
        "WNTW_RISKS",
        "WNTW_RISKR",
    ),
]

COUNTY_METRICS = [
    (
        "Overall natural hazard risk",
        "RISK_SCORE",
        "RISK_RATNG",
    ),
    (
        "Expected annual loss",
        "EAL_SCORE",
        "EAL_RATNG",
    ),
    (
        "Wildfire risk",
        "WFIR_RISKS",
        "WFIR_RISKR",
    ),
    (
        "Inland flooding risk",
        "IFLD_RISKS",
        "IFLD_RISKR",
    ),
    (
        "Hail risk",
        "HAIL_RISKS",
        "HAIL_RISKR",
    ),
    (
        "Drought risk",
        "DRGT_RISKS",
        "DRGT_RISKR",
    ),
    (
        "Heat wave risk",
        "HWAV_RISKS",
        "HWAV_RISKR",
    ),
    (
        "Cold wave risk",
        "CWAV_RISKS",
        "CWAV_RISKR",
    ),
    (
        "Lightning risk",
        "LTNG_RISKS",
        "LTNG_RISKR",
    ),
    (
        "Strong wind risk",
        "SWND_RISKS",
        "SWND_RISKR",
    ),
    (
        "Winter weather risk",
        "WNTW_RISKS",
        "WNTW_RISKR",
    ),
]


def request_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Request JSON with retries and explicit timeouts."""
    if params:
        url = f"{url}?{urlencode(params)}"

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "FEMA-NRI-Updater"
            )
        },
    )

    last_error = None

    for attempt in range(1, 4):
        try:
            with urlopen(
                request,
                timeout=60,
            ) as response:
                import json

                payload = json.load(response)

            if "error" in payload:
                raise RuntimeError(
                    "ArcGIS returned an error: "
                    f"{payload['error']}"
                )

            return payload

        except Exception as error:
            last_error = error

            if attempt < 3:
                time.sleep(attempt * 2)

    raise RuntimeError(
        f"Unable to retrieve FEMA NRI data: {last_error}"
    )


def query_features(
    service_url: str,
    where: str,
    fields: List[str],
) -> List[Dict[str, Any]]:
    """Query attributes from one FEMA ArcGIS layer."""
    payload = request_json(
        f"{service_url}/query",
        {
            "where": where,
            "outFields": ",".join(fields),
            "returnGeometry": "false",
            "orderByFields": fields[0],
            "resultRecordCount": 2_000,
            "f": "json",
        },
    )

    if payload.get("exceededTransferLimit"):
        raise RuntimeError(
            "FEMA ArcGIS query exceeded its transfer limit"
        )

    return [
        feature.get("attributes", {})
        for feature in payload.get("features", [])
    ]


def normalize_fips(value: Any) -> Optional[str]:
    """Normalize a FIPS value read as text or number."""
    if value is None:
        return None

    text = str(value).strip()

    if text.endswith(".0"):
        text = text[:-2]

    return text or None


def clean_text(value: Any) -> Optional[str]:
    """Return stripped text or None."""
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def clean_number(value: Any) -> Optional[float]:
    """Return a finite float or None."""
    if value is None:
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


def fetch_mapped_tract_ids(client) -> Set[str]:
    """Load all distinct mapped tract IDs using pagination."""
    tract_ids: Set[str] = set()
    start = 0

    while True:
        result = (
            client
            .table("parcel_census_tract_map")
            .select("tract_fips")
            .range(
                start,
                start + PAGE_SIZE - 1,
            )
            .execute()
        )

        rows = result.data or []

        for row in rows:
            tract_fips = normalize_fips(
                row.get("tract_fips")
            )

            if tract_fips:
                tract_ids.add(tract_fips)

        if len(rows) < PAGE_SIZE:
            break

        start += PAGE_SIZE

    if not tract_ids:
        raise RuntimeError(
            "No mapped Census tract IDs were found"
        )

    return tract_ids


def get_release_information() -> Tuple[str, str, str]:
    """Derive the NRI version, release label, and source date."""
    metadata = request_json(
        TRACT_SERVICE,
        {"f": "json"},
    )

    metadata_text = " ".join(
        str(metadata.get(field) or "")
        for field in [
            "name",
            "description",
            "serviceDescription",
            "copyrightText",
        ]
    )

    release_match = re.search(
        r"\b("
        r"January|February|March|April|May|June|"
        r"July|August|September|October|November|December"
        r")\s+(\d{4})\b",
        metadata_text,
        flags=re.IGNORECASE,
    )

    version_match = re.search(
        r"\b(?:version|v)\s*"
        r"(\d+\.\d+(?:\.\d+)?)\b",
        metadata_text,
        flags=re.IGNORECASE,
    )

    if release_match:
        month_name = release_match.group(1).title()
        year = int(release_match.group(2))
    else:
        sample = query_features(
            TRACT_SERVICE,
            "STATEABBRV = 'MT'",
            ["TRACTFIPS", "NRI_VER"],
        )

        release_values = {
            clean_text(row.get("NRI_VER"))
            for row in sample
            if clean_text(row.get("NRI_VER"))
        }

        if len(release_values) != 1:
            raise RuntimeError(
                "Unable to determine one FEMA NRI release"
            )

        release_value = next(iter(release_values))
        release_match = re.search(
            r"\b("
            r"January|February|March|April|May|June|"
            r"July|August|September|October|November|December"
            r")\s+(\d{4})\b",
            release_value,
            flags=re.IGNORECASE,
        )

        if not release_match:
            raise RuntimeError(
                "Unable to parse the FEMA NRI release date"
            )

        month_name = release_match.group(1).title()
        year = int(release_match.group(2))

    month_number = datetime.strptime(
        month_name,
        "%B",
    ).month

    source_date = (
        datetime(
            year,
            month_number,
            1,
            tzinfo=timezone.utc,
        )
        .date()
        .isoformat()
    )
    release_label = f"{month_name} {year}"

    if version_match:
        version = version_match.group(1)
        version_parts = version.split(".")

        if (
            len(version_parts) == 3
            and version_parts[-1] == "0"
        ):
            version = ".".join(version_parts[:-1])

        version_label = f"v{version}"
    else:
        # Preserve the currently documented release label if
        # ArcGIS omits the numeric version from layer metadata.
        version_label = "v1.20"

    return version_label, release_label, source_date


def metric_row(
    parcel_id: str,
    geography_name: str,
    geography_level: str,
    metric_name: str,
    metric_value: Any,
    metric_text: Any,
    metric_unit: str,
    source_period: str,
    source_date: str,
    notes: str,
) -> Dict[str, Any]:
    """Build one Supabase context row."""
    return {
        "parcel_id": parcel_id,
        "geography_name": geography_name,
        "geography_level": geography_level,
        "context_category": CONTEXT_CATEGORY,
        "metric_name": metric_name,
        "metric_value": clean_number(metric_value),
        "metric_text": clean_text(metric_text),
        "metric_unit": metric_unit,
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "source_period": source_period,
        "source_date": source_date,
        "confidence_level": "context_only",
        "notes": notes,
    }


def build_tract_rows(
    source_features: List[Dict[str, Any]],
    mapped_tract_ids: Set[str],
    source_period: str,
    source_date: str,
) -> List[Dict[str, Any]]:
    """Build FEMA context metrics for mapped tracts."""
    source_by_tract = {
        normalize_fips(row.get("TRACTFIPS")): row
        for row in source_features
        if normalize_fips(row.get("TRACTFIPS"))
    }

    missing_tracts = (
        mapped_tract_ids
        .difference(source_by_tract)
    )

    if missing_tracts:
        raise RuntimeError(
            "Mapped tracts missing from FEMA NRI: "
            f"{sorted(missing_tracts)}"
        )

    rows: List[Dict[str, Any]] = []

    notes = (
        "Census-tract context from FEMA. Do not "
        "interpret as a property-specific appraisal, "
        "insurance quote, or guaranteed hazard outcome."
    )

    for tract_fips in sorted(mapped_tract_ids):
        source = source_by_tract[tract_fips]
        tract = (
            clean_text(source.get("TRACT"))
            or tract_fips[-6:]
        )
        county = (
            clean_text(source.get("COUNTY"))
            or "Unknown"
        )
        state = (
            clean_text(source.get("STATEABBRV"))
            or "MT"
        )

        geography_name = (
            f"Census Tract {tract}, "
            f"{county} County, {state}"
        )

        for (
            metric_name,
            score_field,
            rating_field,
        ) in TRACT_METRICS:
            rows.append(
                metric_row(
                    parcel_id=f"tract:{tract_fips}",
                    geography_name=geography_name,
                    geography_level="census_tract",
                    metric_name=metric_name,
                    metric_value=source.get(
                        score_field
                    ),
                    metric_text=source.get(
                        rating_field
                    ),
                    metric_unit="FEMA NRI score",
                    source_period=source_period,
                    source_date=source_date,
                    notes=notes,
                )
            )

    return rows


def build_county_rows(
    source_features: List[Dict[str, Any]],
    source_period: str,
    source_date: str,
) -> List[Dict[str, Any]]:
    """Build Yellowstone County fallback metrics."""
    matches = [
        row
        for row in source_features
        if normalize_fips(
            row.get("STCOFIPS")
        ) == YELLOWSTONE_STCOFIPS
    ]

    if len(matches) != 1:
        raise RuntimeError(
            "Expected one Yellowstone County FEMA row; "
            f"found {len(matches)}"
        )

    source = matches[0]
    rows: List[Dict[str, Any]] = []

    notes = (
        "FEMA National Risk Index composite "
        "county-level natural hazard context. "
        "Not a parcel-specific risk determination "
        "or insurance quote."
    )

    for (
        metric_name,
        score_field,
        rating_field,
    ) in COUNTY_METRICS:
        rows.append(
            metric_row(
                parcel_id=YELLOWSTONE_CONTEXT_ID,
                geography_name=(
                    "Yellowstone County, MT"
                ),
                geography_level="county",
                metric_name=metric_name,
                metric_value=source.get(
                    score_field
                ),
                metric_text=source.get(
                    rating_field
                ),
                metric_unit=(
                    "FEMA NRI score/rating"
                ),
                source_period=source_period,
                source_date=source_date,
                notes=notes,
            )
        )

    return rows


def validate_rows(
    tract_rows: List[Dict[str, Any]],
    county_rows: List[Dict[str, Any]],
    mapped_tract_count: int,
) -> None:
    """Validate the complete FEMA result."""
    expected_tract_rows = (
        mapped_tract_count
        * EXPECTED_TRACT_METRICS
    )

    if len(tract_rows) != expected_tract_rows:
        raise RuntimeError(
            "Unexpected tract metric count: "
            f"{len(tract_rows)} instead of "
            f"{expected_tract_rows}"
        )

    if len(county_rows) != EXPECTED_COUNTY_METRICS:
        raise RuntimeError(
            "Unexpected county metric count: "
            f"{len(county_rows)}"
        )

    rows = tract_rows + county_rows

    unique_keys = {
        (
            row["parcel_id"],
            row["geography_name"],
            row["geography_level"],
            row["context_category"],
            row["metric_name"],
            row["source_name"],
        )
        for row in rows
    }

    if len(unique_keys) != len(rows):
        raise RuntimeError(
            "Duplicate FEMA context metric keys found"
        )

    missing_values = [
        (
            row["parcel_id"],
            row["metric_name"],
        )
        for row in rows
        if row["metric_value"] is None
    ]

    if missing_values:
        raise RuntimeError(
            "FEMA metrics contain missing scores: "
            f"{missing_values[:10]}"
        )


def upsert_batches(
    client,
    rows: List[Dict[str, Any]],
) -> int:
    """Upsert FEMA context rows in bounded batches."""
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
                on_conflict=(
                    "parcel_id,"
                    "geography_name,"
                    "geography_level,"
                    "context_category,"
                    "metric_name,"
                    "source_name"
                ),
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def start_pipeline_run(client):
    """Create a pipeline audit record."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name": SOURCE_NAME,
            "job_name": "update_fema_nri",
            "status": "running",
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the FEMA NRI "
            "pipeline audit record"
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


def main():
    client = get_supabase()
    run_id = start_pipeline_run(client)

    try:
        print("Loading mapped Census tracts...")
        mapped_tract_ids = fetch_mapped_tract_ids(
            client
        )

        print("Reading FEMA NRI release metadata...")
        (
            version_label,
            release_label,
            source_date,
        ) = get_release_information()

        tract_fields = sorted({
            "TRACTFIPS",
            "TRACT",
            "COUNTY",
            "STATEABBRV",
            "NRI_VER",
            *[
                field
                for _, score, rating in TRACT_METRICS
                for field in [score, rating]
            ],
        })

        county_fields = sorted({
            "STCOFIPS",
            "COUNTY",
            "STATEABBRV",
            "NRI_VER",
            *[
                field
                for _, score, rating in COUNTY_METRICS
                for field in [score, rating]
            ],
        })

        print("Downloading Montana tract indicators...")
        tract_features = query_features(
            TRACT_SERVICE,
            "STATEABBRV = 'MT'",
            tract_fields,
        )

        print("Downloading Yellowstone County indicators...")
        county_features = query_features(
            COUNTY_SERVICE,
            (
                f"STCOFIPS = "
                f"'{YELLOWSTONE_STCOFIPS}'"
            ),
            county_fields,
        )

        tract_source_period = (
            f"NRI {version_label}, {release_label}"
        )

        print("Preparing FEMA context rows...")
        tract_rows = build_tract_rows(
            tract_features,
            mapped_tract_ids,
            tract_source_period,
            source_date,
        )
        county_rows = build_county_rows(
            county_features,
            release_label,
            source_date,
        )

        print("Validating FEMA context rows...")
        validate_rows(
            tract_rows,
            county_rows,
            len(mapped_tract_ids),
        )

        rows = tract_rows + county_rows

        print("Updating FEMA context indicators...")
        rows_written = upsert_batches(
            client,
            rows,
        )

        validation_summary = {
            "nri_version": version_label,
            "release_label": release_label,
            "mapped_tract_count": len(
                mapped_tract_ids
            ),
            "tract_metric_rows": len(
                tract_rows
            ),
            "county_metric_rows": len(
                county_rows
            ),
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=source_date,
            rows_read=(
                len(tract_features)
                + len(county_features)
            ),
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print("FEMA NRI update succeeded.")
        print(
            "Mapped tracts:",
            len(mapped_tract_ids),
        )
        print(
            "Tract metric rows:",
            len(tract_rows),
        )
        print(
            "County metric rows:",
            len(county_rows),
        )
        print("Rows processed:", rows_written)
        print(
            "Source period:",
            tract_source_period,
        )
        print("Source date:", source_date)

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