# Parcel Proxy AI

Parcel Proxy AI is a public-data-based real estate decision-support application. It combines parcel records, market indicators, location context, and machine-learning predictions to provide quick property-value checks.

The platform is designed for real estate professionals, investors, and other users who need accessible property intelligence while researching properties or working in the field.

## Current Status

Parcel Proxy AI is an active MVP with a deployed React frontend, FastAPI backend, Supabase data layer, trained valuation model, and automated economic-data updates.

## Features

- Search public parcel records
- Generate estimated public parcel values
- Display lower and upper prediction bounds
- Identify model confidence and parcel segments
- Incorporate property type, lot size, assessed-value indicators, and location
- Provide market and environmental context
- Collect user-supplied property information
- Update FRED economic indicators automatically
- Clearly distinguish estimates from appraisals and CMAs

## Technology

### Frontend

- React
- Vite
- Axios
- CSS

### Backend

- Python
- FastAPI
- Uvicorn
- scikit-learn and Joblib
- Supabase
- Docker

### Infrastructure

- GitHub
- GitHub Actions
- Render
- Supabase
- Docker Compose

## Repository Structure

```text
Parcel-Proxy-AI/
├── .github/
│   └── workflows/
├── backend/
│   ├── app/
│   ├── pipeline/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── package.json
│   └── index.html
├── docker-compose.yml
└── README.md
```

## Local Backend Setup

Create the backend environment file:

```bash
cp backend/.env.example backend/.env
```

Add the required Supabase service-role key to `backend/.env`. Never commit the completed `.env` file.

Start the backend with Docker Compose:

```bash
docker compose up --build
```

The backend will be available at:

```text
http://localhost:8000
```

FastAPI documentation will be available at:

```text
http://localhost:8000/docs
```

Docker must be installed before using these commands.

## Local Frontend Setup

Open another terminal and install the frontend dependencies:

```bash
cd frontend
npm ci
```

Create `frontend/.env.local` containing:

```env
VITE_API_BASE_URL=http://localhost:8000
```

Start the frontend:

```bash
npm run dev
```

Vite will display the local frontend address in the terminal. If `VITE_API_BASE_URL` is not provided, the frontend defaults to the deployed Render backend.

## Production Backend

The deployed API is available at:

```text
https://parcel-proxy-backend.onrender.com
```

Example prediction request:

```bash
curl -X POST \
  "https://parcel-proxy-backend.onrender.com/api/v1/predictions/by-parcel" \
  -H "Content-Type: application/json" \
  -d '{"parcel_id":"03103235203120000"}'
```

## Automated FRED Updates

The GitHub Actions workflow at `.github/workflows/update-fred-indicators.yml` updates FRED economic indicators in Supabase every Saturday. It can also be run manually from the repository’s Actions page.

The workflow requires these GitHub repository secrets:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## Model Artifacts

The application uses the compressed model at:

```text
backend/app/ml/artifacts/price_model_compressed.joblib
```

The larger uncompressed model is excluded from Git because it exceeds GitHub’s standard file-size limit.

## Important Disclaimer

Parcel Proxy AI provides a public-data-based parcel value proxy for decision support. Its estimates are not licensed appraisals, comparative market analyses, MLS sale-price estimates, or guarantees of market value.

Prediction quality varies according to parcel type, available public records, geographic coverage, model segment, and current market conditions.

## Repository

https://github.com/rickhegenbart/Parcel-Proxy-AI