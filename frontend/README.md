# REPredict

**REPredict** is a public-data real estate parcel analysis app for Yellowstone County, Montana. It lets a user search for a parcel by address, city, ZIP code, or parcel ID, then generates an **Estimated Public Parcel Value** range using public parcel records, property characteristics, location, and market/economic indicators.

Live architecture:

```text
React frontend
↓
Render FastAPI backend
↓
Supabase PostgreSQL
↓
Compressed scikit-learn model
```

> REPredict is a decision-support tool. It is not an appraisal, not a CMA, not an MLS sale-price estimate, and not a guarantee of market value.

---

## Project status

Current MVP status:

```text
Frontend: live on Render Static Site
Backend: live on Render Web Service
Database: Supabase
Model: compressed scikit-learn artifact loaded by FastAPI
Parcel search: working
Address search: working
Prediction endpoint: working
```

Known live frontend:

```text
https://repredict.onrender.com
```

Known live backend:

```text
https://parcel-proxy-backend.onrender.com
```

Backend docs:

```text
https://parcel-proxy-backend.onrender.com/docs
```

---

## What the app does

A user can:

1. Search for parcels by address, partial address, city, ZIP code, or parcel ID.
2. Select a parcel from search results.
3. Request an estimated public parcel value.
4. View:
   - Estimated Public Parcel Value
   - Low-high value range
   - Confidence label
   - Parcel details
   - Model segment
   - Model notes
   - Disclaimer

Example parcel search:

```text
534 S BILLINGS BLVD
```

Example parcel ID:

```text
03103235203120000
```

---

## What the app does not do

REPredict does not currently use:

```text
MLS comparable sales
Private sale terms
Interior condition
Photos
Bedrooms and bathrooms
Finished square footage
Renovation quality
Inspection issues
Current listing status
Seller motivation
Buyer-specific demand
```

It should not be described as:

```text
An appraisal
A CMA
A sale-price predictor
A guaranteed market value tool
An MLS valuation product
```

---

## Repository structure

The project currently uses two GitHub repositories:

```text
parcel-proxy-backend
parcel-proxy-frontend
```

### Backend repo structure

```text
backend/
  app/
    main.py
    config.py
    schemas.py
    supabase_client.py
    model_service.py
    ml/
      artifacts/
        feature_columns.json
        price_model_compressed.joblib
        model_metrics.json
        model_metadata.json
        segment_model_metrics.csv
  Dockerfile
  requirements.txt
  .gitignore
```

### Frontend repo structure

```text
frontend/
  index.html
  package.json
  package-lock.json
  src/
    App.jsx
    main.jsx
    styles.css
  .gitignore
```

---

## Backend environment variables

The backend runs on Render and requires these environment variables:

```env
SUPABASE_URL=https://zfwtwdjnbdsieibkfbvv.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server-side Supabase service role key>
APP_ENV=production
APP_NAME=Public Parcel Value Proxy API
MODEL_PATH=app/ml/artifacts/price_model_compressed.joblib
FEATURE_COLUMNS_PATH=app/ml/artifacts/feature_columns.json
MODEL_METRICS_PATH=app/ml/artifacts/model_metrics.json
MODEL_METADATA_PATH=app/ml/artifacts/model_metadata.json
SEGMENT_METRICS_PATH=app/ml/artifacts/segment_model_metrics.csv
```

Optional CORS hardening variable:

```env
ALLOWED_ORIGINS=https://repredict.onrender.com,http://localhost:5173,http://100.115.92.194:5173
```

Security note:

```text
Never commit SUPABASE_SERVICE_ROLE_KEY to GitHub.
Never expose SUPABASE_SERVICE_ROLE_KEY in frontend code.
Only store it in trusted server-side environments such as Render backend environment variables.
```

---

## Frontend environment variables

The frontend requires:

```env
VITE_API_BASE_URL=https://parcel-proxy-backend.onrender.com
```

This variable is used by the React app to call the live FastAPI backend.

---

## Backend API endpoints

### Health check

```http
GET /health
```

Expected successful response:

