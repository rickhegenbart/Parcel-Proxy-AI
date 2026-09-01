"""Refresh parcel-to-Census-tract mappings using Census TIGER data."""

import argparse
import math
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import geopandas as gpd
import pandas as pd
import pyogrio

from app.supabase_client import get_supabase


CENSUS_SOURCE_NAME = "U.S. Census Bureau TIGER/Line"
CENSUS_SOURCE_ROOT = (
    "https://www2.census.gov/geo/tiger"
)
MONTANA_STATE_FIPS = "30"

PARCEL_TABLE = "parcel_property_records"
MAPPING_TABLE = "parcel_census_tract_map"

PAGE_SIZE = 1_000
UPSERT_BATCH_SIZE = 500
MINIMUM_EXPECTED_PARCELS = 75_000
MAXIMUM_YEAR_LOOKBACK = 4


def tiger_url(year: int) -> str:
    """Return the Montana tract ZIP URL for one TIGER year."""
    return (
        f"{CENSUS_SOURCE_ROOT}/TIGER{year}/TRACT/"
        f"tl_{year}_30_tract.zip"
    )


def url_exists(url: str) -> bool:
    """Check whether a Census source URL exists."""
    request = Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "Census-Tract-Mapping-Updater"
            )
        },
    )

    try:
        with urlopen(
            request,
            timeout=30,
        ) as response:
            return 200 <= response.status < 400

    except HTTPError as error:
        if error.code == 404:
            return False

        raise RuntimeError(
            f"Census source check failed: {error}"
        ) from error

    except URLError as error:
        raise RuntimeError(
            f"Census source check failed: {error}"
        ) from error


def discover_latest_tiger_year(
    requested_year: Optional[int] = None,
) -> Tuple[int, str]:
    """Find the newest available annual Montana tract ZIP."""
    current_year = datetime.now(
        timezone.utc
    ).year

    if requested_year is not None:
        url = tiger_url(requested_year)

        if not url_exists(url):
            raise RuntimeError(
                "Requested Census TIGER source "
                f"does not exist: {url}"
            )

        return requested_year, url

    for year in range(
        current_year,
        current_year - MAXIMUM_YEAR_LOOKBACK,
        -1,
    ):
        url = tiger_url(year)

        if url_exists(url):
            return year, url

    raise RuntimeError(
        "Unable to locate a recent Montana Census "
        "TIGER tract source"
    )


def download_source(
    url: str,
    destination: Path,
) -> None:
    """Download the selected Census TIGER ZIP."""
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
            "--connect-timeout",
            "30",
            "--max-time",
            "300",
            "--output",
            str(destination),
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Census TIGER download failed: "
            f"{result.stderr.strip()}"
        )

    if not destination.exists():
        raise RuntimeError(
            "Census TIGER archive was not downloaded"
        )

    if destination.stat().st_size < 100_000:
        raise RuntimeError(
            "Downloaded Census TIGER archive is "
            "unexpectedly small"
        )


def extract_shapefile(
    archive_path: Path,
    extract_directory: Path,
) -> Path:
    """Extract and locate the Montana tract shapefile."""
    if not zipfile.is_zipfile(archive_path):
        raise RuntimeError(
            "Census TIGER download is not a valid ZIP"
        )

    with zipfile.ZipFile(archive_path) as archive:
        archive.extractall(extract_directory)

    shapefiles = list(
        extract_directory.glob("*.shp")
    )

    if len(shapefiles) != 1:
        raise RuntimeError(
            "Expected exactly one TIGER shapefile; "
            f"found {len(shapefiles)}"
        )

    return shapefiles[0]


