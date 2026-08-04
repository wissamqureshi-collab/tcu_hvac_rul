# HVAC Filter RUL Prediction System — 1020 Rogers Sites

**Project Goal**: Query filter clogging RUL across 1020 HVAC sites via parallel SSH + InfluxDB, display unified dashboard with air quality correlation analysis.

## Quick Start

**Prerequisites**:
```bash
pip install -r requirements.txt
```

**Workflow**:
1. Run queries: `python query_sites.py` (~50 min for 1020 sites, ~30 sec for 34 with data)
2. View dashboard locally: `streamlit run dashboard_unified.py`
3. Commit/push to GitHub: changes auto-deploy to https://tcu-hvac-rul.streamlit.app

## Key Files

| File | Purpose | Location |
|------|---------|----------|
| `query_sites.py` | Parallel SSH + InfluxDB queries (1020 sites); computes RUL with air quality | `/home/aillm/hvac_rul_project/` |
| `dashboard_unified.py` | Streamlit RUL visualization (max ΔT vs adjusted hours); dynamic threshold slider | **`/home/aillm/` (root, used by Streamlit Cloud)** |
| `sites_data.json` | Output from latest query run (pre-computed, read by dashboard) | **`/home/aillm/` (root, committed to GitHub)** |
| `rul_engine.py` | Archive: per-site RUL service (replaced by unified query approach) | `/home/aillm/hvac_rul_project/` |
| `requirements.txt` | Dependencies (streamlit, plotly, pandas, paramiko, numpy, scipy, requests) | `/home/aillm/hvac_rul_project/` |

## .env (Local, DO NOT COMMIT)

Create `.env` in this folder:
```
SITE_PASSWORD=sitelogic
WEATHERBIT_API_KEY=8c1ecfc1b6b7468e8451fca1b3159267
```

## .gitignore (DO NOT COMMIT)

```
.env
sites_inventory.csv
sites_data*.json
*.pyc
__pycache__/
.DS_Store
```

## Current Status

**Data Flow**:
- Load site inventory from two CSVs: sites_inventory.csv (IP, Site) + sites_inventory_2.csv (Site ID, Lat, Lon)
- Cross-reference Site ↔ Site ID to attach coordinates to each site
- Query InfluxDB from 1020 sites via SSH (68 sites currently return RUL data)
- Extract freecooling episodes (fan ≥95%, FC mode active, ≥30 min)
- Compute max ΔT vs percentage-adjusted cooling hours (physics-based: fan_speed²)
- For sites WITH coordinates: Fetch 90-day hourly air quality from Weatherbit → calculate PM2.5/PM10 averages
- For sites WITHOUT coordinates: Skip Weatherbit, use InfluxDB data only
- Run regression on all sites with air quality data: fit β₁, β₂ from observed slopes vs pollutants
- Apply pollution effect multiplier: adjusted_slope = slope × (1 + β₁×PM2.5 + β₂×PM10)
- Linear projection to failure (10°C threshold); convert adjusted hours → days RUL

**August 4 (Session 1) Updates**:
- ✅ Fixed dashboard TypeError: Type-checking before formatting floats
- ✅ Fixed trend plots: Now using `max_deltas` (actual data field) instead of `onset_deltas`/`rolling_median` (missing)
- ✅ Simplified data loading: dashboard_unified.py at root reads sites_data.json directly
- ✅ Confirmed 68 successful sites with RUL data in sites_data.json
- ❌ Air quality blocked: 0 of 68 sites had lat/long coordinates

**August 4 (Session 2) Updates**:
- ✅ Located coordinate files on Bell laptop: `sites_inventory.csv` (IP, Site, Device) + `sites_inventory_2.csv` (Site ID, Lat, Lon)
- ✅ Verified Weatherbit API format: Hourly historical data with PM2.5, PM10 in μg/m³
- ✅ Verified InfluxDB schema: Contains hvac_DELTA_T, hvac_FREE_COOL_MODE, fan_status fields as expected
- ✅ Implemented dual-model pollution effect approach:
  - Sites WITH coordinates: `adjusted_slope = slope × (1 + effect)` where `effect = β₁×PM2.5 + β₂×PM10`
  - Sites WITHOUT coordinates: Simple linear `slope` (no adjustment)
- ✅ Updated query_sites.py to cross-reference Site (sites_inventory.csv) ↔ Site ID (sites_inventory_2.csv)
- ✅ Added regression module to fit pollution coefficients from 90-day air quality + slope data

