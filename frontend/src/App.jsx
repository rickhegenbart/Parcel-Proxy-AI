import { useEffect, useState } from "react";
import axios from "axios";


function trackEvent(name, props = {}) {
  const eventNameMap = {
    "Search Submitted": "search_submitted",
    "Parcel Selected": "parcel_selected",
    "Feedback Submitted": "feedback_submitted",
    "Admin Dashboard Opened": "admin_dashboard_opened"
  };

  const eventName = eventNameMap[name] || name;

  if (window.plausible) {
    window.plausible(name, { props });
  }

  fetch(`${API_BASE_URL}/api/v1/events`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      event_name: eventName,
      page_path: window.location.pathname + window.location.search,
      referrer: document.referrer || null,
      metadata: props
    })
  }).catch(() => {
    // Do not interrupt the user experience if analytics fails.
  });
}

const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || "https://parcel-proxy-backend.onrender.com";

const emptyPropertyDetails = {
  bedrooms: "",
  bathrooms: "",
  finishedSqft: "",
  garageSize: "",
  yearBuilt: "",
  renovationStatus: "",
  interiorCondition: "",
  recentSalePrice: "",
  listingStatus: "",
  sellerMotivation: "",
  mlsComps: "",
  photoNames: []
};

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "N/A";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0
  }).format(Number(value));
}


function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function toNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function computeUserAdjustedEstimate(selectedResult, details) {
  if (!selectedResult?.prediction?.estimated_public_parcel_value) {
    return null;
  }

  const baseline = Number(selectedResult.prediction.estimated_public_parcel_value);
  const baseMargin = Number(selectedResult.prediction.range_margin_pct || 25);

  let percentAdjustment = 0;
  const adjustments = [];

  function addAdjustment(label, pct) {
    if (!pct) return;
    percentAdjustment += pct;
    adjustments.push({ label, pct });
  }

  const bedrooms = toNumber(details.bedrooms);
  if (bedrooms !== null) {
    if (bedrooms <= 1) addAdjustment("Bedroom count below typical residential range", -3);
    else if (bedrooms === 2) addAdjustment("Two-bedroom layout", -1);
    else if (bedrooms === 4) addAdjustment("Four-bedroom layout", 2);
    else if (bedrooms >= 5) addAdjustment("Five or more bedrooms", 4);
  }

  const bathrooms = toNumber(details.bathrooms);
  if (bathrooms !== null) {
    if (bathrooms <= 1) addAdjustment("One or fewer bathrooms", -4);
    else if (bathrooms >= 2.5 && bathrooms < 3.5) addAdjustment("Additional bathroom utility", 3);
    else if (bathrooms >= 3.5) addAdjustment("High bathroom count", 5);
  }

  const sqft = toNumber(details.finishedSqft);
  if (sqft !== null) {
    if (sqft < 800) addAdjustment("Small finished square footage", -5);
    else if (sqft < 1200) addAdjustment("Below-average finished square footage", -2);
    else if (sqft >= 2000 && sqft < 3000) addAdjustment("Above-average finished square footage", 4);
    else if (sqft >= 3000) addAdjustment("Large finished square footage", 8);
  }

  if (details.garageSize === "none") addAdjustment("No garage", -3);
  if (details.garageSize === "2-car") addAdjustment("Two-car garage", 3);
  if (details.garageSize === "3-car") addAdjustment("Three-car garage", 5);
  if (details.garageSize === "4-plus-car") addAdjustment("Four-plus-car garage", 7);

  const yearBuilt = toNumber(details.yearBuilt);
  if (yearBuilt !== null) {
    const currentYear = new Date().getFullYear();
    const age = currentYear - yearBuilt;

    if (age <= 10) addAdjustment("Newer construction", 5);
    else if (age <= 25) addAdjustment("Relatively newer construction", 2);
    else if (age >= 75) addAdjustment("Older construction", -5);
  }

  if (details.renovationStatus === "recently-renovated") {
    addAdjustment("Recently renovated", 10);
  } else if (details.renovationStatus === "partially-updated") {
    addAdjustment("Partially updated", 4);
  } else if (details.renovationStatus === "dated") {
    addAdjustment("Dated / needs updates", -5);
  } else if (details.renovationStatus === "major-rehab") {
    addAdjustment("Major rehab needed", -15);
  }

  if (details.interiorCondition === "excellent") {
    addAdjustment("Excellent interior condition", 8);
  } else if (details.interiorCondition === "good") {
    addAdjustment("Good interior condition", 4);
  } else if (details.interiorCondition === "poor") {
    addAdjustment("Poor interior condition", -10);
  }

  if (details.listingStatus === "pending") {
    addAdjustment("Pending listing status", 2);
  } else if (details.listingStatus === "expired") {
    addAdjustment("Expired or withdrawn listing status", -3);
  }

  if (details.sellerMotivation === "low") {
    addAdjustment("Low seller motivation", 3);
  } else if (details.sellerMotivation === "high") {
    addAdjustment("High seller motivation", -3);
  } else if (details.sellerMotivation === "distressed") {
    addAdjustment("Distressed or urgent seller scenario", -8);
  }

  const clampedPercentAdjustment = clamp(percentAdjustment, -25, 25);
  let adjustedValue = baseline * (1 + clampedPercentAdjustment / 100);

  const recentSalePrice = toNumber(details.recentSalePrice);
  if (recentSalePrice !== null) {
    adjustedValue = adjustedValue * 0.7 + recentSalePrice * 0.3;
    adjustments.push({
      label: "Recent sale price blended as user-provided context",
      pct: null
    });
  }

  const scenarioMargin = clamp(baseMargin + 10, 20, 55);

  return {
    baseline,
    adjustedValue,
    lowerBound: adjustedValue * (1 - scenarioMargin / 100),
    upperBound: adjustedValue * (1 + scenarioMargin / 100),
    percentAdjustment: clampedPercentAdjustment,
    scenarioMargin,
    adjustments,
    usesRecentSalePrice: recentSalePrice !== null,
    hasPhotos: details.photoNames && details.photoNames.length > 0,
    hasMlsNotes: Boolean(details.mlsComps && details.mlsComps.trim())
  };
}


