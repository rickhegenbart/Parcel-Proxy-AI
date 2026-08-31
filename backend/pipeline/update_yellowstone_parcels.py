"""Update Yellowstone County parcel records from Montana Cadastral data."""

import gc
import json
import math
import os
import re
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import pandas as pd

# Avoid GDAL's memory-intensive full polygon-ring analysis.
os.environ.setdefault("OGR_ORGANIZE_POLYGONS", "ONLY_CCW")

import pyogrio

from app.supabase_client import get_supabase


SOURCE_URL = (
    "https://ftpgeoinfo.msl.mt.gov/Data/Spatial/MSDI/"
    "Cadastral/Parcels/Yellowstone/Yellowstone_GDB.zip"
)
SOURCE_FILE = "Yellowstone_GDB.zip"
SOURCE_NAME = "Montana Cadastral Yellowstone_GDB OwnerParcel"
LAYER_NAME = "OwnerParcel"
TARGET_TABLE = "parcel_property_records"

# Only this many spatial features are held in memory at once.
SOURCE_CHUNK_SIZE = 1_000
UPSERT_BATCH_SIZE = 250

MINIMUM_EXPECTED_ROWS = 75_000
MAXIMUM_ROW_DECREASE_PCT = 10.0
MAXIMUM_BLANK_IDS = 500


PROPERTY_TYPE_GROUPS = {
    "CA - Centrally Assessed": "centrally_assessed",
    "Condominium": "condominium",
    "Exempt Property": "exempt",
    "Partial Exempt Property": "exempt",
    "Golf Course": "golf_course",
    "Gravel Pit": "gravel_pit",
    "Improved Property": "improved_property",
    "Industrial Property": "industrial",
    "On Leased Land": "leased_land",
    "Mobile/RV Parks": "mobile_rv_park",
    "Apartment": "multifamily",
    "Non-Valued Property": "non_valued",
    "Non-Valued with Specials": "non_valued",
    "CN - Centrally Assessed Non-Valued Property": "other",
    "Townhouse": "townhouse",
    "Tribal Property": "tribal",
    "Vacant Land": "vacant_land",
}

RESIDENTIAL_FLAGS = {
    "Condominium": True,
    "Townhouse": True,
    "CA - Centrally Assessed": False,
    "Exempt Property": False,
    "Partial Exempt Property": False,
    "Golf Course": False,
    "Gravel Pit": False,
    "Industrial Property": False,
    "Mobile/RV Parks": False,
    "Apartment": False,
    "Non-Valued Property": False,
    "Non-Valued with Specials": False,
    "Tribal Property": False,
    "Vacant Land": False,
}

# Owner names and mailing-address fields are deliberately excluded.
RAW_PAYLOAD_FIELDS = [
    "PARCELID",
    "COUNTYCD",
    "CountyName",
    "CountyAbbr",
    "GISAcres",
    "TaxYear",
    "PropertyID",
    "AssessmentCode",
    "Township",
    "Range",
    "Section",
    "LegalDescriptionShort",
    "Subdivision",
    "CertificateOfSurvey",
    "AddressLine1",
    "AddressLine2",
    "CityStateZip",
    "PropAccess",
    "LevyDistrict",
    "PropType",
    "ContinuousCropAcres",
    "FallowAcres",
    "FarmsiteAcres",
    "ForestAcres",
    "GrazingAcres",
    "WildHayAcres",
    "IrrigatedAcres",
    "NonQualAcres",
    "TotalAcres",
    "TotalBuildingValue",
    "TotalLandValue",
    "TotalValue",
    "Shape_Length",
    "Shape_Area",
]

VALIDATION_FIELDS = [
    "PARCELID",
    "CountyName",
    "TaxYear",
]

CITY_STATE_ZIP_PATTERN = re.compile(
    r"^\s*(.+?),\s*([A-Za-z]{2})\s+"
    r"(\d{5}(?:-\d{4})?)\s*$"
)


def download_source(destination: Path) -> None:
    """Download the official Yellowstone County geodatabase."""
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
            "--output",
            str(destination),
            SOURCE_URL,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Yellowstone parcel download failed: "
            f"{result.stderr.strip()}"
        )

    if not destination.exists():
        raise RuntimeError(
            "Yellowstone parcel archive was not downloaded"
        )

    if destination.stat().st_size < 1_000_000:
        raise RuntimeError(
            "Downloaded Yellowstone parcel archive is "
            "unexpectedly small"
        )