def read_tract_boundaries(
    shapefile_path: Path,
) -> gpd.GeoDataFrame:
    """Read and validate Montana tract polygons."""
    tracts = pyogrio.read_dataframe(
        shapefile_path,
        columns=[
            "STATEFP",
            "COUNTYFP",
            "TRACTCE",
            "GEOID",
            "NAME",
            "NAMELSAD",
        ],
    )

    required_columns = {
        "STATEFP",
        "COUNTYFP",
        "TRACTCE",
        "GEOID",
        "NAME",
        "NAMELSAD",
        "geometry",
    }
    missing_columns = required_columns.difference(
        tracts.columns
    )

    if missing_columns:
        raise RuntimeError(
            "TIGER tract source is missing columns: "
            f"{sorted(missing_columns)}"
        )

    if tracts.crs is None:
        raise RuntimeError(
            "TIGER tract source has no CRS"
        )

    if len(tracts) < 300:
        raise RuntimeError(
            "Montana TIGER tract count is "
            f"unexpectedly low: {len(tracts)}"
        )

    state_fips = (
        tracts["STATEFP"]
        .astype("string")
        .str.strip()
        .str.zfill(2)
    )

    unexpected_states = set(
        state_fips.dropna().unique()
    ).difference({MONTANA_STATE_FIPS})

    if unexpected_states:
        raise RuntimeError(
            "Unexpected states in Montana tract file: "
            f"{sorted(unexpected_states)}"
        )

    tracts = tracts.copy()
    tracts["STATEFP"] = state_fips
    tracts["COUNTYFP"] = (
        tracts["COUNTYFP"]
        .astype("string")
        .str.strip()
        .str.zfill(3)
    )
    tracts["GEOID"] = (
        tracts["GEOID"]
        .astype("string")
        .str.strip()
        .str.zfill(11)
    )

    if tracts["GEOID"].duplicated().any():
        raise RuntimeError(
            "TIGER tract source contains "
            "duplicate GEOIDs"
        )

    return tracts


def clean_text(value: Any) -> Optional[str]:
    """Return stripped text or None."""
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    return text or None


def clean_coordinate(value: Any) -> Optional[float]:
    """Return a finite coordinate or None."""
    if value is None or pd.isna(value):
        return None

    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(coordinate):
        return None

    return coordinate


def fetch_parcels(client) -> pd.DataFrame:
    """Fetch all parcel coordinates using pagination."""
    rows: List[Dict[str, Any]] = []
    start = 0

    while True:
        result = (
            client
            .table(PARCEL_TABLE)
            .select(
                "parcel_id,latitude,longitude"
            )
            .order("parcel_id")
            .range(
                start,
                start + PAGE_SIZE - 1,
            )
            .execute()
        )

        batch = result.data or []
        rows.extend(batch)

        if len(batch) < PAGE_SIZE:
            break

        start += PAGE_SIZE

        if start % 10_000 == 0:
            print(
                f"  Loaded {start:,} parcel records"
            )

    if len(rows) < MINIMUM_EXPECTED_PARCELS:
        raise RuntimeError(
            "Parcel count is unexpectedly low: "
            f"{len(rows)}"
        )

    parcels = pd.DataFrame(rows)

    required_columns = {
        "parcel_id",
        "latitude",
        "longitude",
    }
    missing_columns = required_columns.difference(
        parcels.columns
    )

    if missing_columns:
        raise RuntimeError(
            "Parcel query is missing columns: "
            f"{sorted(missing_columns)}"
        )

    parcels["parcel_id"] = (
        parcels["parcel_id"]
        .astype("string")
        .str.strip()
    )
    parcels["latitude"] = parcels[
        "latitude"
    ].map(clean_coordinate)
    parcels["longitude"] = parcels[
        "longitude"
    ].map(clean_coordinate)

    if parcels["parcel_id"].isna().any():
        raise RuntimeError(
            "Parcel table contains blank parcel IDs"
        )

    if parcels["parcel_id"].eq("").any():
        raise RuntimeError(
            "Parcel table contains blank parcel IDs"
        )

    if parcels["parcel_id"].duplicated().any():
        raise RuntimeError(
            "Parcel table contains duplicate parcel IDs"
        )

    missing_coordinates = parcels[
        parcels["latitude"].isna()
        | parcels["longitude"].isna()
    ]

    if not missing_coordinates.empty:
        raise RuntimeError(
            "Parcels are missing coordinates: "
            f"{missing_coordinates['parcel_id'].head(10).tolist()}"
        )

    invalid_coordinates = parcels[
        ~parcels["latitude"].between(
            44.0,
            47.0,
        )
        | ~parcels["longitude"].between(
            -111.0,
            -106.0,
        )
    ]

    if not invalid_coordinates.empty:
        raise RuntimeError(
            "Parcel coordinates fall outside the "
            "expected Montana region: "
            f"{invalid_coordinates['parcel_id'].head(10).tolist()}"
        )

    return parcels


