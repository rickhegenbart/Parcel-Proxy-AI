from __future__ import annotations

import gc
import math
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

from app.supabase_client import get_supabase


JOB_NAME = "update_nces_school_indicators"
SOURCE_NAME = "NCES Common Core of Data"
SOURCE_URL = "https://nces.ed.gov/ccd/files.asp"

MAPPING_TABLE = "parcel_school_district_map"
INDICATOR_TABLE = "school_district_indicators"
PIPELINE_RUN_TABLE = "data_pipeline_runs"

SCHOOL_YEAR = "2024-2025"
SOURCE_DATE = date(2025, 7, 30)

NCES_BASE_URL = "https://nces.ed.gov/ccd/Data/zip"

DIRECTORY_FILE = "ccd_lea_029_2425_w_1a_073025.zip"
MEMBERSHIP_FILE = "ccd_lea_052_2425_l_1a_073025.zip"
STAFF_FILE = "ccd_lea_059_2425_l_1a_073025.zip"

DIRECTORY_URL = f"{NCES_BASE_URL}/{DIRECTORY_FILE}"
MEMBERSHIP_URL = f"{NCES_BASE_URL}/{MEMBERSHIP_FILE}"
STAFF_URL = f"{NCES_BASE_URL}/{STAFF_FILE}"

MAPPING_PAGE_SIZE = 1000
READ_CHUNK_SIZE = 100000
UPLOAD_CHUNK_SIZE = 500

DIRECTORY_COLUMNS = [
    "SCHOOL_YEAR",
    "ST",
    "LEA_NAME",
    "LEAID",
    "SY_STATUS_TEXT",
]

MEMBERSHIP_COLUMNS = [
    "SCHOOL_YEAR",
    "ST",
    "LEA_NAME",
    "LEAID",
    "GRADE",
    "RACE_ETHNICITY",
    "SEX",
    "STUDENT_COUNT",
    "TOTAL_INDICATOR",
]

