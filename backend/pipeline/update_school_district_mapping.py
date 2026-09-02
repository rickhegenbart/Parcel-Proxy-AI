import gc
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import geopandas as gpd
import pandas as pd
import pyogrio
from dotenv import load_dotenv

from app.supabase_client import get_supabase


load_dotenv()

PARCEL_TABLE = "parcel_property_records"
TARGET_TABLE = "parcel_school_district_map"
PIPELINE_RUNS_TABLE = "data_pipeline_runs"

SOURCE_NAME = "U.S. Census Bureau TIGER/Line School Districts"
TIGER_ROOT = "https://www2.census.gov/geo/tiger"

STATE_FIPS = "30"
PARCEL_PAGE_SIZE = 1000
UPSERT_BATCH_SIZE = 1000

DISTRICT_SOURCES = {
    "elementary": {
        "folder": "ELSD",
        "suffix": "elsd",
        "lea_field": "ELSDLEA",
    },
    "secondary": {
        "folder": "SCSD",
        "suffix": "scsd",
        "lea_field": "SCSDLEA",
    },
    "unified": {
        "folder": "UNSD",
        "suffix": "unsd",
        "lea_field": "UNSDLEA",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunked(
    rows: List[Dict[str, Any]],
    size: int,
) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def build_source_url(
    tiger_year: int,
    district_type: str,
) -> str:
    config = DISTRICT_SOURCES[district_type]

    return (
        f"{TIGER_ROOT}/TIGER{tiger_year}/"
        f"{config['folder']}/"
        f"tl_{tiger_year}_{STATE_FIPS}_"
        f"{config['suffix']}.zip"
    )


def url_exists(url: str) -> bool:
    request = Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "(school district mapping updater)"
            )
        },
    )

    try:
        with urlopen(request, timeout=45) as response:
            return 200 <= response.status < 400
    except (HTTPError, URLError):
        return False


def discover_latest_tiger_year() -> int:
    current_year = datetime.now(timezone.utc).year

    for tiger_year in range(
        current_year,
        current_year - 6,
        -1,
    ):
        urls = [
            build_source_url(
                tiger_year,
                district_type,
            )
            for district_type in DISTRICT_SOURCES
        ]

        if all(url_exists(url) for url in urls):
            return tiger_year

    raise RuntimeError(
        "No complete recent TIGER school-district release "
        "was found."
    )


def download_file(
    url: str,
    destination: Path,
) -> None:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Parcel-Proxy-AI/1.0 "
                "(school district mapping updater)"
            )
        },
    )

    try:
        with urlopen(request, timeout=180) as response:
            with destination.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)

                    if not block:
                        break

                    output.write(block)
    except (HTTPError, URLError) as error:
        raise RuntimeError(
            f"Unable to download {url}: {error}"
        ) from error

    if not destination.exists():
        raise RuntimeError(
            f"Download did not create {destination}."
        )

    if destination.stat().st_size < 1000:
        raise RuntimeError(
            f"Downloaded file is unexpectedly small: "
            f"{destination}"
        )


def download_boundaries(
    tiger_year: int,
    download_directory: Path,
) -> Dict[str, Path]:
    archives: Dict[str, Path] = {}

    for district_type, config in (
        DISTRICT_SOURCES.items()
    ):
        url = build_source_url(
            tiger_year,
            district_type,
        )
        destination = (
            download_directory
            / f"{config['suffix']}.zip"
        )

        print(
            f"Downloading {district_type} boundaries..."
        )
        download_file(url, destination)
        archives[district_type] = destination

    return archives


def normalize_text(
    value: Any,
) -> Optional[str]:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()

    return text or None


def normalize_lea_id(
    value: Any,
) -> Optional[str]:
    text = normalize_text(value)

    if not text:
        return None

    digits = "".join(
        character
        for character in text
        if character.isdigit()
    )

    if not digits:
        return None

    return digits.zfill(7)