def create_mapping(
    parcels: pd.DataFrame,
    tracts: gpd.GeoDataFrame,
) -> List[Dict[str, Any]]:
    """Spatially join every parcel point to a Census tract."""
    points = gpd.GeoDataFrame(
        parcels.copy(),
        geometry=gpd.points_from_xy(
            parcels["longitude"],
            parcels["latitude"],
        ),
        crs="EPSG:4326",
    )

    if points.crs != tracts.crs:
        points = points.to_crs(tracts.crs)

    joined = gpd.sjoin(
        points,
        tracts[
            [
                "COUNTYFP",
                "GEOID",
                "NAMELSAD",
                "geometry",
            ]
        ],
        how="left",
        predicate="within",
    )

    duplicate_parcels = joined.loc[
        joined["parcel_id"].duplicated(
            keep=False
        ),
        "parcel_id",
    ]

    if not duplicate_parcels.empty:
        raise RuntimeError(
            "Parcel points matched multiple tracts: "
            f"{duplicate_parcels.unique()[:10].tolist()}"
        )

    unmatched = joined.loc[
        joined["GEOID"].isna()
    ]

    if not unmatched.empty:
        raise RuntimeError(
            "Parcels did not match a Census tract: "
            f"{unmatched['parcel_id'].head(20).tolist()} "
            f"(total {len(unmatched)})"
        )

    mapping_rows: List[Dict[str, Any]] = []

    for _, row in joined.iterrows():
        parcel_id = clean_text(
            row.get("parcel_id")
        )
        tract_fips = clean_text(
            row.get("GEOID")
        )
        county_fips = clean_text(
            row.get("COUNTYFP")
        )
        tract_name = clean_text(
            row.get("NAMELSAD")
        )
        latitude = clean_coordinate(
            row.get("latitude")
        )
        longitude = clean_coordinate(
            row.get("longitude")
        )

        if not parcel_id or not tract_fips:
            raise RuntimeError(
                "Prepared mapping row is missing "
                "its parcel or tract identifier"
            )

        mapping_rows.append({
            "parcel_id": parcel_id,
            "tract_fips": tract_fips,
            "county_fips": county_fips,
            "tract_name": tract_name,
            "latitude": latitude,
            "longitude": longitude,
        })

    mapping_rows.sort(
        key=lambda row: row["parcel_id"]
    )

    return mapping_rows


def fetch_existing_mapping_count(client) -> int:
    """Return the current mapping-table row count."""
    result = (
        client
        .table(MAPPING_TABLE)
        .select(
            "parcel_id",
            count="exact",
        )
        .limit(1)
        .execute()
    )

    return int(result.count or 0)


def validate_mapping(
    parcels: pd.DataFrame,
    tracts: gpd.GeoDataFrame,
    mapping_rows: List[Dict[str, Any]],
    existing_count: int,
) -> None:
    """Validate complete one-to-one mapping coverage."""
    parcel_count = len(parcels)
    mapping_count = len(mapping_rows)

    if mapping_count != parcel_count:
        raise RuntimeError(
            "Mapping count does not match parcel count: "
            f"{mapping_count} versus {parcel_count}"
        )

    parcel_ids = {
        str(value)
        for value in parcels["parcel_id"]
    }
    mapped_ids = {
        row["parcel_id"]
        for row in mapping_rows
    }

    if parcel_ids != mapped_ids:
        missing = sorted(
            parcel_ids.difference(mapped_ids)
        )
        extra = sorted(
            mapped_ids.difference(parcel_ids)
        )

        raise RuntimeError(
            "Mapping parcel IDs do not match the "
            f"parcel table. Missing: {missing[:10]}; "
            f"extra: {extra[:10]}"
        )

    if existing_count:
        difference_pct = (
            abs(mapping_count - existing_count)
            / existing_count
            * 100.0
        )

        if difference_pct > 10.0:
            raise RuntimeError(
                "Mapping count changed by "
                f"{difference_pct:.2f}%, exceeding "
                "the 10% safety limit"
            )

    valid_tracts = set(
        tracts["GEOID"].astype(str)
    )
    mapped_tracts = {
        row["tract_fips"]
        for row in mapping_rows
    }

    unknown_tracts = mapped_tracts.difference(
        valid_tracts
    )

    if unknown_tracts:
        raise RuntimeError(
            "Mappings contain unknown tract IDs: "
            f"{sorted(unknown_tracts)[:10]}"
        )


