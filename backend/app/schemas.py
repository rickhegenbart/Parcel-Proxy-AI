from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ParcelPredictionRequest(BaseModel):
    parcel_id: str = Field(..., description="Yellowstone County parcel_id")


class ManualPredictionRequest(BaseModel):
    features: Dict[str, Any] = Field(
        ...,
        description="Dictionary containing the same raw feature names as feature_columns.json"
    )


class PredictionRange(BaseModel):
    estimated_public_parcel_value: float
    lower_bound: float
    upper_bound: float
    range_margin_pct: float
    confidence: str


class ParcelSummary(BaseModel):
    parcel_id: Optional[str] = None
    property_id: Optional[str] = None
    address_line_1: Optional[str] = None
    site_city: Optional[str] = None
    site_state: Optional[str] = None
    site_zip_code: Optional[str] = None
    property_type: Optional[str] = None
    property_type_group: Optional[str] = None
    model_segment: Optional[str] = None
    lot_size_sqft: Optional[float] = None
    total_value: Optional[float] = None


class DataFreshness(BaseModel):
    source: str = "FRED"
    mortgage_indicator_date: Optional[str] = None
    unemployment_indicator_date: Optional[str] = None
    fresh_values_applied: bool = False
    warnings: List[str] = Field(default_factory=list)


class PredictionResponse(BaseModel):
    prediction: PredictionRange
    parcel: Optional[ParcelSummary] = None
    model_segment: Optional[str] = None
    model_notes: List[str]
    disclaimer: str
    data_freshness: Optional[DataFreshness] = None


class SearchResult(BaseModel):
    parcel_id: Optional[str] = None
    property_id: Optional[str] = None
    address_line_1: Optional[str] = None
    site_city: Optional[str] = None
    site_zip_code: Optional[str] = None
    property_type_group: Optional[str] = None
    model_segment: Optional[str] = None
    total_value: Optional[float] = None


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    feature_count: int
    supabase_configured: bool