def read_boundary_layer(
    archive_path: Path,
    district_type: str,
) -> gpd.GeoDataFrame:
    config = DISTRICT_SOURCES[district_type]
    vsi_path = f"/vsizip/{archive_path}"

    required_columns = [
        "GEOID",
        "NAME",
        "LOGRADE",
        "HIGRADE",
        config["lea_field"],
    ]

    try:
        boundaries = pyogrio.read_dataframe(
            vsi_path,
            columns=required_columns,
        )
    except Exception as error:
        raise RuntimeError(
            f"Unable to read {district_type} boundaries: "
            f"{error}"
        ) from error

    if boundaries.empty:
        raise RuntimeError(
            f"The {district_type} boundary layer is empty."
        )

    if boundaries.crs is None:
        raise RuntimeError(
            f"The {district_type} boundary layer "
            "has no CRS."
        )

    boundaries = boundaries.to_crs("EPSG:4326")

    boundaries["district_type"] = district_type
    boundaries["district_geoid"] = (
        boundaries["GEOID"]
        .astype("string")
        .str.strip()
    )
        # TIGER GEOID combines the two-digit state FIPS and
    # five-digit district code and therefore matches NCES LEAID.
    boundaries["lea_id"] = boundaries[
        "district_geoid"
    ].apply(normalize_lea_id)
        
    
    boundaries["district_name"] = (
        boundaries["NAME"]
        .astype("string")
        .str.strip()
    )
    boundaries["low_grade"] = (
        boundaries["LOGRADE"]
        .astype("string")
        .str.strip()
    )
    boundaries["high_grade"] = (
        boundaries["HIGRADE"]
        .astype("string")
        .str.strip()
    )

    boundaries = boundaries[
        [
            "district_type",
            "district_geoid",
            "lea_id",
            "district_name",
            "low_grade",
            "high_grade",
            "geometry",
        ]
    ].copy()

    missing_ids = (
        boundaries["lea_id"].isna()
        | boundaries["district_geoid"].isna()
    )

    if missing_ids.any():
        raise RuntimeError(
            f"The {district_type} boundary layer "
            f"contains {int(missing_ids.sum())} missing IDs."
        )

    invalid_geoid = (
        boundaries["district_geoid"]
        != boundaries["lea_id"]
    )

    if invalid_geoid.any():
        examples = boundaries.loc[
            invalid_geoid,
            [
                "district_geoid",
                "lea_id",
                "district_name",
            ],
        ].head(10)

        raise RuntimeError(
            "TIGER GEOID and NCES LEA ID do not match "
            f"for {district_type} districts: "
            f"{examples.to_dict('records')}"
        )

    if boundaries.geometry.isna().any():
        raise RuntimeError(
            f"The {district_type} layer contains "
            "missing geometries."
        )

    return boundaries


def read_all_boundaries(
    archives: Dict[str, Path],
) -> Tuple[
    Dict[str, gpd.GeoDataFrame],
    int,
]:
    layers: Dict[str, gpd.GeoDataFrame] = {}
    feature_count = 0

    for district_type, archive_path in (
        archives.items()
    ):
        layer = read_boundary_layer(
            archive_path,
            district_type,
        )

        layers[district_type] = layer
        feature_count += len(layer)

        print(
            f"  {district_type.title()} districts:",
            len(layer),
        )

    return layers, feature_count


def fetch_parcels(client) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    offset = 0

    while True:
        result = (
            client
            .table(PARCEL_TABLE)
            .select(
                "parcel_id,latitude,longitude"
            )
            .order("parcel_id")
            .range(
                offset,
                offset + PARCEL_PAGE_SIZE - 1,
            )
            .execute()
        )

        page = result.data or []
        rows.extend(page)

        if len(rows) % 10000 == 0 and rows:
            print(
                f"  Loaded {len(rows):,} parcel records"
            )

        if len(page) < PARCEL_PAGE_SIZE:
            break

        offset += PARCEL_PAGE_SIZE

    if not rows:
        raise RuntimeError(
            "No parcel records were loaded."
        )

    parcels = pd.DataFrame(rows)

    parcels["parcel_id"] = (
        parcels["parcel_id"]
        .astype("string")
        .str.strip()
    )
    parcels["latitude"] = pd.to_numeric(
        parcels["latitude"],
        errors="coerce",
    )
    parcels["longitude"] = pd.to_numeric(
        parcels["longitude"],
        errors="coerce",
    )

    missing = (
        parcels["parcel_id"].isna()
        | parcels["latitude"].isna()
        | parcels["longitude"].isna()
    )

    if missing.any():
        raise RuntimeError(
            "Parcel records contain "
            f"{int(missing.sum())} missing IDs or coordinates."
        )

    duplicate_ids = parcels[
        "parcel_id"
    ].duplicated(keep=False)

    if duplicate_ids.any():
        raise RuntimeError(
            "Parcel source contains duplicated parcel IDs."
        )

    return parcels


