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
    fetch_latest_fred_features,
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
    fred_context = fetch_latest_fred_features()

    prediction_features = {
        **row,
        **fred_context["features"],
    }

    result = model_service.predict(prediction_features)
    result["parcel"] = model_service.parcel_summary(row)
    result["data_freshness"] = fred_context["freshness"]

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
    Keep the most relevant public-safety rows for the parcel's location.

    School context is loaded separately using the parcel's exact Census
    TIGER school-district mapping. Older generic school-context rows are
    excluded here to prevent incorrect city-name or county-wide matches.
    """
    parcel = parcel or {}

    city = (
        parcel.get("site_city")
        or ""
    ).upper()

    address = (
        parcel.get("address_line_1")
        or ""
    ).upper()

    location_text = f"{city} {address}"

    def public_safety_match(geography):
        geography = (
            geography
            or ""
        ).upper()

        if "BILLINGS" in location_text:
            return (
                "BILLINGS POLICE"
                in geography
            )

        # Fallback for non-Billings
        # Yellowstone County parcels.
        return (
            "YELLOWSTONE COUNTY"
            in geography
        )

    filtered = []

    for row in rows:
        category = row.get(
            "context_category"
        )
        geography = (
            row.get("geography_name")
            or ""
        )

        if category == "school_context":
            # Exact school context is loaded from
            # parcel_school_district_map below.
            continue

        if category == "public_safety":
            if public_safety_match(
                geography
            ):
                filtered.append(row)
        else:
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
            "demographic_context": [],
            "rental_context": [],
            "storm_history": [],
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
                # Load the Yellowstone County NOAA storm-history summary.
        # NOAA records are county-level historical context and do not
        # represent parcel-specific hazard exposure.
        if county_context_id:
            storm_result = (
                client
                .table("noaa_storm_event_summary")
                .select("*")
                .eq("county_fips", "30111")
                .limit(1)
                .execute()
            )

            storm_summary = (
                storm_result.data[0]
                if storm_result.data
                else None
            )

            if storm_summary:
                earliest_event = str(
                    storm_summary.get("earliest_event_date")
                    or ""
                )[:10]
                latest_event = str(
                    storm_summary.get("latest_event_date")
                    or ""
                )[:10]
                source_revision = str(
                    storm_summary.get(
                        "latest_source_revision_date"
                    )
                    or ""
                )[:10]

                property_damage = float(
                    storm_summary.get(
                        "reported_property_damage"
                    )
                    or 0
                )
                total_injuries = int(
                    storm_summary.get("total_injuries")
                    or 0
                )
                total_deaths = int(
                    storm_summary.get("total_deaths")
                    or 0
                )

                storm_metrics = [
                    {
                        "metric_name":
                            "Total recorded storm events",
                        "metric_value":
                            storm_summary.get(
                                "total_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('total_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name":
                            "Events recorded in the last 10 years",
                        "metric_value":
                            storm_summary.get(
                                "recent_10_year_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('recent_10_year_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name": "Recorded hail events",
                        "metric_value":
                            storm_summary.get(
                                "hail_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('hail_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name":
                            "Recorded thunderstorm-wind events",
                        "metric_value":
                            storm_summary.get(
                                "thunderstorm_wind_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('thunderstorm_wind_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name":
                            "Recorded flood and flash-flood events",
                        "metric_value":
                            storm_summary.get(
                                "flood_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('flood_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name":
                            "Recorded tornado events",
                        "metric_value":
                            storm_summary.get(
                                "tornado_event_count"
                            ),
                        "metric_text":
                            (
                                f"{storm_summary.get('tornado_event_count')} "
                                "events"
                            ),
                        "metric_unit": "events",
                    },
                    {
                        "metric_name":
                            "Reported property damage",
                        "metric_value": property_damage,
                        "metric_text":
                            f"${property_damage:,.0f}",
                        "metric_unit": "USD",
                    },
                    {
                        "metric_name":
                            "Recorded injuries and deaths",
                        "metric_value": None,
                        "metric_text":
                            (
                                f"{total_injuries} injuries · "
                                f"{total_deaths} deaths"
                            ),
                        "metric_unit": None,
                    },
                    {
                        "metric_name":
                            "Latest recorded storm event",
                        "metric_value": None,
                        "metric_text":
                            latest_event or "Not available",
                        "metric_unit": None,
                    },
                ]

                for index, metric in enumerate(
                    storm_metrics,
                    start=1,
                ):
                    grouped["storm_history"].append({
                        "id": f"noaa-storm-{index}",
                        "parcel_id": parcel_id,
                        "geography_name":
                            "Yellowstone County, MT",
                        "geography_level": "county",
                        "context_category":
                            "storm_history",
                        "metric_name":
                            metric["metric_name"],
                        "metric_value":
                            metric["metric_value"],
                        "metric_text":
                            metric["metric_text"],
                        "metric_unit":
                            metric["metric_unit"],
                        "source_name":
                            "NOAA Storm Events Database",
                        "source_period":
                            (
                                f"{earliest_event} through "
                                f"{latest_event}"
                            ),
                        "source_date": source_revision,
                        "confidence_level":
                            "context_only",
                        "notes": (
                            "Historical county-level reports. "
                            "Reporting practices and event coverage "
                            "have changed over time. A missing event "
                            "does not prove that severe weather did "
                            "not occur at a specific parcel."
                        ),
                    })
                # Load the latest ACS demographic indicators for the parcel's
        # coordinate-derived Census tract. These estimates describe the
        # surrounding tract and are not parcel-specific characteristics.
        if tract_fips:
            demographic_result = (
                client
                .table("acs_tract_indicators")
                .select("*")
                .eq("tract_fips", tract_fips)
                .order("release_year", desc=True)
                .limit(1)
                .execute()
            )

            demographic_row = (
                demographic_result.data[0]
                if demographic_result.data
                else None
            )

            if demographic_row:
                release_year = demographic_row.get(
                    "release_year"
                )
                geography_name = (
                    demographic_row.get("geography_name")
                    or mapping_row.get("tract_name")
                    or f"Census tract {tract_fips}"
                )

                def format_count(value):
                    if value is None:
                        return "Not available"
                    return f"{int(value):,}"

                def format_currency(value):
                    if value is None:
                        return "Not available"
                    return f"${float(value):,.0f}"

                def format_percentage(value):
                    if value is None:
                        return "Not available"
                    return f"{float(value):.1f}%"

                def format_decimal(value):
                    if value is None:
                        return "Not available"
                    return f"{float(value):.1f}"

                demographic_metrics = [
                    {
                        "metric_name": "Total population",
                        "field": "total_population",
                        "metric_unit": "people",
                        "formatter": format_count,
                    },
                    {
                        "metric_name": "Median age",
                        "field": "median_age",
                        "metric_unit": "years",
                        "formatter": format_decimal,
                    },
                    {
                        "metric_name": "Median household income",
                        "field": "median_household_income",
                        "metric_unit": "USD",
                        "formatter": format_currency,
                    },
                    {
                        "metric_name": "Population below poverty",
                        "field": "poverty_rate_pct",
                        "metric_unit": "percent",
                        "formatter": format_percentage,
                    },
                    {
                        "metric_name": "Civilian unemployment rate",
                        "field": "unemployment_rate_pct",
                        "metric_unit": "percent",
                        "formatter": format_percentage,
                    },
                    {
                        "metric_name": "Housing units",
                        "field": "housing_units",
                        "metric_unit": "units",
                        "formatter": format_count,
                    },
                    {
                        "metric_name": "Housing vacancy rate",
                        "field": "vacancy_rate_pct",
                        "metric_unit": "percent",
                        "formatter": format_percentage,
                    },
                    {
                        "metric_name": "Owner-occupancy rate",
                        "field": "owner_occupancy_rate_pct",
                        "metric_unit": "percent",
                        "formatter": format_percentage,
                    },
                    {
                        "metric_name": "Median home value",
                        "field": "median_home_value",
                        "metric_unit": "USD",
                        "formatter": format_currency,
                    },
                    {
                        "metric_name": "Median gross rent",
                        "field": "median_gross_rent",
                        "metric_unit": "USD per month",
                        "formatter": format_currency,
                    },
                ]

                for index, metric in enumerate(
                    demographic_metrics,
                    start=1,
                ):
                    value = demographic_row.get(
                        metric["field"]
                    )

                    grouped["demographic_context"].append({
                        "id": f"acs-demographic-{index}",
                        "parcel_id": parcel_id,
                        "geography_name": geography_name,
                        "geography_level": "census_tract",
                        "context_category":
                            "demographic_context",
                        "metric_name":
                            metric["metric_name"],
                        "metric_value": value,
                        "metric_text":
                            metric["formatter"](value),
                        "metric_unit":
                            metric["metric_unit"],
                        "source_name":
                            demographic_row.get(
                                "source_name"
                            )
                            or (
                                "U.S. Census Bureau ACS "
                                "5-Year Estimates"
                            ),
                        "source_period":
                            (
                                f"{release_year} ACS "
                                "5-Year Estimates"
                            ),
                        "source_date":
                            demographic_row.get(
                                "source_date"
                            ),
                        "confidence_level":
                            "context_only",
                        "notes": (
                            "Census-tract statistical estimate. "
                            "It describes the surrounding area, "
                            "not the occupants, income, employment, "
                            "housing value, or rent of this parcel. "
                            "ACS estimates include sampling error."
                        ),
                    })

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
        # Load the latest HUD Fair Market Rent benchmarks for
        # Yellowstone County. FMRs are regional rental benchmarks,
        # not rent estimates for an individual parcel.
        if county_context_id:
            hud_result = (
                client
                .table("hud_fair_market_rents")
                .select("*")
                .eq("county_fips", "30111")
                .order("fiscal_year", desc=True)
                .limit(1)
                .execute()
            )

            hud_row = (
                hud_result.data[0]
                if hud_result.data
                else None
            )

            if hud_row:
                fiscal_year = hud_row.get("fiscal_year")
                area_name = (
                    hud_row.get("area_name")
                    or "Billings, MT HUD Metro FMR Area"
                )

                hud_metrics = [
                    {
                        "metric_name":
                            "Efficiency fair market rent",
                        "rent_field":
                            "efficiency_rent",
                        "change_field":
                            "efficiency_yoy_change_pct",
                    },
                    {
                        "metric_name":
                            "One-bedroom fair market rent",
                        "rent_field":
                            "one_bedroom_rent",
                        "change_field":
                            "one_bedroom_yoy_change_pct",
                    },
                    {
                        "metric_name":
                            "Two-bedroom fair market rent",
                        "rent_field":
                            "two_bedroom_rent",
                        "change_field":
                            "two_bedroom_yoy_change_pct",
                    },
                    {
                        "metric_name":
                            "Three-bedroom fair market rent",
                        "rent_field":
                            "three_bedroom_rent",
                        "change_field":
                            "three_bedroom_yoy_change_pct",
                    },
                    {
                        "metric_name":
                            "Four-bedroom fair market rent",
                        "rent_field":
                            "four_bedroom_rent",
                        "change_field":
                            "four_bedroom_yoy_change_pct",
                    },
                ]

                for index, metric in enumerate(
                    hud_metrics,
                    start=1,
                ):
                    rent_value = hud_row.get(
                        metric["rent_field"]
                    )
                    change_value = hud_row.get(
                        metric["change_field"]
                    )

                    rent_text = (
                        f"${float(rent_value):,.0f}/month"
                        if rent_value is not None
                        else "Not available"
                    )

                    grouped["rental_context"].append({
                        "id": f"hud-fmr-{index}",
                        "parcel_id": parcel_id,
                        "geography_name": area_name,
                        "geography_level": "county_fmr_area",
                        "context_category":
                            "rental_context",
                        "metric_name":
                            metric["metric_name"],
                        "metric_value": rent_value,
                        "metric_text": rent_text,
                        "metric_unit": "USD per month",
                        "yoy_change_pct": change_value,
                        "source_name":
                            hud_row.get("source_name")
                            or "HUD Fair Market Rents",
                        "source_period":
                            f"FY {fiscal_year}",
                        "confidence_level":
                            "context_only",
                        "notes": (
                            "HUD Fair Market Rent is a regional "
                            "gross-rent benchmark used for housing "
                            "program administration. It is not an "
                            "asking-rent estimate, rental appraisal, "
                            "or prediction for this parcel."
                        ),
                    })
                # Load the parcel's exact Census TIGER school districts and
        # attach the latest NCES enrollment and staffing indicators.
        school_mapping_result = (
            client
            .table("parcel_school_district_map")
            .select(
                "district_type, district_geoid, lea_id, "
                "district_name, low_grade, high_grade, "
                "tiger_year"
            )
            .eq("parcel_id", parcel_id)
            .order("district_type")
            .execute()
        )

        school_mappings = (
            school_mapping_result.data
            or []
        )

        school_lea_ids = sorted({
            str(mapping.get("lea_id")).strip()
            for mapping in school_mappings
            if mapping.get("lea_id")
        })

        if school_lea_ids:
            school_indicator_result = (
                client
                .table(
                    "school_district_indicators"
                )
                .select(
                    "lea_id, school_year, district_name, "
                    "total_students, teacher_fte, "
                    "student_teacher_ratio, source_name, "
                    "source_url, source_date, "
                    "confidence_level, notes"
                )
                .in_("lea_id", school_lea_ids)
                .order(
                    "school_year",
                    desc=True,
                )
                .execute()
            )

            latest_indicator_by_lea = {}

            for indicator in (
                school_indicator_result.data
                or []
            ):
                indicator_lea_id = str(
                    indicator.get("lea_id")
                    or ""
                ).strip()

                if (
                    indicator_lea_id
                    and indicator_lea_id
                    not in latest_indicator_by_lea
                ):
                    latest_indicator_by_lea[
                        indicator_lea_id
                    ] = indicator

            district_type_order = {
                "elementary": 1,
                "secondary": 2,
                "unified": 3,
            }

            school_mappings = sorted(
                school_mappings,
                key=lambda mapping: (
                    district_type_order.get(
                        mapping.get(
                            "district_type"
                        ),
                        99,
                    ),
                    mapping.get(
                        "district_name"
                    )
                    or "",
                ),
            )

            for mapping in school_mappings:
                lea_id = str(
                    mapping.get("lea_id")
                    or ""
                ).strip()

                indicator = (
                    latest_indicator_by_lea.get(
                        lea_id
                    )
                )

                if not indicator:
                    continue

                district_type = (
                    mapping.get(
                        "district_type"
                    )
                    or "district"
                )

                district_name = (
                    mapping.get(
                        "district_name"
                    )
                    or indicator.get(
                        "district_name"
                    )
                    or lea_id
                )

                school_year = (
                    indicator.get(
                        "school_year"
                    )
                )

                total_students = (
                    indicator.get(
                        "total_students"
                    )
                )

                teacher_fte = (
                    indicator.get(
                        "teacher_fte"
                    )
                )

                student_teacher_ratio = (
                    indicator.get(
                        "student_teacher_ratio"
                    )
                )

                school_metrics = [
                    {
                        "metric_key":
                            "total-students",
                        "metric_name":
                            "Total students",
                        "metric_value":
                            total_students,
                        "metric_text": (
                            f"{int(float(total_students)):,} students"
                            if total_students
                            is not None
                            else "Not available"
                        ),
                        "metric_unit":
                            "students",
                    },
                    {
                        "metric_key":
                            "teacher-fte",
                        "metric_name":
                            "Teacher FTE",
                        "metric_value":
                            teacher_fte,
                        "metric_text": (
                            f"{float(teacher_fte):,.2f} teacher FTE"
                            if teacher_fte
                            is not None
                            else "Not available"
                        ),
                        "metric_unit":
                            "teacher FTE",
                    },
                    {
                        "metric_key":
                            "student-teacher-ratio",
                        "metric_name":
                            "Student-teacher ratio",
                        "metric_value":
                            student_teacher_ratio,
                        "metric_text": (
                            f"{float(student_teacher_ratio):.2f} "
                            "students per teacher FTE"
                            if student_teacher_ratio
                            is not None
                            else "Not available"
                        ),
                        "metric_unit":
                            "students per teacher FTE",
                    },
                ]

                for metric in school_metrics:
                    grouped[
                        "school_context"
                    ].append({
                        "id": (
                            "school-"
                            f"{lea_id}-"
                            f"{metric['metric_key']}"
                        ),
                        "parcel_id": parcel_id,
                        "lea_id": lea_id,
                        "district_type":
                            district_type,
                        "district_geoid":
                            mapping.get(
                                "district_geoid"
                            ),
                        "geography_name":
                            district_name,
                        "geography_level":
                            (
                                f"{district_type}_"
                                "school_district"
                            ),
                        "low_grade":
                            mapping.get(
                                "low_grade"
                            ),
                        "high_grade":
                            mapping.get(
                                "high_grade"
                            ),
                        "tiger_year":
                            mapping.get(
                                "tiger_year"
                            ),
                        "context_category":
                            "school_context",
                        "metric_name":
                            metric[
                                "metric_name"
                            ],
                        "metric_value":
                            metric[
                                "metric_value"
                            ],
                        "metric_text":
                            metric[
                                "metric_text"
                            ],
                        "metric_unit":
                            metric[
                                "metric_unit"
                            ],
                        "source_name":
                            indicator.get(
                                "source_name"
                            )
                            or (
                                "NCES Common "
                                "Core of Data"
                            ),
                        "source_url":
                            indicator.get(
                                "source_url"
                            ),
                        "source_period":
                            school_year,
                        "source_date":
                            indicator.get(
                                "source_date"
                            ),
                        "confidence_level":
                            indicator.get(
                                "confidence_level"
                            )
                            or "context_only",
                        "notes":
                            indicator.get(
                                "notes"
                            ),
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
