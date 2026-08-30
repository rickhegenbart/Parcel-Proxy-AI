import os
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client


from .config import APP_NAME, APP_ENV, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
from .model_service import model_service
from .schemas import (
    HealthResponse,
    ManualPredictionRequest,
    ParcelPredictionRequest,
    PredictionResponse,
    SearchResult,
)
from .supabase_client import (
    fetch_proxy_training_row_by_parcel_id,
    search_parcels,
    supabase_is_configured,
)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "https://repredict.onrender.com,http://localhost:5173,http://100.115.92.194:5173"
    ).split(",")
    if origin.strip()
]

app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description="FastAPI backend for the public parcel value proxy model."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,  # Lock this down before production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_model=HealthResponse)
def root():
    return HealthResponse(
        status=f"{APP_NAME} running in {APP_ENV}",
        model_loaded=model_service.is_loaded,
        feature_count=len(model_service.feature_columns),
        supabase_configured=supabase_is_configured(),
    )


@app.get("/health", response_model=HealthResponse)
def health():
    return root()


@app.get("/api/v1/model/metadata")
def model_metadata():
    return {
        "model_loaded": model_service.is_loaded,
        "feature_count": len(model_service.feature_columns),
        "feature_columns": model_service.feature_columns,
        "model_metrics": model_service.model_metrics,
        "model_metadata": model_service.model_metadata,
    }


@app.get("/api/v1/parcels/search", response_model=list[SearchResult])
def parcel_search(
    q: str = Query(..., min_length=2, description="Parcel ID or address search text"),
    limit: int = Query(20, ge=1, le=50),
):
    if not supabase_is_configured():
        raise HTTPException(status_code=500, detail="Supabase is not configured.")

    return search_parcels(q, limit=limit)


@app.get("/api/v1/parcels/{parcel_id}")
def get_parcel(parcel_id: str):
    if not supabase_is_configured():
        raise HTTPException(status_code=500, detail="Supabase is not configured.")

    row = fetch_proxy_training_row_by_parcel_id(parcel_id)

    if not row:
        raise HTTPException(status_code=404, detail=f"Parcel not found in proxy_training_data: {parcel_id}")

    return row


@app.post("/api/v1/predictions/by-parcel", response_model=PredictionResponse)
def predict_by_parcel(request: ParcelPredictionRequest):
    if not model_service.is_loaded:
        raise HTTPException(
            status_code=500,
            detail="Model artifacts are missing. Add price_model.pkl and feature_columns.json to app/ml/artifacts/",
        )

    if not supabase_is_configured():
        raise HTTPException(status_code=500, detail="Supabase is not configured.")

    row = fetch_proxy_training_row_by_parcel_id(request.parcel_id)

    if not row:
        raise HTTPException(status_code=404, detail=f"Parcel not found in proxy_training_data: {request.parcel_id}")

    result = model_service.predict(row)
    result["parcel"] = model_service.parcel_summary(row)

    return result


@app.post("/api/v1/predictions/manual", response_model=PredictionResponse)
def predict_manual(request: ManualPredictionRequest):
    if not model_service.is_loaded:
        raise HTTPException(
            status_code=500,
            detail="Model artifacts are missing. Add price_model.pkl and feature_columns.json to app/ml/artifacts/",
        )

    result = model_service.predict(request.features)
    result["parcel"] = None

    return result


class FeedbackSubmission(BaseModel):
    parcel_id: str | None = None
    property_id: str | None = None
    address_line_1: str | None = None
    rating: str = Field(..., pattern="^(too_low|about_right|too_high)$")
    comment: str | None = None
    baseline_estimate: float | None = None
    adjusted_estimate: float | None = None


@app.post("/api/v1/feedback")
def submit_feedback(feedback: FeedbackSubmission):
    """
    Capture user feedback about whether an estimate felt too low,
    about right, or too high.
    """
    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        payload = feedback.model_dump()
        payload["source"] = "repredict_frontend"

        result = client.table("parcel_feedback").insert(payload).execute()

        feedback_id = None
        if result.data and len(result.data) > 0:
            feedback_id = result.data[0].get("id")

        return {
            "status": "saved",
            "feedback_id": feedback_id,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to save feedback: {str(exc)}"
        )


def verify_admin_feedback_token(x_admin_token: str | None = Header(default=None)):
    """
    Simple private-token protection for admin feedback review endpoints.
    Do not expose this token in frontend code.
    """
    expected_token = os.getenv("ADMIN_FEEDBACK_TOKEN", "")

    if not expected_token:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_FEEDBACK_TOKEN is not configured."
        )

    if x_admin_token != expected_token:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized."
        )


