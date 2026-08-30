from typing import Any, Dict, List, Optional
from supabase import create_client, Client

from .config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


_client: Optional[Client] = None


def get_supabase() -> Client:
    global _client

    if _client is not None:
        return _client

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in backend/.env"
        )

    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def supabase_is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and "your-service-role-key" not in SUPABASE_SERVICE_ROLE_KEY)


def fetch_proxy_training_row_by_parcel_id(parcel_id: str) -> Optional[Dict[str, Any]]:
    supabase = get_supabase()

    response = (
        supabase
        .table("proxy_training_data")
        .select("*")
        .eq("parcel_id", parcel_id)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    return rows[0] if rows else None


def search_parcels(q: str, limit: int = 10):
    """
    Search parcels by parcel ID, property ID, address, city, ZIP, or partial address.
    Handles full address strings better by falling back to house-number search.
    """
    supabase = get_supabase()

    query = (q or "").strip()
    if not query:
        return []

    select_cols = """
        parcel_id,
        property_id,
        address_line_1,
        site_city,
        site_state,
        site_zip_code,
        property_type,
        property_type_group,
        model_segment,
        lot_size_sqft,
        total_value
    """

    # First pass: normal broad search
    try:
        response = (
            supabase
            .table("proxy_training_data")
            .select(select_cols)
            .or_(
                f"parcel_id.ilike.%{query}%,"
                f"property_id.ilike.%{query}%,"
                f"address_line_1.ilike.%{query}%,"
                f"site_city.ilike.%{query}%,"
                f"site_zip_code.ilike.%{query}%"
            )
            .limit(limit)
            .execute()
        )

        if response.data:
            return response.data

    except Exception:
        pass

    # Second pass: if user typed a full address, search by house number
    tokens = (
        query.replace(",", " ")
        .replace(".", " ")
        .replace("#", " ")
        .upper()
        .split()
    )

    number_tokens = [t for t in tokens if t.isdigit()]

    if number_tokens:
        house_number = number_tokens[0]

        response = (
            supabase
            .table("proxy_training_data")
            .select(select_cols)
            .ilike("address_line_1", f"%{house_number}%")
            .limit(50)
            .execute()
        )

        rows = response.data or []

        ignore_tokens = {"MT", "MONTANA", "BILLINGS", "YELLOWSTONE", "COUNTY"}

        useful_tokens = [
            t for t in tokens
            if t not in ignore_tokens and not (len(t) == 5 and t.isdigit())
        ]

        scored = []
        for row in rows:
            address = (row.get("address_line_1") or "").upper()
            score = sum(1 for token in useful_tokens if token in address)
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [row for score, row in scored[:limit]]

    return []

def fetch_latest_fred_features() -> Dict[str, Any]:
    """
    Load the latest validated FRED features for live predictions.

    If either query fails, the prediction can fall back to the values
    already stored in proxy_training_data.
    """
    supabase = get_supabase()
    features: Dict[str, Any] = {}
    freshness: Dict[str, Any] = {
        "source": "FRED",
        "mortgage_indicator_date": None,
        "unemployment_indicator_date": None,
        "fresh_values_applied": False,
        "warnings": [],
    }

    try:
        response = (
            supabase
            .table("mortgage_rate_indicators")
            .select(
                "indicator_date,"
                "mortgage_rate,"
                "mortgage_rate_4week_avg,"
                "mortgage_rate_13week_avg,"
                "mortgage_rate_change_52week"
            )
            .order("indicator_date", desc=True)
            .limit(1)
            .execute()
        )

        if response.data:
            row = response.data[0]
            freshness["mortgage_indicator_date"] = row.get(
                "indicator_date"
            )

            for field in (
                "mortgage_rate",
                "mortgage_rate_4week_avg",
                "mortgage_rate_13week_avg",
                "mortgage_rate_change_52week",
            ):
                if row.get(field) is not None:
                    features[field] = row[field]
        else:
            freshness["warnings"].append(
                "No mortgage indicator row was available."
            )

    except Exception:
        freshness["warnings"].append(
            "Latest mortgage indicators could not be loaded."
        )

    try:
        response = (
            supabase
            .table("unemployment_rate_indicators")
            .select(
                "indicator_date,"
                "unemployment_rate,"
                "unemployment_rate_3month_avg,"
                "unemployment_rate_12month_avg,"
                "unemployment_pressure_score"
            )
            .order("indicator_date", desc=True)
            .limit(1)
            .execute()
        )

        if response.data:
            row = response.data[0]
            freshness["unemployment_indicator_date"] = row.get(
                "indicator_date"
            )

            for field in (
                "unemployment_rate",
                "unemployment_rate_3month_avg",
                "unemployment_rate_12month_avg",
                "unemployment_pressure_score",
            ):
                if row.get(field) is not None:
                    features[field] = row[field]
        else:
            freshness["warnings"].append(
                "No unemployment indicator row was available."
            )

    except Exception:
        freshness["warnings"].append(
            "Latest unemployment indicators could not be loaded."
        )

    freshness["fresh_values_applied"] = bool(features)

    return {
        "features": features,
        "freshness": freshness,
    }




def get_supabase_client():
    """
    Compatibility helper used by the feedback endpoint.
    Returns the configured Supabase client.
    """
    global _client

    if _client is not None:
        return _client

    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )

    _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client
