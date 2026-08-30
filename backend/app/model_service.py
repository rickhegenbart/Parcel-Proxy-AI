from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
import pandas as pd

from .config import (
    DISCLAIMER,
    FEATURE_COLUMNS_PATH,
    MODEL_METADATA_PATH,
    MODEL_METRICS_PATH,
    MODEL_PATH,
    SEGMENT_METRICS_PATH,
)


class ParcelValueModelService:
    def __init__(self) -> None:
        self.model = None
        self.feature_columns: List[str] = []
        self.model_metrics: Dict[str, Any] = {}
        self.model_metadata: Dict[str, Any] = {}
        self.segment_metrics: pd.DataFrame = pd.DataFrame()
        self.load()

    def load(self) -> None:
        if MODEL_PATH.exists():
            self.model = joblib.load(MODEL_PATH)

        if FEATURE_COLUMNS_PATH.exists():
            self.feature_columns = json.loads(FEATURE_COLUMNS_PATH.read_text(encoding="utf-8"))

        if MODEL_METRICS_PATH.exists():
            self.model_metrics = json.loads(MODEL_METRICS_PATH.read_text(encoding="utf-8"))

        if MODEL_METADATA_PATH.exists():
            self.model_metadata = json.loads(MODEL_METADATA_PATH.read_text(encoding="utf-8"))

        if SEGMENT_METRICS_PATH.exists():
            self.segment_metrics = pd.read_csv(SEGMENT_METRICS_PATH)

    @property
    def is_loaded(self) -> bool:
        return self.model is not None and bool(self.feature_columns)

    def _build_model_frame(self, features: Dict[str, Any]) -> pd.DataFrame:
        if not self.is_loaded:
            raise RuntimeError(
                "Model artifacts are missing. Add price_model.pkl and feature_columns.json to app/ml/artifacts/"
            )

        row = pd.DataFrame([features])

        for col in self.feature_columns:
            if col not in row.columns:
                row[col] = np.nan

        return row[self.feature_columns]

    def predict(self, features: Dict[str, Any]) -> Dict[str, Any]:
        model_frame = self._build_model_frame(features)
        estimate = float(self.model.predict(model_frame)[0])

        model_segment = features.get("model_segment")
        range_margin = self._range_margin_for_segment(model_segment)
        confidence = self._confidence_for_margin(range_margin, model_segment)

        lower = max(0.0, estimate * (1 - range_margin))
        upper = estimate * (1 + range_margin)

        notes = self._notes_for_segment(model_segment)

        return {
            "prediction": {
                "estimated_public_parcel_value": round(estimate, 2),
                "lower_bound": round(lower, 2),
                "upper_bound": round(upper, 2),
                "range_margin_pct": round(range_margin * 100, 2),
                "confidence": confidence,
            },
            "model_segment": model_segment,
            "model_notes": notes,
            "disclaimer": DISCLAIMER,
        }

    def _range_margin_for_segment(self, model_segment: Optional[str]) -> float:
        """
        Conservative V1 range logic:
        - Start from model's overall MAPE if available, but clamp it.
        - Override/expand for weaker segments.
        """
        default_margin = 0.25

        try:
            metrics = self.model_metrics.get("model_metrics", {})
            mape_pct = float(metrics.get("mape_pct", 25))
            default_margin = max(0.12, min(0.35, mape_pct / 100))
        except Exception:
            default_margin = 0.25

        segment_overrides = {
            "residential": 0.18,
            "improved_unknown": 0.25,
            "land": 0.35,
            "commercial_or_income": 0.40,
            "industrial": 0.50,
            "agricultural": 0.45,
        }

        if model_segment in segment_overrides:
            return segment_overrides[model_segment]

        return default_margin

    def _confidence_for_margin(self, margin: float, model_segment: Optional[str]) -> str:
        if model_segment in {"industrial", "agricultural", "commercial_or_income"}:
            return "low"

        if margin <= 0.20:
            return "medium"

        if margin <= 0.30:
            return "medium-low"

        return "low"

    def _notes_for_segment(self, model_segment: Optional[str]) -> List[str]:
        notes = [
            "This is a public parcel value proxy, not a sale-price appraisal.",
            "Prediction quality varies by parcel segment.",
        ]

        if model_segment == "land":
            notes.append("Land parcels are valued differently from improved parcels; treat the range as broad.")
        elif model_segment == "improved_unknown":
            notes.append("Improved Property means a parcel has improvements/buildings, but the public source does not clearly identify residential versus commercial use.")
        elif model_segment == "residential":
            notes.append("Residential-labeled parcels generally have stronger model reliability than land or industrial parcels.")
        elif model_segment == "commercial_or_income":
            notes.append("Commercial/income parcels have limited training examples and may need a separate future model.")
        elif model_segment == "industrial":
            notes.append("Industrial parcels have very limited training examples; use extra caution.")

        return notes

    def parcel_summary(self, row: Dict[str, Any]) -> Dict[str, Any]:
        fields = [
            "parcel_id",
            "property_id",
            "address_line_1",
            "site_city",
            "site_state",
            "site_zip_code",
            "property_type",
            "property_type_group",
            "model_segment",
            "lot_size_sqft",
            "total_value",
        ]

        return {field: row.get(field) for field in fields}


model_service = ParcelValueModelService()
