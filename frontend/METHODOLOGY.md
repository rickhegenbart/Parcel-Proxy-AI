# REPredict Methodology

_Last updated: August 25, 2026_

## 1. What REPredict is

**REPredict** is a public-data real estate parcel analysis tool for Yellowstone County, Montana. It estimates an **Estimated Public Parcel Value** range using public parcel characteristics, location fields, public market indicators, and economic indicators.

REPredict is designed as a decision-support and screening tool. It is not an appraisal product, not a CMA, not an MLS-based pricing tool, and not a guarantee of sale price.

## 2. What the model predicts

The current model predicts a public-record-based value target derived from the parcel dataset:

```text
target_proxy_value = total_value
```

In plain English, the model is trained to estimate the parcel's **public total value** as represented in the source parcel records. This is why the output should be labeled as:

```text
Estimated Public Parcel Value
```

It should not be labeled as:

```text
Appraised value
Market value
MLS sale price
Guaranteed sale price
CMA value
```

## 3. High-level prediction flow

The live application works like this:

```text
User searches address or parcel ID
↓
React frontend sends request to Render backend
↓
FastAPI backend searches Supabase parcel data
↓
User selects a parcel
↓
Backend loads parcel features from Supabase
↓
Backend prepares the same model features used in training
↓
Compressed scikit-learn model predicts estimated public parcel value
↓
Backend applies a segment-based range and confidence label
↓
Frontend displays estimate, range, parcel details, notes, and disclaimer
```

## 4. Current model inputs

The deployed model currently uses 35 input features.

### 4.1 Parcel size and location fields

These features describe the parcel's physical size, approximate location, and tax-year context:

```text
gis_acres
total_acres
lot_size_sqft
latitude
longitude
tax_year
```

These help the model learn how parcel size and location relate to public parcel value.

### 4.2 Housing Price Index indicators

These features provide broader housing-market context:

```text
hpi_index
hpi_yoy_change_pct
hpi_period_change_pct
```

They help the model account for market direction and price-index movement.

### 4.3 Mortgage-rate indicators

These features describe the financing environment:

```text
mortgage_rate
mortgage_rate_4week_avg
mortgage_rate_13week_avg
mortgage_rate_change_52week
```

They help represent the cost-of-borrowing environment at prediction time.

### 4.4 Unemployment and economic pressure indicators

These features describe general economic pressure:

```text
unemployment_rate
unemployment_rate_3month_avg
unemployment_rate_12month_avg
unemployment_pressure_score
```

They provide economic context that can influence real estate demand and value patterns.

### 4.5 Realtor county-market indicators

These features describe the broader county-level listing market:

```text
median_listing_price
active_listing_count
median_days_on_market
new_listing_count
pending_listing_count
price_reduced_count
median_listing_price_per_square_foot
price_reduction_share
pending_to_active_ratio
market_heat_score
```

These help the model account for market conditions such as supply, demand, days on market, price reductions, and listing-price trends.

### 4.6 Categorical location and property-type fields

These features help the model distinguish between property types, parcel groups, and local geographies:

```text
site_city
site_state
site_zip_code
county_name
property_type
property_type_group
model_segment
is_residential
```

These are important because different parcel segments behave differently. For example, a vacant land parcel should not be treated the same as a condominium or an improved commercial-style parcel.

## 5. Important features the model does not currently use

The current model does **not** use the following property-specific details:

```text
Bedrooms
Bathrooms
Finished square footage
Garage size
Year built
Interior condition
Renovation status
Photos
MLS comparable sales
Actual sale history
Current listing status
Seller motivation
Buyer demand for the specific property
Inspection issues
School district
Zoning
Floodplain status
```

The model also does **not** currently use these public valuation components as input features:

```text
total_value
total_land_value
total_building_value
```

This is important: the model is not simply copying the public total value back into the output. It is estimating public parcel value from parcel size, location, property classification, and market/economic context.

## 6. Model segments

Parcels are grouped into modeling segments to improve interpretation and confidence handling. Current segments include:

```text
residential
land
improved_unknown
commercial_or_income
industrial
agricultural
```

The `model_segment` helps determine both the prediction and the confidence/range behavior.