function MainApp() {
  const [query, setQuery] = useState("");
  const [parcels, setParcels] = useState([]);
  const [selectedResult, setSelectedResult] = useState(null);
  const [parcelContext, setParcelContext] = useState(null);
  const [propertyDetails, setPropertyDetails] = useState(emptyPropertyDetails);
  const [loadingSearch, setLoadingSearch] = useState(false);
  const [loadingPrediction, setLoadingPrediction] = useState(false);
  const [error, setError] = useState("");
  const [feedbackRating, setFeedbackRating] = useState("");
  const [feedbackComment, setFeedbackComment] = useState("");
  const [feedbackStatus, setFeedbackStatus] = useState("");
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);


  useEffect(() => {
    trackEvent("page_view", {
      page: "main"
    });
  }, []);

  async function searchParcels(event) {
    event.preventDefault();
    setError("");
    setSelectedResult(null);
    setParcelContext(null);
    setPropertyDetails(emptyPropertyDetails);
    setParcels([]);

    if (!query.trim()) {
      setError("Enter an address, city, or parcel ID.");
      return;
    }

    const normalizedQuery = query.trim();
    const searchType = /^\d{10,}$/.test(normalizedQuery)
      ? "parcel_id"
      : "address_or_city";

    trackEvent("Search Submitted", {
      search_type: searchType
    });

    setLoadingSearch(true);

    try {
      const response = await fetch(
        `${API_BASE_URL}/api/v1/parcels/search?q=${encodeURIComponent(
          query
        )}&limit=10`
      );

      if (!response.ok) {
        throw new Error("Parcel search failed.");
      }

      const data = await response.json();
      setParcels(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingSearch(false);
    }
  }

  
  async function loadParcelContext(parcelId) {
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/parcels/${parcelId}/context`);

      if (!response.ok) {
        throw new Error("Context lookup failed.");
      }

      const data = await response.json();
      setParcelContext(data);
    } catch (err) {
      console.warn("Parcel context unavailable:", err);
      setParcelContext(null);
    }
  }

async function getPrediction(parcelId) {
    trackEvent("Parcel Selected", {
      parcel_id: parcelId
    });

    setError("");
    setLoadingPrediction(true);
    setSelectedResult(null);
    setPropertyDetails(emptyPropertyDetails);

    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/predictions/by-parcel`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          parcel_id: parcelId
        })
      });

      if (!response.ok) {
        throw new Error("Prediction failed.");
      }

      const data = await response.json();
      setSelectedResult(data);
      loadParcelContext(parcelId);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoadingPrediction(false);
    }
  }

  function updatePropertyDetail(field, value) {
    setPropertyDetails((current) => ({
      ...current,
      [field]: value
    }));
  }

  function handlePhotoChange(event) {
    const files = Array.from(event.target.files || []);
    setPropertyDetails((current) => ({
      ...current,
      photoNames: files.map((file) => file.name)
    }));
  }

  const adjustedScenario = selectedResult ? computeUserAdjustedEstimate(selectedResult, propertyDetails) : null;


  async function submitFeedback(event) {
    event.preventDefault();

    if (!feedbackRating || !selectedResult) {
      setFeedbackStatus("Please choose whether the estimate felt too low, about right, or too high.");
      return;
    }

    setFeedbackSubmitting(true);
    setFeedbackStatus("");

    const parcel = selectedResult?.parcel || selectedResult;

    const baselineEstimate =
      selectedResult?.prediction?.estimated_public_parcel_value ?? null;

    const scenarioEstimate =
      adjustedScenario?.adjustedValue ?? null;

    const payload = {
      parcel_id: parcel?.parcel_id || null,
      property_id: parcel?.property_id || null,
      address_line_1: parcel?.address_line_1 || null,
      rating: feedbackRating,
      comment: feedbackComment || null,
      baseline_estimate: baselineEstimate,
      adjusted_estimate: scenarioEstimate,
    };

    try {
      await axios.post(`${API_BASE_URL}/api/v1/feedback`, payload);

      trackEvent("Feedback Submitted", {
        parcel_id: parcel?.parcel_id || null,
        rating: feedbackRating
      });

      setFeedbackStatus("Thank you — your feedback was saved.");
      setFeedbackComment("");
    } catch (err) {
      console.error("Feedback save error:", err);

      const detail =
        err?.response?.data?.detail ||
        err?.response?.data?.message ||
        err?.message ||
        "Unknown error";

      setFeedbackStatus(`Sorry, your feedback could not be saved: ${detail}`);
    } finally {
      setFeedbackSubmitting(false);
    }
  }


  function scrollToSection(sectionId) {
    const target = document.getElementById(sectionId);

    if (target) {
      target.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }

  useEffect(() => {
    if (selectedResult) {
      window.setTimeout(() => {
        scrollToSection("estimate-section");
      }, 200);
    }
  }, [selectedResult]);

  return (
    <main className="page">
      <section className="hero">
        <div className="brandBar">
          <div>
            <p className="eyebrow">Yellowstone County, Montana</p>
            <h1>REPredict</h1>
          </div>
          <span className="statusPill">Live MVP</span>
        </div>

        <p className="subtitle">
          Estimated Public Parcel Value powered by public records, market indicators,
          and transparent scenario adjustments.
        </p>

        <div className="heroHighlights">
          <span>Public parcel records</span>
          <span>Market context</span>
          <span>Scenario estimate</span>
        </div>

        <section className="methodologyBox">
          <div>
            <h2>How REPredict Works</h2>
            <p>
              REPredict transforms scattered public property, market, and
              environmental data into a clear parcel-value estimate and location
              profile. Built for homeowners, agents, and investors, it delivers
              faster insight for smarter real-estate decisions—without claiming
              to replace an appraisal or CMA.
            </p>
          </div>

          <div className="methodologyGrid">
            <div>
              <h3>Model Estimate</h3>
              <p>
                The model uses public parcel characteristics, location, property
                type, housing trends, mortgage rates, unemployment, and local
                listing conditions. It is not trained on private MLS data or
                verified sale prices.
              </p>
            </div>

            <div>
              <h3>Location Context</h3>
              <p>
                FEMA environmental data is matched by Census tract. School,
                public-safety, disaster-history, and construction-cost data provide
                additional context but do not change the model estimate.
              </p>
            </div>

            <div>
              <h3>User Scenario</h3>
              <p>
                Optional property details create visible, rule-based adjustments
                to the baseline estimate. This is not a retrained model prediction,
                and photographs are not currently analyzed.
              </p>
            </div>
          </div>

          <p className="methodologyDisclaimer">
            Public records may be incomplete or outdated, and reliability varies
            by property type. REPredict is not an appraisal, CMA, MLS valuation,
            inspection, insurance assessment, or guaranteed sale-price estimate.
          </p>
        </section>

        <form onSubmit={searchParcels} className="searchBox">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search address, city, or parcel ID"
          />
          <button type="submit" disabled={loadingSearch}>
            {loadingSearch ? "Searching..." : "Search"}
          </button>
        </form>

        {error && <div className="error">{error}</div>}
      </section>

      {parcels.length > 0 && (
        <section className="card">
          <h2>Parcel Results</h2>
          <div className="results">
            {parcels.map((parcel) => (
              <button
                key={parcel.parcel_id}
                className="parcelButton"
                onClick={() => getPrediction(parcel.parcel_id)}
              >
                <strong>{parcel.address_line_1 || "No address listed"}</strong>
                <span>
                  {parcel.site_city || "Unknown city"}, {parcel.site_state || "MT"}{" "}
                  {parcel.site_zip_code || ""}
                </span>
                <small>Parcel ID: {parcel.parcel_id}</small>
              </button>
            ))}
          </div>
        </section>
      )}

      {loadingPrediction && (
        <section className="card">
          <p>Generating estimate...</p>
        </section>
      )}

      {selectedResult && (
        <>
          <section id="estimate-section" className="card estimateCard">
            <h2>Estimated Public Parcel Value</h2>
            <div className="sectionNav">
              <span>Step 1 of 4</span>
              <button type="button" onClick={() => scrollToSection("context-section")}>
                Next: Location Context ↓
              </button>
            </div>

            <div className="estimate">
              {formatMoney(selectedResult.prediction.estimated_public_parcel_value)}
            </div>

            <div className="range">
              {formatMoney(selectedResult.prediction.lower_bound)} –{" "}
              {formatMoney(selectedResult.prediction.upper_bound)}
            </div>

            <div className="grid">
              <div>
                <span>Confidence</span>
                <strong>{selectedResult.prediction.confidence}</strong>
              </div>
              <div>
                <span>Segment</span>
                <strong>{selectedResult.model_segment}</strong>
              </div>
              <div>
                <span>Public Total Value</span>
                <strong>{formatMoney(selectedResult.parcel.total_value)}</strong>
              </div>
              <div>
                <span>Range Margin</span>
                <strong>{selectedResult.prediction.range_margin_pct}%</strong>
              </div>
            </div>

            <div className="parcelDetails">
              <h3>Parcel</h3>
              <p>
                <strong>{selectedResult.parcel.address_line_1}</strong>
                <br />
                {selectedResult.parcel.site_city}, {selectedResult.parcel.site_state}{" "}
                {selectedResult.parcel.site_zip_code}
                <br />
                Parcel ID: {selectedResult.parcel.parcel_id}
                <br />
                Property Type: {selectedResult.parcel.property_type}
              </p>
            </div>

            <div className="notes">
              <h3>Model Notes</h3>
              {selectedResult.model_notes.map((note, index) => (
                <p key={index}>{note}</p>
              ))}
            </div>

            <p className="disclaimer">{selectedResult.disclaimer}</p>
          </section>

                    {parcelContext && (
            <section id="context-section" className="card contextCard">
              <h2>Location, Demographic, Risk & Cost Context</h2>
              {parcelContext?.tract_fips && (
                <p className="helperText">
                  Tract-level context matched to {parcelContext.tract_name}
                  {" "}· Census tract FIPS {parcelContext.tract_fips}
                </p>
              )}
              <p className="helperText">
                These indicators provide context only and do not change the model
                estimate. Demographic and environmental indicators are Census-tract
                context; historical storm events are county-level; school and
                public-safety data may cover broader areas; construction-cost
                indicators are national.
              </p>

              <div className="contextGrid">
                {[
                  ["construction_cost", "Construction Cost Context"],
                  ["environmental_risk", "Environmental Risk Context"],
                  ["demographic_context", "Demographic & Housing Context"],
                  ["storm_history", "Historical Storm Events"],
                  ["school_context", "School Context"],
                  ["public_safety", "Public Safety Context"],
                  ["civic_disruption", "Civic Disruption Context"]
                ].map(([key, title]) => (
                  <div className="contextTile" key={key}>
                    <h3>{title}</h3>

                    {(parcelContext.context?.[key] || []).length === 0 ? (
                      <p>No context indicators loaded yet.</p>
                    ) : (
                      <>
                        <p className="contextCount">
                          Showing {Math.min(
  (parcelContext.context?.[key] || []).length,
  key === "environmental_risk"
    ? 10
    : key === "demographic_context"
      ? 10
      : key === "storm_history"
        ? 9
        : key === "school_context"
          ? 9
          : key === "construction_cost"
            ? 6
            : 8
)} of {(parcelContext.context?.[key] || []).length} indicators
                        </p>

                        {(parcelContext.context[key] || [])
                          .slice(
  0,
  key === "environmental_risk"
    ? 10
    : key === "demographic_context"
      ? 10
      : key === "storm_history"
        ? 9
        : key === "school_context"
          ? 9
          : key === "construction_cost"
            ? 6
            : 8
)
                          .map((item) => (
                        <div className="contextMetric" key={item.id || item.metric_name}>
                          <strong>{item.metric_name}</strong>

                          <span>
                            {item.metric_text ||
                              (item.metric_value !== null && item.metric_value !== undefined
                                ? `${item.metric_value}${item.metric_unit ? ` ${item.metric_unit}` : ""}`
                                : "Not available")}
                          </span>

                          {item.metric_value !== null && item.metric_value !== undefined && (
                            <small>
                              Value: {item.metric_value}
                              {item.metric_unit ? ` ${item.metric_unit}` : ""}
                            </small>
                          )}

                          {item.yoy_change_pct !== null && item.yoy_change_pct !== undefined && (
                            <small>
                              Year-over-year change: {Number(item.yoy_change_pct).toFixed(2)}%
                            </small>
                          )}

                          {item.mom_change_pct !== null && item.mom_change_pct !== undefined && (
                            <small>
                              Month-over-month change: {Number(item.mom_change_pct).toFixed(2)}%
                            </small>
                          )}

                          <small>
                            Source: {item.source_name}
                            {item.source_period ? ` · ${item.source_period}` : ""}
                          </small>

                        </div>
                          ))}
                      </>
                    )}
                  </div>
                ))}
              </div>

              <p className="disclaimer">
                Context indicators are not parcel-level inspections, hazard
                certifications, school ratings, neighborhood rankings, safety
                scores, contractor bids, or insurance estimates.
              </p>

              <div className="sectionNav">
                <button
                  type="button"
                  className="secondaryButton"
                  onClick={() => scrollToSection("details-section")}
                >
                  Next: Property Details
                </button>
              </div>
            </section>
          )}

<section id="details-section" className="card">
            <h2>User-Provided Property Details</h2>
            <div className="sectionNav">
              <span>Step 2 of 4</span>
              <button type="button" onClick={() => scrollToSection("scenario-section")}>
                Next: Scenario Estimate ↓
              </button>
            </div>
            <p className="helperText">
              These details are not currently part of the trained machine-learning
              model. They are used to create an optional user-adjusted scenario
              estimate and may also help guide future model improvements.
            </p>

            <div className="scenarioExplainer">
              <h3>How the User-Adjusted Scenario Works</h3>
              <p>
                REPredict first generates a baseline estimate from the trained model.
                When you enter property details below, the app applies a transparent
                adjustment layer to that baseline estimate.
              </p>
              <ul>
                <li>
                  <strong>Baseline Estimate:</strong> Generated by the trained REPredict model.
                </li>
                <li>
                  <strong>User-Adjusted Scenario:</strong> Adjusted using user-entered
                  details such as condition, renovation status, finished square footage,
                  garage size, listing status, and seller motivation.
                </li>
                <li>
                  <strong>Important:</strong> This is not a retrained model prediction,
                  appraisal, CMA, MLS estimate, or guaranteed sale price.
                </li>
              </ul>
            </div>

            <div className="formGrid">
              <label>
                Bedrooms
                <input
                  type="number"
                  min="0"
                  value={propertyDetails.bedrooms}
                  onChange={(e) => updatePropertyDetail("bedrooms", e.target.value)}
                  placeholder="Example: 3"
                />
              </label>

              <label>
                Bathrooms
                <input
                  type="number"
                  min="0"
                  step="0.5"
                  value={propertyDetails.bathrooms}
                  onChange={(e) => updatePropertyDetail("bathrooms", e.target.value)}
                  placeholder="Example: 2.5"
                />
              </label>

              <label>
                Finished Square Footage
                <input
                  type="number"
                  min="0"
                  value={propertyDetails.finishedSqft}
                  onChange={(e) => updatePropertyDetail("finishedSqft", e.target.value)}
                  placeholder="Example: 1850"
                />
              </label>

              <label>
                Garage Size
                <select
                  value={propertyDetails.garageSize}
                  onChange={(e) => updatePropertyDetail("garageSize", e.target.value)}
                >
                  <option value="">Select</option>
                  <option value="none">No garage</option>
                  <option value="1-car">1-car</option>
                  <option value="2-car">2-car</option>
                  <option value="3-car">3-car</option>
                  <option value="4-plus-car">4+ car</option>
                  <option value="unknown">Unknown</option>
                </select>
              </label>

              <label>
                Year Built
                <input
                  type="number"
                  min="1800"
                  max="2100"
                  value={propertyDetails.yearBuilt}
                  onChange={(e) => updatePropertyDetail("yearBuilt", e.target.value)}
                  placeholder="Example: 1978"
                />
              </label>

              <label>
                Renovation Status
                <select
                  value={propertyDetails.renovationStatus}
                  onChange={(e) =>
                    updatePropertyDetail("renovationStatus", e.target.value)
                  }
                >
                  <option value="">Select</option>
                  <option value="recently-renovated">Recently renovated</option>
                  <option value="partially-updated">Partially updated</option>
                  <option value="dated">Dated / needs updates</option>
                  <option value="major-rehab">Major rehab needed</option>
                  <option value="unknown">Unknown</option>
                </select>
              </label>

              <label>
                Interior Condition
                <select
                  value={propertyDetails.interiorCondition}
                  onChange={(e) =>
                    updatePropertyDetail("interiorCondition", e.target.value)
                  }
                >
                  <option value="">Select</option>
                  <option value="excellent">Excellent</option>
                  <option value="good">Good</option>
                  <option value="average">Average</option>
                  <option value="poor">Poor</option>
                  <option value="unknown">Unknown</option>
                </select>
              </label>

              <label>
                Recent Sale Price
                <input
                  type="number"
                  min="0"
                  value={propertyDetails.recentSalePrice}
                  onChange={(e) =>
                    updatePropertyDetail("recentSalePrice", e.target.value)
                  }
                  placeholder="Example: 350000"
                />
              </label>

              <label>
                Listing Status
                <select
                  value={propertyDetails.listingStatus}
                  onChange={(e) => updatePropertyDetail("listingStatus", e.target.value)}
                >
                  <option value="">Select</option>
                  <option value="not-listed">Not listed</option>
                  <option value="active">Active listing</option>
                  <option value="pending">Pending</option>
                  <option value="recently-sold">Recently sold</option>
                  <option value="expired">Expired / withdrawn</option>
                  <option value="unknown">Unknown</option>
                </select>
              </label>

              <label>
                Seller Motivation
                <select
                  value={propertyDetails.sellerMotivation}
                  onChange={(e) =>
                    updatePropertyDetail("sellerMotivation", e.target.value)
                  }
                >
                  <option value="">Select</option>
                  <option value="low">Low motivation</option>
                  <option value="normal">Normal motivation</option>
                  <option value="high">High motivation</option>
                  <option value="distressed">Distressed / urgent</option>
                  <option value="unknown">Unknown</option>
                </select>
              </label>
            </div>

            <label className="wideLabel">
              MLS Comps / Notes
              <textarea
                value={propertyDetails.mlsComps}
                onChange={(e) => updatePropertyDetail("mlsComps", e.target.value)}
                placeholder="Paste or summarize comparable properties here. Only use MLS data you are authorized to use."
                rows="5"
              />
            </label>

            <label className="wideLabel">
              Photos
              <input
                type="file"
                accept="image/*"
                multiple
                onChange={handlePhotoChange}
              />
            </label>

            {propertyDetails.photoNames.length > 0 && (
              <div className="photoList">
                <strong>Selected photos:</strong>
                <ul>
                  {propertyDetails.photoNames.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="detailsSummary">
              <h3>Entered Property Context</h3>
              <p>
                Bedrooms: {propertyDetails.bedrooms || "Not entered"} | Bathrooms:{" "}
                {propertyDetails.bathrooms || "Not entered"} | Finished Sq Ft:{" "}
                {propertyDetails.finishedSqft || "Not entered"}
              </p>
              <p>
                Year Built: {propertyDetails.yearBuilt || "Not entered"} | Condition:{" "}
                {propertyDetails.interiorCondition || "Not entered"} | Renovation:{" "}
                {propertyDetails.renovationStatus || "Not entered"}
              </p>
              <p>
                Recent Sale Price: {formatMoney(propertyDetails.recentSalePrice)} |
                Listing Status: {propertyDetails.listingStatus || "Not entered"} |
                Seller Motivation: {propertyDetails.sellerMotivation || "Not entered"}
              </p>
            </div>
          </section>

          {adjustedScenario && (
            <section id="scenario-section" className="card adjustmentCard">
              <p className="eyebrow">Optional user scenario</p>
              <h2>User-Adjusted Scenario Estimate</h2>
              <div className="sectionNav">
                <span>Step 3 of 4</span>
                <button type="button" onClick={() => scrollToSection("feedback-section")}>
                  Next: Feedback ↓
                </button>
              </div>

              <p className="scenarioIntro">
                This scenario applies the adjustments shown below to the baseline
                model estimate.
              </p>

              <div className="estimate">
                {formatMoney(adjustedScenario.adjustedValue)}
              </div>

              <div className="range">
                {formatMoney(adjustedScenario.lowerBound)} –{" "}
                {formatMoney(adjustedScenario.upperBound)}
              </div>

              <div className="grid">
                <div>
                  <span>Baseline Model Estimate</span>
                  <strong>{formatMoney(adjustedScenario.baseline)}</strong>
                </div>
                <div>
                  <span>User Scenario Adjustment</span>
                  <strong>
                    {adjustedScenario.percentAdjustment > 0 ? "+" : ""}
                    {adjustedScenario.percentAdjustment}%
                  </strong>
                </div>
                <div>
                  <span>Scenario Range Margin</span>
                  <strong>{adjustedScenario.scenarioMargin}%</strong>
                </div>
                <div>
                  <span>Estimate Type</span>
                  <strong>Scenario</strong>
                </div>
              </div>

              {adjustedScenario.adjustments.length > 0 ? (
                <div className="adjustmentList">
                  <h3>Applied Scenario Factors</h3>
                  <ul>
                    {adjustedScenario.adjustments.map((item, index) => (
                      <li key={index}>
                        <span>{item.label}</span>
                        {item.pct !== null && (
                          <strong>
                            {item.pct > 0 ? "+" : ""}
                            {item.pct}%
                          </strong>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : (
                <div className="adjustmentList">
                  <h3>No scenario adjustments entered</h3>
                  <p>
                    Enter property details above to generate a user-adjusted
                    scenario estimate.
                  </p>
                </div>
              )}

              {(adjustedScenario.hasPhotos || adjustedScenario.hasMlsNotes) && (
                <div className="notes">
                  <h3>Additional User Context</h3>
                  {adjustedScenario.hasPhotos && (
                    <p>
                      Photos were selected, but REPredict does not currently
                      analyze image content. Photo support can be added later as
                      a review or computer-vision feature.
                    </p>
                  )}
                  {adjustedScenario.hasMlsNotes && (
                    <p>
                      MLS comp notes were entered as user-provided context. They
                      are not independently verified or directly modeled in this
                      version.
                    </p>
                  )}
                </div>
              )}

              <p className="disclaimer">
                This user-adjusted scenario estimate is not generated by a
                retrained machine-learning model. It is a transparent adjustment
                layer applied to the baseline REPredict estimate using
                user-entered property details. It is not an appraisal, CMA, MLS
                valuation, or guaranteed sale-price estimate.
              </p>
            </section>
          )}

        {selectedResult && (
          <section id="feedback-section" className="card feedbackCard">
            <h2>Estimate Feedback</h2>
            <div className="sectionNav">
              <span>Step 4 of 4</span>
              <button type="button" onClick={() => scrollToSection("estimate-section")}>
                Back to Estimate ↑
              </button>
            </div>
            <p className="helperText">
              Help improve REPredict. Based on what you know about this property,
              did this estimate feel too low, about right, or too high?
            </p>

            <form onSubmit={submitFeedback} className="feedbackForm">
              <div className="feedbackChoices">
                <button
                  type="button"
                  className={feedbackRating === "too_low" ? "feedbackChoice active" : "feedbackChoice"}
                  onClick={() => setFeedbackRating("too_low")}
                >
                  Too Low
                </button>

                <button
                  type="button"
                  className={feedbackRating === "about_right" ? "feedbackChoice active" : "feedbackChoice"}
                  onClick={() => setFeedbackRating("about_right")}
                >
                  About Right
                </button>

                <button
                  type="button"
                  className={feedbackRating === "too_high" ? "feedbackChoice active" : "feedbackChoice"}
                  onClick={() => setFeedbackRating("too_high")}
                >
                  Too High
                </button>
              </div>

              <label className="wideLabel">
                Optional comment
                <textarea
                  value={feedbackComment}
                  onChange={(event) => setFeedbackComment(event.target.value)}
                  rows="3"
                  placeholder="Example: Recently renovated kitchen, dated interior, larger garage, strong comparable sale nearby..."
                />
              </label>

              <button className="submitFeedbackButton" type="submit" disabled={feedbackSubmitting}>
                {feedbackSubmitting ? "Saving..." : "Submit Feedback"}
              </button>

              {feedbackStatus && (
                <p className="feedbackStatus">{feedbackStatus}</p>
              )}
            </form>
          </section>
        )}


        </>
      )}
    </main>
  );
}


function AdminDashboard() {
  const [token, setToken] = useState(() => sessionStorage.getItem("repredictAdminToken") || "");
  const [summary, setSummary] = useState([]);
  const [feedback, setFeedback] = useState([]);
  const [trafficSummary, setTrafficSummary] = useState(null);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  function formatMoney(value) {
    if (value === null || value === undefined || value === "") return "N/A";

    return Number(value).toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 0,
    });
  }

  function formatRating(value) {
    if (value === "too_low") return "Too Low";
    if (value === "about_right") return "About Right";
    if (value === "too_high") return "Too High";
    return value || "Unknown";
  }

  function saveToken() {
    sessionStorage.setItem("repredictAdminToken", token);
    setStatus("Admin token saved for this browser session.");
  }

  function clearToken() {
    sessionStorage.removeItem("repredictAdminToken");
    setToken("");
    setSummary([]);
    setFeedback([]);
    setTrafficSummary(null);
    setStatus("Admin token cleared.");
  }

  async function loadAdminFeedback() {
    if (!token) {
      setStatus("Enter your admin token first.");
      return;
    }

    setLoading(true);
    setStatus("");

    try {
      const headers = { "x-admin-token": token };

      const [recentResponse, summaryResponse, trafficResponse] = await Promise.all([
        axios.get(`${API_BASE_URL}/api/v1/admin/feedback/recent?limit=50`, { headers }),
        axios.get(`${API_BASE_URL}/api/v1/admin/feedback/summary`, { headers }),
        axios.get(`${API_BASE_URL}/api/v1/admin/traffic/summary`, { headers }),
      ]);

      setFeedback(recentResponse.data.feedback || []);
      setSummary(summaryResponse.data.summary || []);
      setTrafficSummary(trafficResponse.data || null);
      sessionStorage.setItem("repredictAdminToken", token);
      setStatus("Feedback loaded.");
    } catch (err) {
      console.error("Admin feedback load error:", err);

      const detail =
        err?.response?.data?.detail ||
        err?.response?.data?.message ||
        err?.message ||
        "Unknown error";

      setStatus(`Could not load admin feedback: ${detail}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="page adminPage">
      <section className="hero adminHero">
        <div className="brandBar">
          <div>
            <p className="eyebrow">Private Admin</p>
            <h1>Feedback Dashboard</h1>
          </div>
          <span className="statusPill">Token Protected</span>
        </div>

        <p className="subtitle">
          Review REPredict estimate feedback submitted by testers.
        </p>

        <div className="adminActions">
          <a href="/" className="adminBackLink">← Back to REPredict</a>
        </div>
      </section>

      <section className="card adminTokenCard">
        <h2>Admin Access</h2>
        <p className="helperText">
          Enter your private admin token. The token is stored only in this browser session.
        </p>

        <div className="adminTokenRow">
          <input
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="Paste admin token"
          />
          <button type="button" onClick={saveToken}>Save Token</button>
          <button type="button" onClick={loadAdminFeedback} disabled={loading}>
            {loading ? "Loading..." : "Load Feedback"}
          </button>
          <button type="button" className="secondaryButton" onClick={clearToken}>Clear</button>
        </div>

        {status && <p className="feedbackStatus">{status}</p>}
      </section>

      <section className="card adminTrafficCard">
        <h2>Website Traffic</h2>

        {!trafficSummary ? (
          <p className="helperText">
            No traffic data loaded yet. Click Load Feedback to refresh admin data.
          </p>
        ) : (
          <div className="adminSummaryGrid">
            <div className="adminSummaryTile">
              <span>Page Views</span>
              <strong>{trafficSummary.page_views || 0}</strong>
              <small>Total recorded page views</small>
            </div>

            <div className="adminSummaryTile">
              <span>Searches</span>
              <strong>{trafficSummary.searches || 0}</strong>
              <small>Property searches submitted</small>
            </div>

            <div className="adminSummaryTile">
              <span>Parcel Clicks</span>
              <strong>{trafficSummary.parcel_selections || 0}</strong>
              <small>Selected parcel estimates</small>
            </div>

            <div className="adminSummaryTile">
              <span>Feedback</span>
              <strong>{trafficSummary.feedback_submissions || 0}</strong>
              <small>Feedback submissions tracked</small>
            </div>

            <div className="adminSummaryTile">
              <span>Total Events</span>
              <strong>{trafficSummary.total_events || 0}</strong>
              <small>All recorded traffic events</small>
            </div>

            <div className="adminSummaryTile">
              <span>Latest Activity</span>
              <strong className="smallMetric">
                {trafficSummary.latest_event_at
                  ? new Date(trafficSummary.latest_event_at).toLocaleDateString()
                  : "N/A"}
              </strong>
              <small>
                {trafficSummary.latest_event_at
                  ? new Date(trafficSummary.latest_event_at).toLocaleTimeString()
                  : "No events yet"}
              </small>
            </div>
          </div>
        )}
      </section>

      <section className="card adminSummaryCard">
        <h2>Feedback Summary</h2>

        {summary.length === 0 ? (
          <p className="helperText">No summary rows loaded yet.</p>
        ) : (
          <div className="adminSummaryGrid">
            {summary.map((row) => (
              <div key={row.rating} className="adminSummaryTile">
                <span>{formatRating(row.rating)}</span>
                <strong>{row.feedback_count}</strong>
                <small>Avg baseline: {formatMoney(row.avg_baseline_estimate)}</small>
                <small>Avg adjusted: {formatMoney(row.avg_adjusted_estimate)}</small>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card adminFeedbackCard">
        <h2>Recent Feedback</h2>

        {feedback.length === 0 ? (
          <p className="helperText">No feedback rows loaded yet.</p>
        ) : (
          <div className="adminTableWrap">
            <table className="adminTable">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Rating</th>
                  <th>Address</th>
                  <th>Parcel ID</th>
                  <th>Baseline</th>
                  <th>Adjusted</th>
                  <th>Comment</th>
                </tr>
              </thead>
              <tbody>
                {feedback.map((row) => (
                  <tr key={row.id || `${row.parcel_id}-${row.created_at}`}>
                    <td>{row.created_at ? new Date(row.created_at).toLocaleString() : "N/A"}</td>
                    <td>
                      <span className={`ratingBadge rating-${row.rating}`}>
                        {formatRating(row.rating)}
                      </span>
                    </td>
                    <td>{row.address_line_1 || "N/A"}</td>
                    <td>{row.parcel_id || "N/A"}</td>
                    <td>{formatMoney(row.baseline_estimate)}</td>
                    <td>{formatMoney(row.adjusted_estimate)}</td>
                    <td>{row.comment || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

function App() {
  const params = new URLSearchParams(window.location.search);
  const isAdminDashboard =
    window.location.pathname === "/admin" ||
    params.get("admin") === "feedback";

  if (isAdminDashboard) {
    return <AdminDashboard />;
  }

  return <MainApp />;
}

export default App;