@app.get("/api/v1/admin/feedback/recent")
def get_recent_feedback(
    limit: int = Query(default=25, ge=1, le=100),
    x_admin_token: str | None = Header(default=None),
):
    """
    Private endpoint for reviewing recent parcel estimate feedback.
    """
    verify_admin_feedback_token(x_admin_token)

    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        result = (
            client
            .table("parcel_feedback")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        rows = result.data or []

        return {
            "count": len(rows),
            "feedback": rows,
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to load feedback: {str(exc)}"
        )


@app.get("/api/v1/admin/feedback/summary")
def get_feedback_summary(
    x_admin_token: str | None = Header(default=None),
):
    """
    Private endpoint for feedback rating counts and estimate averages.
    """
    verify_admin_feedback_token(x_admin_token)

    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        result = (
            client
            .table("parcel_feedback_summary")
            .select("*")
            .execute()
        )

        return {
            "summary": result.data or [],
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to load feedback summary: {str(exc)}"
        )


class SiteEventSubmission(BaseModel):
    event_name: str
    page_path: str | None = None
    referrer: str | None = None
    metadata: dict | None = None


@app.post("/api/v1/events")
def capture_site_event(event: SiteEventSubmission):
    """
    Capture first-party REPredict traffic and product events.
    Does not collect names, emails, or IP addresses.
    """
    allowed_events = {
        "page_view",
        "search_submitted",
        "parcel_selected",
        "feedback_submitted",
        "admin_dashboard_opened",
    }

    if event.event_name not in allowed_events:
        raise HTTPException(status_code=400, detail="Unsupported event name.")

    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        payload = event.dict()
        payload["source"] = "repredict_frontend"

        client.table("site_events").insert(payload).execute()

        return {"status": "saved"}

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to save site event: {str(exc)}"
        )


@app.get("/api/v1/admin/traffic/summary")
def get_traffic_summary(
    x_admin_token: str | None = Header(default=None),
):
    """
    Private admin endpoint for traffic totals.
    """
    expected_token = os.getenv("ADMIN_FEEDBACK_TOKEN", "")

    if not expected_token:
        raise HTTPException(status_code=500, detail="ADMIN_FEEDBACK_TOKEN is not configured.")

    if x_admin_token != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        result = (
            client
            .table("site_events")
            .select("event_name,created_at")
            .order("created_at", desc=True)
            .limit(10000)
            .execute()
        )

        rows = result.data or []

        counts = {
            "page_views": 0,
            "searches": 0,
            "parcel_selections": 0,
            "feedback_submissions": 0,
            "admin_dashboard_opened": 0,
            "total_events": len(rows),
            "latest_event_at": rows[0]["created_at"] if rows else None,
        }

        for row in rows:
            name = row.get("event_name")

            if name == "page_view":
                counts["page_views"] += 1
            elif name == "search_submitted":
                counts["searches"] += 1
            elif name == "parcel_selected":
                counts["parcel_selections"] += 1
            elif name == "feedback_submitted":
                counts["feedback_submissions"] += 1
            elif name == "admin_dashboard_opened":
                counts["admin_dashboard_opened"] += 1

        return counts

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to load traffic summary: {str(exc)}"
        )


@app.get("/api/v1/admin/traffic/daily")
def get_daily_traffic(
    limit: int = Query(default=30, ge=1, le=365),
    x_admin_token: str | None = Header(default=None),
):
    """
    Private admin endpoint for daily traffic totals.
    """
    expected_token = os.getenv("ADMIN_FEEDBACK_TOKEN", "")

    if not expected_token:
        raise HTTPException(status_code=500, detail="ADMIN_FEEDBACK_TOKEN is not configured.")

    if x_admin_token != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        result = (
            client
            .table("site_traffic_daily")
            .select("*")
            .limit(limit)
            .execute()
        )

        return {
            "daily": result.data or []
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to load daily traffic: {str(exc)}"
        )



def filter_context_for_location(rows, parcel):
    """
    Keep the most relevant public-safety and school context rows for the
    parcel's city/address.

    Environmental risk remains county-level for now because the current FEMA
    NRI import is county-level. Construction cost remains national.
    """
    parcel = parcel or {}

    city = (parcel.get("site_city") or "").upper()
    address = (parcel.get("address_line_1") or "").upper()

    # Some parcel records may have city blank but address/city-like text elsewhere.
    location_text = f"{city} {address}"

    def school_match(geography):
        geography = (geography or "").upper()

        if "BILLINGS" in location_text:
            return geography.startswith("BILLINGS ")

        if "LAUREL" in location_text:
            return geography.startswith("LAUREL ")

        if "LOCKWOOD" in location_text:
            return "LOCKWOOD" in geography

        if "SHEPHERD" in location_text:
            return "SHEPHERD" in geography

        if "HUNTLEY" in location_text:
            return "HUNTLEY" in geography

        if "BROADVIEW" in location_text:
            return "BROADVIEW" in geography

        if "BLUE CREEK" in location_text:
            return "BLUE CREEK" in geography

        if "ELDER GROVE" in location_text:
            return "ELDER GROVE" in geography

        if "ELYSIAN" in location_text:
            return "ELYSIAN" in geography

        # Fallback: if we do not know the school area, keep the county-area
        # district rows rather than hiding the entire school context.
        return True

    def public_safety_match(geography):
        geography = (geography or "").upper()

        if "BILLINGS" in location_text:
            return "BILLINGS POLICE" in geography

        # Fallback for non-Billings Yellowstone County parcels.
        return "YELLOWSTONE COUNTY" in geography

    filtered = []

    for row in rows:
        category = row.get("context_category")
        geography = row.get("geography_name") or ""

        if category == "school_context":
            if school_match(geography):
                filtered.append(row)

        elif category == "public_safety":
            if public_safety_match(geography):
                filtered.append(row)

        else:
            # Keep environmental, civic, parcel-specific, and other context rows.
            filtered.append(row)

    return filtered