### Segment meaning

| Segment | Meaning |
|---|---|
| `residential` | Residential-like records such as condominiums and townhouses, plus clearly residential property groups when available. |
| `land` | Vacant land parcels. |
| `improved_unknown` | Parcels with improvements/buildings where the public source does not clearly identify residential versus commercial use. |
| `commercial_or_income` | Commercial, income, multifamily, mixed-use, or mobile/RV park style parcels. |
| `industrial` | Industrial property records. |
| `agricultural` | Agricultural or farmstead-related records, when present. |

## 7. Confidence and prediction range

The model returns a central estimate plus a low-high range.

Example:

```text
Estimated Public Parcel Value: $330,593
Estimated Range: $247,945 – $413,242
Confidence: medium-low
Range Margin: 25%
```

The range is not a formal appraisal confidence interval. It is a conservative decision-support range applied by the backend based on the parcel's model segment and expected reliability.

Typical logic:

| Segment | Typical range behavior |
|---|---|
| `residential` | Narrower range, higher reliability. |
| `improved_unknown` | Moderate/wider range because use type is less clear. |
| `land` | Wider range because land values are harder to model from limited public features. |
| `commercial_or_income` | Wider range because public features may not capture income, tenancy, cap rates, or condition. |
| `industrial` | Wider range because sample size is smaller and parcels may be specialized. |
| `agricultural` | Wider range because public parcel data may not fully capture land productivity, water rights, improvements, or special use. |

## 8. Known model performance from prototype training

The current prototype model was trained on the broad all-parcel dataset for Yellowstone County. The training results showed stronger performance for residential-like parcels and weaker performance for land, commercial/income, and industrial parcels.

Approximate prototype evaluation results:

```text
Overall MAE: about $90,000
Median absolute error: about $35,000
Overall R²: about 0.50
Within 20%: about 55%
```

Segment-level interpretation:

| Segment | Interpretation |
|---|---|
| Residential-like | Most reliable current segment. |
| Improved unknown | Usable with caution. |
| Land | Weaker percentage accuracy; should show caution. |
| Commercial/income | Limited public inputs; should show caution. |
| Industrial | Small sample size; not highly reliable yet. |

These prototype metrics are good enough for an MVP screening tool, but not enough to support appraisal-style claims.

## 9. Why predictions can differ from actual sale price

REPredict does not see private or highly property-specific information. A parcel's real transaction price can be affected by:

```text
Interior condition
Renovations
Deferred maintenance
Functional layout
Photos and curb appeal
Buyer/seller motivation
Financing terms
Inspection outcomes
Appraisal conditions
MLS exposure
Current inventory competition
Off-market terms
```

Because the model does not see these factors, its output should be interpreted as a public-data proxy range, not a sale-price forecast.

## 10. Recommended product language

Use:

```text
Estimated Public Parcel Value
Public Parcel Value Proxy
Decision-support estimate
Public-data-based estimate range
```

Avoid:

```text
Appraisal
CMA
Guaranteed value
True market value
MLS value
Sale price prediction
```

## 11. Required disclaimer

Use this disclaimer on result pages and methodology pages:

```text
This estimate is a public-data-based parcel value proxy. It is based on public parcel records, assessed-value indicators, property type, lot size, location, and market context. It is not an appraisal, not a CMA, not an MLS sale-price estimate, and should be used for decision support only.
```

## 12. Recommended next improvements

The highest-value next model improvements are:

```text
Add building square footage, bedrooms, bathrooms, and year built if public sources provide them.
Train separate models by segment.
Improve the vacant land model separately.
Add zoning and land-use features.
Add floodplain and environmental-risk indicators.
Add school district or neighborhood proxy features.
Add distance-to-downtown or distance-to-services features.
Improve confidence scoring using empirical error by segment and value band.
Create a retraining workflow and model-version log.
```

## 13. Summary

REPredict estimates public parcel value ranges using public parcel size, location, property classification, county market indicators, housing price index data, mortgage-rate conditions, and unemployment/economic indicators.

It is most useful as an early-stage screening and comparison tool. It should not be used as a substitute for an appraisal, CMA, MLS comparable-sales analysis, inspection, or professional valuation judgment.