def extract_source_date(archive_path: Path) -> str:
    """Use the newest archive-entry date as the source date."""
    with zipfile.ZipFile(archive_path) as archive:
        dated_entries = [
            entry.date_time
            for entry in archive.infolist()
            if not entry.is_dir()
        ]

    if not dated_entries:
        raise RuntimeError(
            "Yellowstone parcel archive contains no dated files"
        )

    newest = max(dated_entries)

    return datetime(
        *newest,
        tzinfo=timezone.utc,
    ).date().isoformat()


def locate_geodatabase(extract_directory: Path) -> Path:
    """Locate the extracted File Geodatabase."""
    geodatabases = list(
        extract_directory.rglob("*.gdb")
    )

    if len(geodatabases) != 1:
        raise RuntimeError(
            "Expected exactly one File Geodatabase; "
            f"found {len(geodatabases)}"
        )

    return geodatabases[0]


def get_source_info(
    geodatabase: Path,
) -> Dict[str, Any]:
    """Read source metadata without loading parcel geometries."""
    layers = pyogrio.list_layers(geodatabase)
    layer_names = set(layers[:, 0].tolist())

    if LAYER_NAME not in layer_names:
        raise RuntimeError(
            f"Layer {LAYER_NAME!r} is missing"
        )

    info = pyogrio.read_info(
        geodatabase,
        layer=LAYER_NAME,
    )

    if not info.get("crs"):
        raise RuntimeError(
            "Yellowstone parcel layer has no CRS"
        )

    feature_count = int(info.get("features", 0))

    if feature_count < MINIMUM_EXPECTED_ROWS:
        raise RuntimeError(
            "Source feature count is unexpectedly low: "
            f"{feature_count}"
        )

    available_fields = set(info.get("fields", []))
    required_fields = set(
        RAW_PAYLOAD_FIELDS + VALIDATION_FIELDS
    )
    missing_fields = required_fields.difference(
        available_fields
    )

    if missing_fields:
        raise RuntimeError(
            "Yellowstone parcel source is missing fields: "
            f"{sorted(missing_fields)}"
        )

    return {
        "feature_count": feature_count,
        "crs": info["crs"],
    }


def read_validation_attributes(
    geodatabase: Path,
) -> pd.DataFrame:
    """Read only lightweight fields needed for preflight checks."""
    return pyogrio.read_dataframe(
        geodatabase,
        layer=LAYER_NAME,
        columns=VALIDATION_FIELDS,
        read_geometry=False,
    )


def read_source_chunk(
    geodatabase: Path,
    start: int,
    size: int,
) -> gpd.GeoDataFrame:
    """Read one bounded spatial chunk without owner information."""
    return pyogrio.read_dataframe(
        geodatabase,
        layer=LAYER_NAME,
        columns=RAW_PAYLOAD_FIELDS,
        skip_features=start,
        max_features=size,
    )


def clean_text(value: Any) -> Optional[str]:
    """Return stripped text or None."""
    if value is None or pd.isna(value):
        return None

    cleaned = str(value).strip()
    return cleaned or None


def clean_integer(value: Any) -> Optional[int]:
    """Return a native integer or None."""
    if value is None or pd.isna(value):
        return None

    return int(value)


def clean_number(value: Any) -> Optional[float]:
    """Return a finite native float or None."""
    if value is None or pd.isna(value):
        return None

    number = float(value)

    if not math.isfinite(number):
        return None

    return number


