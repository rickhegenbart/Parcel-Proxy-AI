import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

APP_NAME = os.getenv("APP_NAME", "Public Parcel Value Proxy API")
APP_ENV = os.getenv("APP_ENV", "development")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

MODEL_PATH = Path(os.getenv("MODEL_PATH", "app/ml/artifacts/price_model_compressed.joblib"))
FEATURE_COLUMNS_PATH = Path(os.getenv("FEATURE_COLUMNS_PATH", "app/ml/artifacts/feature_columns.json"))
MODEL_METRICS_PATH = Path(os.getenv("MODEL_METRICS_PATH", "app/ml/artifacts/model_metrics.json"))
MODEL_METADATA_PATH = Path(os.getenv("MODEL_METADATA_PATH", "app/ml/artifacts/model_metadata.json"))
SEGMENT_METRICS_PATH = Path(os.getenv("SEGMENT_METRICS_PATH", "app/ml/artifacts/segment_model_metrics.csv"))

DISCLAIMER = (
    "This estimate is a public-data-based parcel value proxy. It is based on public parcel records, "
    "assessed-value indicators, property type, lot size, location, and market context. It is not an "
    "appraisal, not a CMA, not an MLS sale-price estimate, and should be used for decision support only."
)