**August 4 (Session 3) Updates**:
- ✅ **Complete query_sites.py implementation delivered** with:
  - `load_inventory()`: Robust CSV cross-reference with case/whitespace normalization
  - `fetch_weatherbit_90day_avg()`: Chunked 90-day historical fetch (3 × 30-day chunks to avoid "Request too large")
  - `extract_episodes()`: Robust sensor naming (fallback chains for fan_status, fc_mode, delta_t)
  - `compute_rul_mode3()`: Returns full metrics dict (max_deltas, cumulative_hours, slope, R², RUL, urgency, % life)
  - `run_air_quality_regression()`: Flexible 1/2/3-factor regression (auto-selects features based on available data)
  - `apply_pollution_effect_to_rul()`: Recalculates RUL for sites with air quality using adjusted slope
  - `query_all_sites_parallel()`: ThreadPoolExecutor with concurrent workers (default 10)
- ✅ Output JSON includes: query timestamp, elapsed time, urgency summary, regression results, per-site metrics
- ✅ Ready for first full test run

**August 4 (Session 4) Updates**:
- ✅ **First successful full query run completed on Bell laptop**:
  - 60 of 1020 sites successfully queried (remaining unreachable from office network)
  - 575 sites had coordinates loaded from CSV cross-reference
  - 48 sites with complete Weatherbit air quality data (90-day averages)
  - 3-factor regression fitted: slope ~ adjusted_hours + PM10 + PM2.5
  - Regression coefficients: β_adjusted_hours=0.003935, β_PM10=-0.032911, β_PM2.5=0.047004
  - R²=0.0426 (explains ~4.3% of slope variance; site-specific factors dominate)
  - Pollution effect successfully applied to adjust RUL for all 48 sites with air quality
- ✅ **Dashboard rebuilt** with controls in main content (sidebar was causing rendering issues):
  - Control panel at top with 4 sliders: Duration, Fan Speed, Rolling Window, Failure ΔT
  - Filter/sort options in collapsible section
  - Air quality regression summary visible in Model Architecture expander
  - All equations formatted as readable black text (removed code blocks)
- ✅ **Documentation clarified**:
  - Pollution effect is a multiplier on raw_slope, not a separate linear factor in ΔT equation
  - All sites use base equation: ΔT = β₀ + β₁ × (adj_hours)
  - Adjusted slope = raw_slope × (1 + β_pm10×PM10 + β_pm25×PM2.5)
- ✅ **Pipeline fully functional**: Query → Regression → RUL Adjustment → Dashboard Display

**Pollution Effect Model** (Multiplier Approach):

**Step 1: Fit Regression across all sites with air quality data**
- Model: slope ~ adjusted_hours + PM10 + PM2.5 (flexible 1/2/3-factor based on data)
- Output: Coefficients β_hours, β_pm10, β_pm25 that quantify how each factor relates to degradation rate

**Step 2: Apply Multiplier to each site's slope**
- Base equation (all sites): ΔT = β₀ + β₁ × (adj_hours)
- Pollution effect: effect = β_pm10 × PM10 + β_pm25 × PM2.5
- Adjusted slope: adjusted_slope = raw_slope × (1 + effect)
- Final RUL: (FAILURE_DT - intercept) / adjusted_slope

**Key insight**: PM10 and PM2.5 are **never** linear factors in the ΔT equation. They only modify the degradation rate slope.

**Urgency Logic**:
- 🔴 URGENT: RUL < 14 days
- 🟡 WARNING: RUL 14–30 days
- 🟢 OK: RUL ≥ 30 days

## Physics Model: Percentage-Adjusted Hours

**Why**: Air resistance ∝ fan_speed². Partial-speed operation produces proportionally less filter clogging.

**Calculation**:
```python
adjusted_hours = duration_min × (fan_speed_pct)² / 60
```

**Examples**:
- 1 hour at 100% fan = 1.0 adjusted hours
- 1 hour at 50% fan = 0.25 adjusted hours
- 1 hour at 75% fan = 0.5625 adjusted hours

## Deployment

**GitHub**: https://github.com/wissamqureshi-collab/tcu_hvac_rul.git

**Streamlit Cloud** (auto-deploy from GitHub):
- Live dashboard: https://tcu-hvac-rul.streamlit.app
- Reads pre-computed `sites_data.json` (no SSH/InfluxDB access needed)
- Workflow: User runs query_sites.py locally → commits sites_data.json → pushes → dashboard auto-updates

## query_sites.py Implementation Details

**Workflow**:
1. **Load inventory** → CSV cross-reference (sites_inventory.csv ↔ sites_inventory_2.csv by normalized Site ID)
2. **Parallel SSH queries** → 10 concurrent ThreadPoolExecutor workers, query each site's InfluxDB
3. **Extract episodes** → Identify freecooling bursts (fan ≥95%, FC mode active, ≥30 min)
4. **Compute RUL** → Linear regression max_ΔT vs cumulative adjusted hours, R² ≥ 0.25 required
5. **Fetch air quality** → Weatherbit 90-day historical for sites with coordinates (chunked 30-day requests)
6. **Fit regression** → Slope ~ adjusted_hours ± PM10 ± PM2.5 (flexible 1/2/3-factor)
7. **Apply adjustment** → Recalculate RUL with pollution effect for sites with air quality data
8. **Output JSON** → sites_data.json with full metrics, regression results, urgency summary