def parse_city_state_zip(
    value: Any,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse values such as BILLINGS, MT 59102."""
    text = clean_text(value)

    if text is None:
        return None, None, None

    match = CITY_STATE_ZIP_PATTERN.match(text)

    if not match:
        return None, None, None

    return (
        match.group(1).strip(),
        match.group(2).strip(),
        match.group(3).strip(),
    )


def json_value(value: Any) -> Any:
    """Convert pandas and NumPy values for strict JSON."""
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
    latitude: Optional[float],
    longitude: Optional[float],
) -> Dict[str, Any]:
    """Build a privacy-safe raw source payload."""
    payload = {
        field: json_value(source_row.get(field))
        for field in RAW_PAYLOAD_FIELDS
    }

    payload["longitude"] = longitude
    payload["latitude"] = latitude

    # Ensure the dictionary contains valid strict JSON.
    json.dumps(payload, allow_nan=False)

    return payload


def prepare_chunk(
    source_chunk: gpd.GeoDataFrame,
    source_date: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Convert one source chunk into Supabase rows."""
    required_columns = set(
        RAW_PAYLOAD_FIELDS + ["geometry"]
    )
    missing_columns = required_columns.difference(
        source_chunk.columns
    )

    if missing_columns:
        raise RuntimeError(
            "Source chunk is missing columns: "
            f"{sorted(missing_columns)}"
        )

    data = source_chunk.copy()

    parcel_ids = (
        data["PARCELID"]
        .astype("string")
        .str.strip()
    )
    valid_ids = (
        parcel_ids.notna()
        & parcel_ids.ne("")
    )
    dropped_blank_ids = int((~valid_ids).sum())

    data = data.loc[valid_ids].copy()
    data["PARCELID"] = parcel_ids.loc[valid_ids]

    if data["PARCELID"].duplicated().any():
        duplicates = (
            data.loc[
                data["PARCELID"].duplicated(
                    keep=False
                ),
                "PARCELID",
            ]
            .astype(str)
            .unique()
            .tolist()
        )

        raise RuntimeError(
            "Duplicate parcel IDs in source chunk: "
            f"{duplicates[:10]}"
        )

    native_centroids = data.geometry.centroid

    geographic_centroids = gpd.GeoSeries(
        native_centroids,
        index=data.index,
        crs=data.crs,
    ).to_crs("EPSG:4326")

    rows: List[Dict[str, Any]] = []

    for index, source_row in data.iterrows():
        centroid = geographic_centroids.loc[index]

        if centroid is None or centroid.is_empty:
            latitude = None
            longitude = None
        else:
            latitude = clean_number(centroid.y)
            longitude = clean_number(centroid.x)

        property_type = clean_text(
            source_row.get("PropType")
        )
        property_id = clean_integer(
            source_row.get("PropertyID")
        )
        city, state, zip_code = parse_city_state_zip(
            source_row.get("CityStateZip")
        )
        gis_acres = clean_number(
            source_row.get("GISAcres")
        )

        lot_size_sqft = (
            gis_acres * 43_560.0
            if gis_acres is not None
            else None
        )

        row = {
            "parcel_id": clean_text(
                source_row.get("PARCELID")
            ),
            "property_id": (
                str(property_id)
                if property_id is not None
                else None
            ),
            "assessment_code": clean_text(
                source_row.get("AssessmentCode")
            ),
            "county_code": clean_integer(
                source_row.get("COUNTYCD")
            ),
            "county_name": clean_text(
                source_row.get("CountyName")
            ),
            "county_abbr": clean_text(
                source_row.get("CountyAbbr")
            ),
            "tax_year": clean_integer(
                source_row.get("TaxYear")
            ),
            "address_line_1": clean_text(
                source_row.get("AddressLine1")
            ),
            "address_line_2": clean_text(
                source_row.get("AddressLine2")
            ),
            "city_state_zip": clean_text(
                source_row.get("CityStateZip")
            ),
            "site_city": city,
            "site_state": state,
            "site_zip_code": zip_code,
            "property_type": property_type,
            "property_type_group": (
                PROPERTY_TYPE_GROUPS.get(
                    property_type
                )
            ),
            "is_residential": (
                RESIDENTIAL_FLAGS.get(
                    property_type
                )
            ),
            "property_access": clean_text(
                source_row.get("PropAccess")
            ),
            "levy_district": clean_text(
                source_row.get("LevyDistrict")
            ),
            "gis_acres": gis_acres,
            "total_acres": clean_number(
                source_row.get("TotalAcres")
            ),
            "lot_size_sqft": lot_size_sqft,
            "total_building_value": clean_number(
                source_row.get(
                    "TotalBuildingValue"
                )
            ),
            "total_land_value": clean_number(
                source_row.get("TotalLandValue")
            ),
            "total_value": clean_number(
                source_row.get("TotalValue")
            ),
            "township": clean_text(
                source_row.get("Township")
            ),
            "range": clean_text(
                source_row.get("Range")
            ),
            "section": clean_text(
                source_row.get("Section")
            ),
            "subdivision": clean_text(
                source_row.get("Subdivision")
            ),
            "certificate_of_survey": clean_text(
                source_row.get(
                    "CertificateOfSurvey"
                )
            ),
            "legal_description_short": clean_text(
                source_row.get(
                    "LegalDescriptionShort"
                )
            ),
            "latitude": latitude,
            "longitude": longitude,
            "source_name": SOURCE_NAME,
            "source_file": SOURCE_FILE,
            "source_date": source_date,
            "raw_payload": build_raw_payload(
                source_row,
                latitude,
                longitude,
            ),
        }

        rows.append(row)

    rows.sort(
        key=lambda row: row["parcel_id"]
    )

    return rows, dropped_blank_ids


def fetch_existing_count(client) -> int:
    """Return the current destination row count."""
    result = (
        client
        .table(TARGET_TABLE)
        .select(
            "parcel_id",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    return int(result.count or 0)


def validate_source(
    attributes: pd.DataFrame,
    feature_count: int,
    existing_count: int,
) -> Dict[str, Any]:
    """Validate the full source before database writes."""
    if len(attributes) != feature_count:
        raise RuntimeError(
            "Attribute count does not match source "
            f"feature count: {len(attributes)} versus "
            f"{feature_count}"
        )

    parcel_ids = (
        attributes["PARCELID"]
        .astype("string")
        .str.strip()
    )

    valid_ids = (
        parcel_ids.notna()
        & parcel_ids.ne("")
    )
    blank_id_count = int((~valid_ids).sum())
    valid_parcel_ids = parcel_ids.loc[valid_ids]
    prepared_count = len(valid_parcel_ids)

    if blank_id_count > MAXIMUM_BLANK_IDS:
        raise RuntimeError(
            "Too many source records have blank parcel "
            f"IDs: {blank_id_count}"
        )

    if prepared_count < MINIMUM_EXPECTED_ROWS:
        raise RuntimeError(
            "Prepared parcel count is unexpectedly low: "
            f"{prepared_count}"
        )

    duplicate_ids = valid_parcel_ids[
        valid_parcel_ids.duplicated(
            keep=False
        )
    ]

    if not duplicate_ids.empty:
        raise RuntimeError(
            "Duplicate nonblank parcel IDs found: "
            f"{duplicate_ids.unique()[:10].tolist()}"
        )

    county_names = {
        clean_text(value)
        for value in attributes.loc[
            valid_ids,
            "CountyName",
        ]
    }
    county_names.discard(None)

    if county_names != {"Yellowstone"}:
        raise RuntimeError(
            "Unexpected county names found: "
            f"{sorted(county_names)}"
        )

    tax_years = {
        clean_integer(value)
        for value in attributes.loc[
            valid_ids,
            "TaxYear",
        ]
        if not pd.isna(value)
    }

    if not tax_years:
        raise RuntimeError(
            "No parcel tax years were found"
        )

    if existing_count:
        decrease_pct = (
            (existing_count - prepared_count)
            / existing_count
            * 100.0
        )

        if decrease_pct > MAXIMUM_ROW_DECREASE_PCT:
            raise RuntimeError(
                "Prepared parcel count decreased by "
                f"{decrease_pct:.2f}%, exceeding the "
                f"{MAXIMUM_ROW_DECREASE_PCT:.2f}% "
                "safety limit"
            )

    return {
        "blank_id_count": blank_id_count,
        "prepared_count": prepared_count,
        "latest_tax_year": max(tax_years),
    }


def validate_prepared_chunk(
    rows: List[Dict[str, Any]],
) -> None:
    """Validate one prepared chunk before uploading it."""
    parcel_ids = [
        row["parcel_id"]
        for row in rows
    ]

    if any(not parcel_id for parcel_id in parcel_ids):
        raise RuntimeError(
            "Prepared chunk contains blank parcel IDs"
        )

    if len(parcel_ids) != len(set(parcel_ids)):
        raise RuntimeError(
            "Prepared chunk contains duplicate parcel IDs"
        )

    invalid_counties = {
        row["county_name"]
        for row in rows
        if row["county_name"] != "Yellowstone"
    }

    if invalid_counties:
        raise RuntimeError(
            "Unexpected county names in prepared chunk: "
            f"{sorted(invalid_counties)}"
        )

    invalid_coordinates = [
        row["parcel_id"]
        for row in rows
        if row["latitude"] is not None
        and row["longitude"] is not None
        and not (
            44.0 <= row["latitude"] <= 47.0
            and -111.0 <= row["longitude"] <= -106.0
        )
    ]

    if invalid_coordinates:
        raise RuntimeError(
            "Parcel coordinates fall outside the "
            "expected Montana area: "
            f"{invalid_coordinates[:10]}"
        )


def upsert_rows(
    client,
    rows: List[Dict[str, Any]],
) -> int:
    """Upsert one prepared source chunk in small batches."""
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
                on_conflict="parcel_id",
            )
            .execute()
        )

        rows_written += len(batch)

    return rows_written


def process_source_chunks(
    client,
    geodatabase: Path,
    feature_count: int,
    source_date: str,
) -> Tuple[int, int]:
    """Read, validate, and upload bounded spatial chunks."""
    total_written = 0
    total_blank_ids = 0

    for start in range(
        0,
        feature_count,
        SOURCE_CHUNK_SIZE,
    ):
        chunk_number = (
            start // SOURCE_CHUNK_SIZE
        ) + 1

        source_chunk = read_source_chunk(
            geodatabase,
            start,
            SOURCE_CHUNK_SIZE,
        )

        rows, blank_ids = prepare_chunk(
            source_chunk,
            source_date,
        )

        validate_prepared_chunk(rows)

        written = upsert_rows(
            client,
            rows,
        )

        total_written += written
        total_blank_ids += blank_ids

        print(
            f"  Chunk {chunk_number}: "
            f"{total_written:,} of "
            f"{feature_count:,} source features processed"
        )

        del rows
        del source_chunk
        gc.collect()

    return total_written, total_blank_ids


def start_pipeline_run(client):
    """Create a pipeline audit record."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name": (
                "Montana State Library Cadastral"
            ),
            "job_name": (
                "update_yellowstone_parcels"
            ),
            "status": "running",
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the parcel pipeline "
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


def main():
    client = get_supabase()
    run_id = start_pipeline_run(client)

    try:
        with tempfile.TemporaryDirectory(
            prefix="yellowstone_parcels_"
        ) as temporary_directory:
            temporary_path = Path(
                temporary_directory
            )
            archive_path = (
                temporary_path / SOURCE_FILE
            )
            extract_directory = (
                temporary_path / "extracted"
            )

            print(
                "Downloading Yellowstone parcel data..."
            )
            download_source(archive_path)

            source_date = extract_source_date(
                archive_path
            )

            print(
                "Extracting parcel geodatabase..."
            )
            with zipfile.ZipFile(
                archive_path
            ) as archive:
                archive.extractall(
                    extract_directory
                )

            geodatabase = locate_geodatabase(
                extract_directory
            )

            print("Reading source metadata...")
            source_info = get_source_info(
                geodatabase
            )
            feature_count = source_info[
                "feature_count"
            ]

            print(
                "Validating parcel IDs and source fields..."
            )
            attributes = read_validation_attributes(
                geodatabase
            )
            existing_count = fetch_existing_count(
                client
            )

            validation = validate_source(
                attributes,
                feature_count,
                existing_count,
            )

            del attributes
            gc.collect()

            print(
                "Processing and updating parcel chunks..."
            )
            rows_written, blank_ids_seen = (
                process_source_chunks(
                    client,
                    geodatabase,
                    feature_count,
                    source_date,
                )
            )

        if (
            blank_ids_seen
            != validation["blank_id_count"]
        ):
            raise RuntimeError(
                "Chunk processing found a different "
                "blank-ID count than preflight validation"
            )

        if (
            rows_written
            != validation["prepared_count"]
        ):
            raise RuntimeError(
                "Rows written do not match the validated "
                "prepared row count"
            )

        validation_summary = {
            "source_row_count": feature_count,
            "prepared_row_count": (
                validation["prepared_count"]
            ),
            "previous_database_row_count": (
                existing_count
            ),
            "blank_parcel_ids_removed": (
                validation["blank_id_count"]
            ),
            "latest_tax_year": (
                validation["latest_tax_year"]
            ),
            "source_date": source_date,
            "source_chunk_size": (
                SOURCE_CHUNK_SIZE
            ),
            "stale_rows_deleted": 0,
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=source_date,
            rows_read=feature_count,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print(
            "Yellowstone parcel update succeeded."
        )
        print("Source rows:", feature_count)
        print("Rows processed:", rows_written)
        print(
            "Blank parcel IDs removed:",
            validation["blank_id_count"],
        )
        print(
            "Tax year:",
            validation["latest_tax_year"],
        )
        print("Source date:", source_date)
        print(
            "Note: parcels absent from the new source "
            "were not automatically deleted."
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