STAFF_COLUMNS = [
    "SCHOOL_YEAR",
    "ST",
    "LEA_NAME",
    "LEAID",
    "STAFF",
    "STAFF_COUNT",
    "TOTAL_INDICATOR",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_lea_id(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text or text.lower() in {
        "nan",
        "none",
        "<na>",
    }:
        return None

    if text.endswith(".0"):
        text = text[:-2]

    digits = "".join(
        character
        for character in text
        if character.isdigit()
    )

    if not digits:
        return None

    return digits.zfill(7)


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    text = str(value).strip()

    if not text or text.lower() in {
        "nan",
        "none",
        "<na>",
    }:
        return None

    return text


def clean_number(value: Any) -> Optional[float]:
    if value is None:
        return None

    number = pd.to_numeric(
        pd.Series([value]),
        errors="coerce",
    ).iloc[0]

    if pd.isna(number):
        return None

    number = float(number)

    # NCES uses negative values for missing,
    # not applicable, or suppressed values.
    if number < 0 or not math.isfinite(number):
        return None

    return number


def chunks(
    values: List[Dict[str, Any]],
    size: int,
) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def download_file(
    url: str,
    destination: Path,
) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "Parcel-Proxy-AI/1.0 "
                "(public-data pipeline)",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=180,
    ) as response:
        total_header = response.headers.get(
            "Content-Length"
        )
        total_bytes = (
            int(total_header)
            if total_header
            else None
        )

        downloaded = 0
        next_report = 50 * 1024 * 1024

        with destination.open("wb") as output:
            while True:
                block = response.read(
                    1024 * 1024
                )

                if not block:
                    break

                output.write(block)
                downloaded += len(block)

                if downloaded >= next_report:
                    if total_bytes:
                        percent = (
                            downloaded
                            / total_bytes
                            * 100
                        )
                        print(
                            "  Downloaded "
                            f"{downloaded / 1024 / 1024:,.0f} MB "
                            f"of {total_bytes / 1024 / 1024:,.0f} MB "
                            f"({percent:.1f}%)"
                        )
                    else:
                        print(
                            "  Downloaded "
                            f"{downloaded / 1024 / 1024:,.0f} MB"
                        )

                    next_report += (
                        50 * 1024 * 1024
                    )

    if not destination.exists():
        raise RuntimeError(
            f"Download did not create {destination.name}."
        )

    if destination.stat().st_size == 0:
        raise RuntimeError(
            f"Downloaded file is empty: {destination.name}"
        )

    print(
        "  Finished download: "
        f"{destination.stat().st_size / 1024 / 1024:,.1f} MB"
    )


def find_csv_member(
    archive: zipfile.ZipFile,
) -> str:
    csv_members = [
        name
        for name in archive.namelist()
        if name.lower().endswith(".csv")
    ]

    if len(csv_members) != 1:
        raise RuntimeError(
            "Expected exactly one CSV file in "
            f"{archive.filename}; found "
            f"{csv_members}."
        )

    return csv_members[0]


def fetch_target_lea_ids(
    client,
) -> Set[str]:
    rows: List[Dict[str, Any]] = []
    offset = 0

    while True:
        result = (
            client
            .table(MAPPING_TABLE)
            .select(
                "parcel_id,district_type,lea_id"
            )
            .order("lea_id")
            .order("parcel_id")
            .order("district_type")
            .range(
                offset,
                offset + MAPPING_PAGE_SIZE - 1,
            )
            .execute()
        )

        page = result.data or []
        rows.extend(page)

        if len(rows) % 10000 == 0 and rows:
            print(
                f"  Read {len(rows):,} district mappings"
            )

        if len(page) < MAPPING_PAGE_SIZE:
            break

        offset += MAPPING_PAGE_SIZE

    if not rows:
        raise RuntimeError(
            "No parcel school-district mappings were found."
        )

    lea_ids = {
        normalized
        for row in rows
        if (
            normalized := normalize_lea_id(
                row.get("lea_id")
            )
        )
    }

    if not lea_ids:
        raise RuntimeError(
            "The mapping table contains no usable NCES LEA IDs."
        )

    print(
        f"Mapped NCES districts: {len(lea_ids):,}"
    )

    return lea_ids


def read_directory(
    archive_path: Path,
    target_lea_ids: Set[str],
) -> Tuple[pd.DataFrame, int]:
    with zipfile.ZipFile(
        archive_path
    ) as archive:
        csv_member = find_csv_member(archive)

        with archive.open(csv_member) as source:
            directory = pd.read_csv(
                source,
                dtype="string",
                usecols=DIRECTORY_COLUMNS,
                low_memory=False,
            )

    rows_read = len(directory)

    directory["LEAID"] = directory[
        "LEAID"
    ].apply(normalize_lea_id)

    selected = directory.loc[
        directory["ST"].str.upper().eq("MT")
        & directory["LEAID"].isin(
            target_lea_ids
        )
    ].copy()

    selected = selected.drop_duplicates(
        subset=["LEAID"],
        keep="last",
    )

    return selected, rows_read


def read_membership_totals(
    archive_path: Path,
    target_lea_ids: Set[str],
) -> Tuple[pd.DataFrame, int]:
    selected_chunks: List[pd.DataFrame] = []
    rows_read = 0

    with zipfile.ZipFile(
        archive_path
    ) as archive:
        csv_member = find_csv_member(archive)

        with archive.open(csv_member) as source:
            reader = pd.read_csv(
                source,
                dtype="string",
                usecols=MEMBERSHIP_COLUMNS,
                chunksize=READ_CHUNK_SIZE,
                low_memory=False,
            )

            for chunk_number, chunk in enumerate(
                reader,
                start=1,
            ):
                rows_read += len(chunk)

                chunk["LEAID"] = chunk[
                    "LEAID"
                ].apply(normalize_lea_id)

                selected = chunk.loc[
                    chunk["ST"].str.upper().eq(
                        "MT"
                    )
                    & chunk["LEAID"].isin(
                        target_lea_ids
                    )
                    & chunk[
                        "TOTAL_INDICATOR"
                    ].str.strip().eq(
                        "Education Unit Total"
                    )
                ].copy()

                if not selected.empty:
                    selected_chunks.append(
                        selected
                    )

                if chunk_number % 10 == 0:
                    print(
                        "  Membership rows scanned: "
                        f"{rows_read:,}"
                    )

                del chunk
                gc.collect()

    if selected_chunks:
        selected_rows = pd.concat(
            selected_chunks,
            ignore_index=True,
        )
    else:
        selected_rows = pd.DataFrame(
            columns=MEMBERSHIP_COLUMNS
        )

    return selected_rows, rows_read


def read_teacher_totals(
    archive_path: Path,
    target_lea_ids: Set[str],
) -> Tuple[pd.DataFrame, int]:
    selected_chunks: List[pd.DataFrame] = []
    rows_read = 0

    with zipfile.ZipFile(
        archive_path
    ) as archive:
        csv_member = find_csv_member(archive)

        with archive.open(csv_member) as source:
            reader = pd.read_csv(
                source,
                dtype="string",
                usecols=STAFF_COLUMNS,
                chunksize=READ_CHUNK_SIZE,
                low_memory=False,
            )

            for chunk_number, chunk in enumerate(
                reader,
                start=1,
            ):
                rows_read += len(chunk)

                chunk["LEAID"] = chunk[
                    "LEAID"
                ].apply(normalize_lea_id)

                selected = chunk.loc[
                    chunk["ST"].str.upper().eq(
                        "MT"
                    )
                    & chunk["LEAID"].isin(
                        target_lea_ids
                    )
                    & chunk["STAFF"].str.strip().eq(
                        "Teachers"
                    )
                    & chunk[
                        "TOTAL_INDICATOR"
                    ].str.strip().eq(
                        "Derived - Major Staffing Category"
                    )
                ].copy()

                if not selected.empty:
                    selected_chunks.append(
                        selected
                    )

                if chunk_number % 10 == 0:
                    print(
                        "  Staff rows scanned: "
                        f"{rows_read:,}"
                    )

                del chunk
                gc.collect()

    if selected_chunks:
        selected_rows = pd.concat(
            selected_chunks,
            ignore_index=True,
        )
    else:
        selected_rows = pd.DataFrame(
            columns=STAFF_COLUMNS
        )

    return selected_rows, rows_read


def unique_metric_values(
    data: pd.DataFrame,
    value_column: str,
    label: str,
) -> Dict[str, float]:
    values: Dict[str, float] = {}
    conflicts: Dict[str, List[float]] = {}

    for lea_id, group in data.groupby(
        "LEAID",
        dropna=True,
    ):
        normalized_lea_id = normalize_lea_id(
            lea_id
        )

        if not normalized_lea_id:
            continue

        usable_values = sorted({
            number
            for raw_value in group[value_column]
            if (
                number := clean_number(
                    raw_value
                )
            ) is not None
        })

        if len(usable_values) == 1:
            values[normalized_lea_id] = (
                usable_values[0]
            )
        elif len(usable_values) > 1:
            conflicts[normalized_lea_id] = (
                usable_values
            )

    if conflicts:
        raise RuntimeError(
            f"Conflicting {label} values were found: "
            f"{conflicts}"
        )

    return values


def build_rows(
    directory: pd.DataFrame,
    membership: pd.DataFrame,
    staff: pd.DataFrame,
    target_lea_ids: Set[str],
) -> List[Dict[str, Any]]:
    directory_by_lea: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for source in directory.to_dict(
        "records"
    ):
        lea_id = normalize_lea_id(
            source.get("LEAID")
        )

        if lea_id:
            directory_by_lea[lea_id] = source

    students_by_lea = unique_metric_values(
        membership,
        "STUDENT_COUNT",
        "student enrollment",
    )

    teachers_by_lea = unique_metric_values(
        staff,
        "STAFF_COUNT",
        "teacher FTE",
    )

    rows: List[Dict[str, Any]] = []

    for lea_id in sorted(target_lea_ids):
        directory_row = directory_by_lea.get(
            lea_id
        )

        if not directory_row:
            continue

        total_students = students_by_lea.get(
            lea_id
        )
        teacher_fte = teachers_by_lea.get(
            lea_id
        )

        if (
            total_students is None
            or teacher_fte is None
            or teacher_fte <= 0
        ):
            continue

        student_teacher_ratio = round(
            total_students / teacher_fte,
            2,
        )

        district_name = clean_text(
            directory_row.get("LEA_NAME")
        )

        if not district_name:
            continue

        rows.append({
            "lea_id": lea_id,
            "school_year": SCHOOL_YEAR,
            "district_name": district_name,
            "state_abbreviation": "MT",
            "total_students": (
                int(total_students)
                if total_students.is_integer()
                else total_students
            ),
            "teacher_fte": round(
                teacher_fte,
                2,
            ),
            "student_teacher_ratio":
                student_teacher_ratio,
            "source_name": SOURCE_NAME,
            "directory_source_file":
                DIRECTORY_FILE,
            "membership_source_file":
                MEMBERSHIP_FILE,
            "staff_source_file":
                STAFF_FILE,
            "source_url": SOURCE_URL,
            "source_date":
                SOURCE_DATE.isoformat(),
            "confidence_level":
                "context_only",
            "notes": (
                "District-level public school enrollment "
                "and staffing context from the NCES "
                "Common Core of Data. The "
                "student-teacher ratio is calculated from "
                "district enrollment and teacher FTE. "
                "These values are not a school quality "
                "score, ranking, recommendation, or "
                "parcel valuation adjustment."
            ),
            "updated_at": utc_now(),
        })

    return rows


def validate_rows(
    rows: List[Dict[str, Any]],
    target_lea_ids: Set[str],
    directory: pd.DataFrame,
    membership: pd.DataFrame,
    staff: pd.DataFrame,
) -> Dict[str, Any]:
    if not rows:
        raise RuntimeError(
            "No NCES district indicator rows were prepared."
        )

    row_lea_ids = {
        row["lea_id"]
        for row in rows
    }

    missing_lea_ids = sorted(
        target_lea_ids - row_lea_ids
    )

    if missing_lea_ids:
        raise RuntimeError(
            "NCES metrics were not available for all "
            "mapped districts. Missing LEA IDs: "
            f"{missing_lea_ids}"
        )

    if len(rows) != len(target_lea_ids):
        raise RuntimeError(
            "Prepared row count does not match the "
            "number of mapped districts."
        )

    key_count = len({
        (
            row["lea_id"],
            row["school_year"],
        )
        for row in rows
    })

    if key_count != len(rows):
        raise RuntimeError(
            "Prepared NCES rows contain duplicate "
            "LEA ID and school-year keys."
        )

    invalid_rows = [
        row
        for row in rows
        if (
            not row.get("district_name")
            or row.get("total_students") is None
            or row.get("teacher_fte") is None
            or row.get(
                "student_teacher_ratio"
            ) is None
            or float(row["teacher_fte"]) <= 0
            or float(
                row["student_teacher_ratio"]
            ) <= 0
        )
    ]

    if invalid_rows:
        raise RuntimeError(
            "Prepared NCES rows contain invalid "
            f"metrics: {invalid_rows[:10]}"
        )

    school_years = {
        clean_text(value)
        for value in directory[
            "SCHOOL_YEAR"
        ].tolist()
        if clean_text(value)
    }

    if school_years != {SCHOOL_YEAR}:
        raise RuntimeError(
            "Unexpected school year in the NCES "
            f"directory data: {sorted(school_years)}"
        )

    return {
        "school_year": SCHOOL_YEAR,
        "mapped_district_count":
            len(target_lea_ids),
        "directory_matches": len(directory),
        "membership_total_rows":
            len(membership),
        "teacher_total_rows": len(staff),
        "prepared_rows": len(rows),
        "missing_lea_ids": missing_lea_ids,
    }


def upload_rows(
    client,
    rows: List[Dict[str, Any]],
) -> None:
    uploaded = 0

    for batch in chunks(
        rows,
        UPLOAD_CHUNK_SIZE,
    ):
        (
            client
            .table(INDICATOR_TABLE)
            .upsert(
                batch,
                on_conflict=(
                    "lea_id,school_year"
                ),
            )
            .execute()
        )

        uploaded += len(batch)

        print(
            f"  Uploaded {uploaded:,} "
            f"of {len(rows):,} district indicators"
        )


def start_pipeline_run(
    client,
) -> Optional[str]:
    values = {
        "source_name": SOURCE_NAME,
        "job_name": JOB_NAME,
        "status": "running",
        "started_at": utc_now(),
        "source_period": SCHOOL_YEAR,
        "rows_read": 0,
        "rows_written": 0,
        "validation_summary": {},
    }

    try:
        result = (
            client
            .table(PIPELINE_RUN_TABLE)
            .insert(values)
            .execute()
        )

        data = result.data or []

        if data:
            return data[0].get("id")
    except Exception as error:
        print(
            "Warning: could not create pipeline "
            f"run record: {error}"
        )

    return None


def finish_pipeline_run(
    client,
    run_id: Optional[str],
    status: str,
    *,
    rows_read: int = 0,
    rows_written: int = 0,
    validation_summary: Optional[
        Dict[str, Any]
    ] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return

    values = {
        "status": status,
        "completed_at": utc_now(),
        "source_period": SCHOOL_YEAR,
        "rows_read": rows_read,
        "rows_written": rows_written,
        "validation_summary":
            validation_summary or {},
        "error_message": error_message,
    }

    try:
        (
            client
            .table(PIPELINE_RUN_TABLE)
            .update(values)
            .eq("id", run_id)
            .execute()
        )
    except Exception as error:
        print(
            "Warning: could not finish pipeline "
            f"run record: {error}"
        )


def main() -> None:
    client = get_supabase()
    run_id = start_pipeline_run(client)

    total_rows_read = 0
    rows_written = 0
    validation_summary: Dict[str, Any] = {}

    try:
        print(
            "Loading mapped NCES school districts..."
        )
        target_lea_ids = fetch_target_lea_ids(
            client
        )

        with tempfile.TemporaryDirectory() as temp:
            temp_directory = Path(temp)

            directory_path = (
                temp_directory / DIRECTORY_FILE
            )
            membership_path = (
                temp_directory / MEMBERSHIP_FILE
            )
            staff_path = (
                temp_directory / STAFF_FILE
            )

            print(
                "Downloading NCES district directory..."
            )
            download_file(
                DIRECTORY_URL,
                directory_path,
            )

            print(
                "Downloading NCES district membership..."
            )
            download_file(
                MEMBERSHIP_URL,
                membership_path,
            )

            print(
                "Downloading NCES district staffing..."
            )
            download_file(
                STAFF_URL,
                staff_path,
            )

            print(
                "Reading NCES district directory..."
            )
            directory, directory_rows_read = (
                read_directory(
                    directory_path,
                    target_lea_ids,
                )
            )
            total_rows_read += (
                directory_rows_read
            )

            print(
                "Reading NCES district enrollment..."
            )
            membership, membership_rows_read = (
                read_membership_totals(
                    membership_path,
                    target_lea_ids,
                )
            )
            total_rows_read += (
                membership_rows_read
            )

            print(
                "Reading NCES teacher staffing..."
            )
            staff, staff_rows_read = (
                read_teacher_totals(
                    staff_path,
                    target_lea_ids,
                )
            )
            total_rows_read += staff_rows_read

            print(
                "Preparing NCES district indicators..."
            )
            rows = build_rows(
                directory,
                membership,
                staff,
                target_lea_ids,
            )

            print(
                "Validating NCES district indicators..."
            )
            validation_summary = validate_rows(
                rows,
                target_lea_ids,
                directory,
                membership,
                staff,
            )

            print(
                "Updating NCES district indicators..."
            )
            upload_rows(client, rows)
            rows_written = len(rows)

        finish_pipeline_run(
            client,
            run_id,
            "succeeded",
            rows_read=total_rows_read,
            rows_written=rows_written,
            validation_summary=
                validation_summary,
        )

        print()
        print(
            "NCES school indicator update succeeded."
        )
        print(
            f"School year: {SCHOOL_YEAR}"
        )
        print(
            "Mapped districts processed: "
            f"{rows_written}"
        )
        print(
            "Source rows read: "
            f"{total_rows_read:,}"
        )

        for row in sorted(
            rows,
            key=lambda item: (
                item["district_name"]
            ),
        ):
            print(
                f"{row['lea_id']} | "
                f"{row['district_name']} | "
                f"{row['total_students']:,} students | "
                f"{row['teacher_fte']:,.2f} teacher FTE | "
                f"{row['student_teacher_ratio']:.2f} ratio"
            )

    except Exception as error:
        finish_pipeline_run(
            client,
            run_id,
            "failed",
            rows_read=total_rows_read,
            rows_written=rows_written,
            validation_summary=
                validation_summary,
            error_message=str(error),
        )
        raise


if __name__ == "__main__":
    main()