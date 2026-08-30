# Public Parcel Value Proxy API

FastAPI backend for the Yellowstone County public parcel value proxy model.

## What this API does

- Loads `price_model.pkl`
- Loads `feature_columns.json`
- Reads parcel rows from Supabase `proxy_training_data`
- Predicts an estimated public parcel value proxy by `parcel_id`
- Returns a value range, confidence, segment notes, and disclaimer

This is **not** an appraisal, not a CMA, and not an MLS sale-price model.

## Required model artifacts

Copy these from your Colab `proxy_model_artifacts.zip` into:

```text
backend/app/ml/artifacts/
```

Required:

```text
price_model.pkl
feature_columns.json
```

Recommended:

```text
model_metrics.json
model_metadata.json
segment_model_metrics.csv
```

## Setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Mac/Linux
# .venv\Scripts\activate  # Windows PowerShell

pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and paste your real Supabase service role key.

## Run

```bash
uvicorn app.main:app --reload
```

Open:

```text
http://localhost:8000/docs
```

## Test endpoints

Health:

```bash
curl http://localhost:8000/health
```

Search parcels:

```bash
curl "http://localhost:8000/api/v1/parcels/search?q=main&limit=5"
```

Predict by parcel:

```bash
curl -X POST "http://localhost:8000/api/v1/predictions/by-parcel" \
  -H "Content-Type: application/json" \
  -d '{"parcel_id":"3062801217010000"}'
```

## Docker

From the project root:

```bash
docker compose up --build
```

Then open:

```text
http://localhost:8000/docs
```