@app.get("/api/v1/parcels/{parcel_id}/context")
def get_parcel_context(parcel_id: str):
    """
    Return public-data context indicators for a parcel.

    Construction cost indicators are currently national context indicators.
    They are not part of the trained valuation model yet.
    """
    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

        grouped = {
            "construction_cost": [],
            "environmental_risk": [],
            "school_context": [],
            "public_safety": [],
            "civic_disruption": [],
        }

        # Look up the parcel and its coordinate-derived Census tract.
        parcel_row = fetch_proxy_training_row_by_parcel_id(parcel_id) or {}

        mapping_result = (
            client
            .table("parcel_census_tract_map")
            .select("tract_fips, county_fips, tract_name")
            .eq("parcel_id", parcel_id)
            .limit(1)
            .execute()
        )

        mapping_row = (
            mapping_result.data[0]
            if mapping_result.data
            else {}
        )

        tract_fips = mapping_row.get("tract_fips")
        county_fips = mapping_row.get("county_fips")
        tract_context_id = (
            f"tract:{tract_fips}"
            if tract_fips
            else None
        )

        # Only use the Yellowstone fallback when the coordinates actually
        # place the parcel in Yellowstone County.
        county_context_id = (
            "county:yellowstone_mt"
            if county_fips == "111"
            else None
        )

        context_ids = [parcel_id]

        if tract_context_id:
            context_ids.append(tract_context_id)

        if county_context_id:
            context_ids.append(county_context_id)

        parcel_result = (
            client
            .table("parcel_context_indicators")
            .select("*")
            .in_("parcel_id", context_ids)
            .order("context_category")
            .execute()
        )

        context_rows = parcel_result.data or []

        # Environmental priority:
        # exact parcel -> Census tract -> county fallback.
        environmental_priority = [
            parcel_id,
            tract_context_id,
            county_context_id,
        ]

        selected_environmental_id = next(
            (
                candidate
                for candidate in environmental_priority
                if candidate
                and any(
                    row.get("context_category") == "environmental_risk"
                    and row.get("parcel_id") == candidate
                    for row in context_rows
                )
            ),
            None,
        )

        if selected_environmental_id:
            context_rows = [
                row
                for row in context_rows
                if (
                    row.get("context_category") != "environmental_risk"
                    or row.get("parcel_id") == selected_environmental_id
                )
            ]

        location_filtered_rows = filter_context_for_location(
            context_rows,
            parcel_row,
        )

        for row in location_filtered_rows:
            category = row.get("context_category")
            if category in grouped:
                grouped[category].append(row)

        # 2. Load latest construction-cost indicators.
        # These are national context rows and apply broadly across parcels.
        construction_result = (
            client
            .table("construction_cost_indicators")
            .select(
                "period, series_id, metric_name, metric_value, metric_unit, "
                "mom_change_pct, yoy_change_pct, cost_pressure_label, "
                "source_name, geography_name, geography_level, "
                "context_category, confidence_level, notes"
            )
            .order("period", desc=True)
            .order("series_id")
            .limit(6)
            .execute()
        )

        for row in construction_result.data or []:
            grouped["construction_cost"].append({
                "id": f"construction-{row.get('series_id')}",
                "parcel_id": parcel_id,
                "geography_name": row.get("geography_name") or "United States",
                "geography_level": row.get("geography_level") or "national",
                "context_category": "construction_cost",
                "metric_name": row.get("metric_name"),
                "metric_value": row.get("metric_value"),
                "metric_text": row.get("cost_pressure_label"),
                "metric_unit": row.get("metric_unit"),
                "mom_change_pct": row.get("mom_change_pct"),
                "yoy_change_pct": row.get("yoy_change_pct"),
                "cost_pressure_label": row.get("cost_pressure_label"),
                "source_name": row.get("source_name") or "BLS Producer Price Index via FRED",
                "source_period": row.get("period"),
                "confidence_level": row.get("confidence_level") or "context_only",
                "notes": row.get("notes"),
            })

        return {
            "parcel_id": parcel_id,
            "tract_fips": tract_fips,
            "tract_name": mapping_row.get("tract_name"),
            "context": grouped,
            "disclaimer": (
                "These indicators are public-data context layers only. "
                "They are not an appraisal, CMA, MLS valuation, contractor bid, "
                "insurance replacement estimate, safety score, school quality score, "
                "or political opinion score."
            ),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to load parcel context: {str(exc)}"
        )
