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

**Pollution Effect Model**:
```
For sites WITH air quality:
  adjusted_slope = raw_slope × (1 + β₁×PM2.5 + β₂×PM10)
  RUL = (FAILURE_DT - intercept) / adjusted_slope

For sites WITHOUT air quality:
  slope = raw_slope (no adjustment)
  RUL = (FAILURE_DT - intercept) / slope
```

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

## Known Issues

**Network Access** (66% of sites unreachable from Bell office):
- Root cause: Sites on isolated regional networks
- Workaround: Script gracefully skips unreachable sites, continues with successful ones
- Resolution: Would need to run from server/Pi on internal network

**Authentication** (16% of sites):
- Some sites don't use default `plc` / `sitelogic` credentials
- Workaround: Script logs failures; manual SSH testing needed
- Resolution: Document per-site credentials

## Next Steps (Priority Order)

1. **TEST: Run full query_sites.py with coordinate integration + pollution effect model**
   - Place sites_inventory.csv and sites_inventory_2.csv in tcu_hvac_rul folder (Bell laptop)
   - Run `python query_sites.py` to verify:
     - CSV cross-reference works (coordinates attached to sites)
     - Weatherbit API calls succeed for sites with coords
     - Regression fit calculates β₁, β₂ correctly
     - RUL values updated with pollution effect multiplier
   - Monitor output for any API rate limit issues (1500 req/day Weatherbit limit)

2. **Dashboard improvements** (after test confirms air quality data flow):
   - Monitor regression outputs: Track which pollutants correlate most with filter clogging
   - Regional analysis: Compare air quality effects across different geographic areas
   - Add model type filter to dashboard (1-factor vs 2/3-factor)

3. **Operational**:
   - Daily query runs: Schedule `python query_sites.py` to run daily, commit/push results
   - Network expansion: Test running script from internal server to reach more sites

## Token Efficiency Note

This folder is organized to minimize Claude context pollution:
- Only HVAC project files here (query_sites.py, dashboard, docs)
- Large test/simulation data archived separately
- Open Claude in this directory: `cd hvac_rul_project && claude`
- Keeps token usage low, context focused