def create_mapping_for_layer(
    parcel_points: gpd.GeoDataFrame,
    boundaries: gpd.GeoDataFrame,
    district_type: str,
    tiger_year: int,
) -> List[Dict[str, Any]]:
    joined = gpd.sjoin(
        parcel_points,
        boundaries,
        how="left",
        predicate="within",
    )

    unmatched = joined["lea_id"].isna()

    if unmatched.any():
        # A small number of points can lie exactly on a
        # polygon edge. Retry those using intersects.
        retry_points = parcel_points.loc[
            joined.loc[
                unmatched,
                "parcel_index",
            ].dropna().astype(int).unique()
        ]

        if not retry_points.empty:
            retry = gpd.sjoin(
                retry_points,
                boundaries,
                how="left",
                predicate="intersects",
            )

            retry_lookup = (
                retry.dropna(subset=["lea_id"])
                .drop_duplicates(
                    subset=["parcel_index"],
                    keep="first",
                )
                .set_index("parcel_index")
            )

            for row_index in joined.index[unmatched]:
                parcel_index = joined.at[
                    row_index,
                    "parcel_index",
                ]

                if parcel_index in retry_lookup.index:
                    replacement = retry_lookup.loc[
                        parcel_index
                    ]

                    for column in [
                        "district_geoid",
                        "lea_id",
                        "district_name",
                        "low_grade",
                        "high_grade",
                    ]:
                        joined.at[
                            row_index,
                            column,
                        ] = replacement[column]

    joined = joined.dropna(subset=["lea_id"]).copy()

    duplicate_keys = joined.duplicated(
        subset=[
            "parcel_id",
            "district_type",
        ],
        keep=False,
    )

    if duplicate_keys.any():
        duplicates = joined.loc[
            duplicate_keys,
            [
                "parcel_id",
                "district_type",
                "lea_id",
            ],
        ].head(20)

        raise RuntimeError(
            "Multiple school districts of the same type "
            "matched a parcel: "
            f"{duplicates.to_dict('records')}"
        )

    updated_at = utc_now()

    rows = []

    for record in joined.to_dict("records"):
        rows.append({
            "parcel_id": record["parcel_id"],
            "district_type": district_type,
            "district_geoid":
                record["district_geoid"],
            "lea_id": record["lea_id"],
            "district_name":
                record["district_name"],
            "low_grade": record["low_grade"],
            "high_grade": record["high_grade"],
            "latitude": float(
                record["latitude"]
            ),
            "longitude": float(
                record["longitude"]
            ),
            "tiger_year": tiger_year,
            "updated_at": updated_at,
        })

    return rows


def create_mapping(
    parcels: pd.DataFrame,
    layers: Dict[str, gpd.GeoDataFrame],
    tiger_year: int,
) -> List[Dict[str, Any]]:
    parcel_points = gpd.GeoDataFrame(
        parcels.reset_index(
            names="parcel_index"
        ),
        geometry=gpd.points_from_xy(
            parcels["longitude"],
            parcels["latitude"],
        ),
        crs="EPSG:4326",
    )

    rows: List[Dict[str, Any]] = []

    for district_type, boundaries in (
        layers.items()
    ):
        print(
            f"Assigning {district_type} districts..."
        )

        layer_rows = create_mapping_for_layer(
            parcel_points,
            boundaries,
            district_type,
            tiger_year,
        )

        rows.extend(layer_rows)

        print(
            f"  {len(layer_rows):,} "
            f"{district_type} mappings"
        )

    return rows