def upsert_batches(
    client,
    rows: List[Dict[str, Any]],
) -> int:
    """Upsert mapping rows in bounded batches."""
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
            .table(MAPPING_TABLE)
            .upsert(
                batch,
                on_conflict="parcel_id",
            )
            .execute()
        )

        rows_written += len(batch)

        if rows_written % 5_000 == 0:
            print(
                f"  Uploaded {rows_written:,} "
                f"of {len(rows):,} mappings"
            )

    return rows_written


def start_pipeline_run(
    client,
    tiger_year: int,
):
    """Create a pipeline audit record."""
    result = (
        client
        .table("data_pipeline_runs")
        .insert({
            "source_name": CENSUS_SOURCE_NAME,
            "job_name":
                "update_census_tract_mapping",
            "status": "running",
            "source_period": str(tiger_year),
            "started_at": datetime.now(
                timezone.utc
            ).isoformat(),
        })
        .execute()
    )

    if not result.data:
        raise RuntimeError(
            "Unable to create the Census mapping "
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


def parse_arguments():
    """Parse optional TIGER-year selection."""
    parser = argparse.ArgumentParser(
        description=(
            "Refresh parcel-to-Census-tract "
            "mappings"
        )
    )
    parser.add_argument(
        "--tiger-year",
        type=int,
        default=None,
        help=(
            "Specific TIGER year to use. "
            "Defaults to the newest available year."
        ),
    )

    return parser.parse_args()


def main():
    arguments = parse_arguments()

    print("Discovering latest Census TIGER source...")
    tiger_year, source_url = (
        discover_latest_tiger_year(
            arguments.tiger_year
        )
    )

    client = get_supabase()
    run_id = start_pipeline_run(
        client,
        tiger_year,
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="census_tract_mapping_"
        ) as temporary_directory:
            temporary_path = Path(
                temporary_directory
            )
            archive_path = (
                temporary_path
                / f"tl_{tiger_year}_30_tract.zip"
            )
            extract_directory = (
                temporary_path / "tracts"
            )

            print(
                f"Downloading Census TIGER "
                f"{tiger_year} tract boundaries..."
            )
            download_source(
                source_url,
                archive_path,
            )

            print("Extracting TIGER tract boundaries...")
            shapefile_path = extract_shapefile(
                archive_path,
                extract_directory,
            )

            print("Reading Montana tract boundaries...")
            tracts = read_tract_boundaries(
                shapefile_path
            )

            print("Loading current parcel coordinates...")
            parcels = fetch_parcels(client)

            print(
                "Assigning parcels to Census tracts..."
            )
            mapping_rows = create_mapping(
                parcels,
                tracts,
            )

            existing_count = (
                fetch_existing_mapping_count(client)
            )

            print("Validating tract mappings...")
            validate_mapping(
                parcels,
                tracts,
                mapping_rows,
                existing_count,
            )

            print("Updating Census tract mappings...")
            rows_written = upsert_batches(
                client,
                mapping_rows,
            )

        mapped_tract_count = len({
            row["tract_fips"]
            for row in mapping_rows
        })
        mapped_counties = sorted({
            row["county_fips"]
            for row in mapping_rows
            if row["county_fips"]
        })

        validation_summary = {
            "tiger_year": tiger_year,
            "source_url": source_url,
            "parcel_count": len(parcels),
            "mapping_count": len(mapping_rows),
            "previous_mapping_count":
                existing_count,
            "mapped_tract_count":
                mapped_tract_count,
            "mapped_counties": mapped_counties,
            "unmapped_count": 0,
            "stale_mappings_deleted": 0,
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            rows_read=(
                len(parcels) + len(tracts)
            ),
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print(
            "Census tract mapping update succeeded."
        )
        print("TIGER year:", tiger_year)
        print("Parcels mapped:", rows_written)
        print(
            "Distinct mapped tracts:",
            mapped_tract_count,
        )
        print(
            "Mapped county FIPS:",
            mapped_counties,
        )
        print(
            "Note: mappings absent from the current "
            "parcel table were not automatically deleted."
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