**Configuration** (tunable in script):
- `FAN_THRESHOLD = 95.0` — Minimum fan % to trigger episode
- `MIN_EPISODE_MINUTES = 30.0` — Minimum episode duration
- `R2_THRESHOLD = 0.25` — Minimum R² to estimate RUL
- `FAILURE_DT = 10.0` — Temperature threshold for filter failure
- `SSH_TIMEOUT = 30` — SSH connection timeout (seconds)
- `QUERY_TIMEOUT = 60` — InfluxDB query timeout (seconds)
- `QUERY_DAYS = 90` — Historical data window
- `max_workers = 10` — Parallel threads (increase for faster runs, decrease if rate-limited)

**Output JSON structure**:
```json
{
  "query_timestamp": "2026-08-04T...",
  "query_elapsed_seconds": 1234,
  "sites_queried": 68,
  "sites_total": 1020,
  "sites_failed": 952,
  "sites_with_air_quality": 45,
  "urgency_summary": {"URGENT": 5, "WARNING": 12, "OK": 51},
  "air_quality_regression": {
    "model_type": "2-factor (adjusted_hours + PM2.5)",
    "sites_analyzed": 45,
    "r_squared": 0.42,
    "coefficient_adjusted_hours": 0.001234,
    "coefficient_pm25": 0.000567,
    "coefficient_pm10": null
  },
  "sites": {
    "SITE-001": {
      "site_id": "SITE-001",
      "success": true,
      "slope": 0.08,
      "adjusted_slope": 0.085,
      "pollution_effect": 0.0625,
      "rul_days": 18.5,
      "urgency": "WARNING",
      "air_quality": {"pm10": 45.2, "pm25": 12.8},
      "latitude": 40.123,
      "longitude": -75.456,
      ...
    }
  }
}
```

## Known Issues & Mitigations

**Network Access** (~66% of sites unreachable from Bell office):
- Root cause: Sites on isolated regional networks
- Mitigation: Script gracefully skips timeouts, continues with successful sites
- Resolution: Run from internal server/Pi to reach blocked sites

**Authentication** (~16% of sites):
- Root cause: Non-standard plc user credentials per site
- Mitigation: Script logs failures; continue with sites that authenticate
- Resolution: Manual SSH probe per site; document credentials in CLAUDE.md

**Weatherbit API Rate Limits**:
- Limit: 1500 requests/day (depends on API tier)
- Chunked requests: Script splits 90-day window into 3 × 30-day chunks to avoid "Request too large" error
- Mitigations:
  - If 429 (rate-limited): Retry next day or reduce `max_workers` (fewer concurrent sites → fewer simultaneous Weatherbit calls)
  - If quota exceeded: Script logs warning and continues without air quality for that site (degrades to 1-factor model)

**CSV Cross-Reference**:
- Issue: Site ID format might differ (leading zeros, case sensitivity, whitespace)
- Mitigation: Script normalizes both CSVs (uppercase, strip whitespace) before matching
- Fallback: Sites without matched coordinates skip Weatherbit; use raw slope (1-factor model)

## Next Steps (Priority Order)

1. **Validate regression model** (Session 5):
   - Inspect which pollutants drive filter degradation (β_PM2.5 > 0 suggests pollution accelerates clogging)
   - β_PM10 < 0 is counterintuitive; possible confounding factors (e.g., sites in polluted areas run cooler)
   - Consider: add humidity/temperature normalization in future iterations
   - Monitor: Check if R² improves as more sites accumulate air quality data

2. **Dashboard monitoring** (ongoing):
   - Verify RUL values make physical sense (sites in clean air should have longer RUL than polluted sites)
   - Watch for outliers: Sites with dramatic RUL shifts due to pollution effect
   - Track urgency category shifts as pollution effect adjusts slopes

3. **Operational** (after current test confirmed stable):
   - Schedule daily runs: `0 */6 * * * cd /home/aillm/hvac_rul_project && python query_sites.py && cd /home/aillm && git add sites_data.json && git commit -m "Auto-update RUL data" && git push`
   - Network expansion: Test running from internal server to reach blocked sites (currently 66% unreachable)
   - Monitor Weatherbit API quota: Track cumulative requests vs 1500/day limit

4. **Future enhancements** (after pipeline stable):
   - Refactor regression: Add temperature/humidity normalization (may improve R²)
   - Regional analysis: Geographic heatmap of pollution effect strength
   - Predictive alerts: Notify when sites entering URGENT or WARNING thresholds
   - Historical tracking: Store daily RUL snapshots to monitor degradation acceleration

## Token Efficiency Note

This folder is organized to minimize Claude context pollution:
- Only HVAC project files here (query_sites.py, dashboard, docs)
- Large test/simulation data archived separately
- Open Claude in this directory: `cd hvac_rul_project && claude`
- Keeps token usage low, context focused