def validate_mapping(
    parcels: pd.DataFrame,
    rows: List[Dict[str, Any]],
    tiger_year: int,
) -> None:
    if not rows:
        raise RuntimeError(
            "No school-district mappings were prepared."
        )

    parcel_ids = set(
        parcels["parcel_id"].astype(str)
    )
    mapped_parcel_ids = {
        row["parcel_id"]
        for row in rows
    }

    missing_parcels = (
        parcel_ids - mapped_parcel_ids
    )

    if missing_parcels:
        raise RuntimeError(
            f"{len(missing_parcels):,} parcels did not "
            "match any school district. Examples: "
            f"{sorted(missing_parcels)[:20]}"
        )

    keys = [
        (
            row["parcel_id"],
            row["district_type"],
        )
        for row in rows
    ]

    if len(keys) != len(set(keys)):
        raise RuntimeError(
            "Duplicate parcel/district-type mappings "
            "were prepared."
        )

    type_by_parcel: Dict[str, set] = {}

    for row in rows:
        type_by_parcel.setdefault(
            row["parcel_id"],
            set(),
        ).add(row["district_type"])

        if row["tiger_year"] != tiger_year:
            raise RuntimeError(
                "Prepared rows contain inconsistent "
                "TIGER years."
            )

        if row["district_geoid"] != row["lea_id"]:
            raise RuntimeError(
                "A district GEOID does not match its "
                "NCES LEA ID."
            )

    invalid_combinations = {}

    for parcel_id, district_types in (
        type_by_parcel.items()
    ):
        valid = (
            district_types == {"unified"}
            or district_types
            == {"elementary", "secondary"}
        )

        if not valid:
            invalid_combinations[
                parcel_id
            ] = sorted(district_types)

            if len(invalid_combinations) >= 20:
                break

    if invalid_combinations:
        raise RuntimeError(
            "Some parcels have incomplete or conflicting "
            "district-type combinations: "
            f"{invalid_combinations}"
        )


def start_pipeline_run(client) -> Optional[str]:
    try:
        result = (
            client
            .table(PIPELINE_RUNS_TABLE)
            .insert({
                "source_name": SOURCE_NAME,
                "job_name":
                    "update_school_district_mapping",
                "status": "running",
                "started_at": utc_now(),
                "rows_read": 0,
                "rows_written": 0,
            })
            .execute()
        )

        data = result.data or []

        if data:
            return data[0].get("id")

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
        "validation_summary": validation_summary or {},
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

    for batch in chunked(
        rows,
        UPSERT_BATCH_SIZE,
    ):
        (
            client
            .table(TARGET_TABLE)
            .upsert(
                batch,
                on_conflict=(
                    "parcel_id,district_type"
                ),
            )
            .execute()
        )

        written += len(batch)

        if (
            written % 10000 == 0
            or written == len(rows)
        ):
            print(
                f"  Uploaded {written:,} of "
                f"{len(rows):,} mappings"
            )

    return written


def main() -> None:
    client = get_supabase()
    run_id = start_pipeline_run(client)

    tiger_year: Optional[int] = None
    rows_read = 0
    rows_written = 0

    try:
        print(
            "Discovering latest Census TIGER "
            "school-district release..."
        )
        tiger_year = discover_latest_tiger_year()

        print("TIGER year:", tiger_year)

        with tempfile.TemporaryDirectory() as temp:
            temp_directory = Path(temp)

            archives = download_boundaries(
                tiger_year,
                temp_directory,
            )

            print("Reading school-district boundaries...")
            layers, boundary_count = (
                read_all_boundaries(archives)
            )

            print("Loading current parcel coordinates...")
            parcels = fetch_parcels(client)

            rows_read = (
                boundary_count + len(parcels)
            )

            print(
                "Assigning parcels to school districts..."
            )
            rows = create_mapping(
                parcels,
                layers,
                tiger_year,
            )

            print(
                "Validating school-district mappings..."
            )
            validate_mapping(
                parcels,
                rows,
                tiger_year,
            )

            type_counts = (
                pd.Series(
                    row["district_type"]
                    for row in rows
                )
                .value_counts()
                .sort_index()
                .to_dict()
            )

            distinct_districts = len({
                row["lea_id"]
                for row in rows
            })

            del layers
            gc.collect()

            print(
                "Updating school-district mappings..."
            )
            rows_written = upsert_rows(
                client,
                rows,
            )

        validation_summary = {
            "tiger_year": tiger_year,
            "parcel_count": len(parcels),
            "mapping_count": rows_written,
            "distinct_district_count":
                distinct_districts,
            "district_type_counts":
                type_counts,
        }

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            source_period=str(tiger_year),
            rows_read=rows_read,
            rows_written=rows_written,
            validation_summary=validation_summary,
        )

        print()
        print(
            "School-district mapping update succeeded."
        )
        print("TIGER year:", tiger_year)
        print("Parcels mapped:", len(parcels))
        print("Mappings written:", rows_written)
        print(
            "Distinct districts:",
            distinct_districts,
        )
        print(
            "District-type counts:",
            type_counts,
        )
        print(
            "Note: mappings absent from the new source "
            "were not automatically deleted."
        )

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            source_period=(
                str(tiger_year)
                if tiger_year is not None
                else None
            ),
            rows_read=rows_read,
            rows_written=rows_written,
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()