```json
{
  "status": "Public Parcel Value Proxy API running in production",
  "model_loaded": true,
  "feature_count": 35,
  "supabase_configured": true
}
```

### Search parcels

```http
GET /api/v1/parcels/search?q=<search_term>&limit=10
```

Example:

```text
https://parcel-proxy-backend.onrender.com/api/v1/parcels/search?q=534%20S%20BILLINGS%20BLVD&limit=10
```

### Get parcel by ID

```http
GET /api/v1/parcels/{parcel_id}
```

### Predict by parcel

```http
POST /api/v1/predictions/by-parcel
```

Example body:

```json
{
  "parcel_id": "03103235203120000"
}
```

Example successful response includes:

```json
{
  "prediction": {
    "estimated_public_parcel_value": 330593.22,
    "lower_bound": 247944.92,
    "upper_bound": 413241.53,
    "range_margin_pct": 25,
    "confidence": "medium-low"
  },
  "parcel": {
    "parcel_id": "03103235203120000",
    "address_line_1": "2226 PATRICIA LN",
    "site_city": "Billings",
    "site_state": "MT",
    "site_zip_code": "59102"
  }
}
```

---

## Local frontend development

From the frontend folder:

```bash
cd ~/REPredictworking/frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open the network URL printed by Vite. On Chromebook Linux, it may look similar to:

```text
http://100.115.92.194:5173/
```

---

## Local backend development

From the backend folder:

```bash
cd ~/REPredictworking/fastapi_parcel_proxy_backend/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Local backend docs:

```text
http://localhost:8000/docs
```

---

## Deployment notes

### Backend on Render

Use Render Web Service with Docker.

Recommended settings:

```text
Runtime: Docker
Branch: main
Root Directory: blank, if repo root is already backend
Dockerfile: Dockerfile
```

Docker command is handled by the Dockerfile:

```dockerfile
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}
```

### Frontend on Render

Use Render Static Site.

Recommended settings:

```text
Build Command: npm install && npm run build
Publish Directory: dist
Environment Variable: VITE_API_BASE_URL=https://parcel-proxy-backend.onrender.com
```

---

## Model methodology summary

The model predicts an **Estimated Public Parcel Value** using 35 features, including:

```text
Parcel size
Latitude/longitude
Tax year
Housing Price Index indicators
Mortgage-rate indicators
Unemployment indicators
Realtor county-market indicators
City/state/ZIP/county
Property type
Property type group
Model segment
Residential flag
```

The model does not currently use MLS data, photos, interior condition, bedrooms, bathrooms, finished square footage, or actual sale terms.

For full details, see:

```text
METHODOLOGY.md
```

---

## Required disclaimer

Use this disclaimer in the app and public documentation:

```text
This estimate is a public-data-based parcel value proxy. It is based on public parcel records, assessed-value indicators, property type, lot size, location, and market context. It is not an appraisal, not a CMA, not an MLS sale-price estimate, and should be used for decision support only.
```

---

## Security checklist

Before public promotion:

```text
Rotate any Supabase service role key that was exposed outside Render.
Confirm .env files are ignored by Git.
Confirm GitHub does not contain service keys.
Restrict backend CORS to https://repredict.onrender.com.
Keep service role key only in Render backend environment variables.
Do not expose service role key in React frontend.
```

---

## Recommended next improvements

Short-term product improvements:

```text
Add a New Search button.
Improve loading state and error messages.
Add a Methodology page.
Add an About page.
Add confidence explanations.
Improve mobile spacing.
Add custom domain.
```

Model improvements:

```text
Train separate models by parcel segment.
Improve land-specific modeling.
Add building details if available.
Add zoning and floodplain data.
Add neighborhood/location engineering.
Add empirical confidence scoring by segment and value band.
Track model versions and retraining dates.
```

---

## Suggested app language

Use:

```text
REPredict
Estimated Public Parcel Value
Public Parcel Value Proxy
Decision-support estimate
```

Avoid:

```text
Appraisal
CMA
Guaranteed market value
Sale price prediction
MLS estimate
```
