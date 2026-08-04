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
- Query InfluxDB from 1020 sites (68 successful sites with RUL data)
- Extract freecooling episodes (fan ≥95%, FC mode active, ≥30 min)
- Compute max ΔT vs percentage-adjusted cooling hours (physics-based: fan_speed²)
- Fetch 90-day PM10 + PM2.5 averages from Weatherbit (3×30-day chunks per site) — **BLOCKED: missing lat/long coordinates**
- Flexible regression: Currently all 1-factor (hours only) due to missing air quality data
- Linear projection to failure (10°C threshold); convert adjusted hours → days RUL

**August 4, 2026 Session Updates**:
- ✅ Fixed dashboard TypeError: Type-checking before formatting floats
- ✅ Fixed trend plots: Now using `max_deltas` (actual data field) instead of `onset_deltas`/`rolling_median` (missing)
- ✅ Simplified data loading: dashboard_unified.py at root reads sites_data.json directly
- ✅ Confirmed 68 successful sites with RUL data in sites_data.json
- ❌ **Air quality blocked**: 0 of 68 sites have lat/long coordinates → Weatherbit API can't be called
- 📍 Coordinates stored in separate file on Bell laptop (filename TBD) — needs to be integrated into query_sites.py

**Regression Model** (3-factor when both pollutants available):
```
Slope ~ β₀ + β₁*(adjusted_fan_hours_per_day) + β₂*(PM10_avg) + β₃*(PM2.5_avg)
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

**Missing Coordinates → No Air Quality Data**:
- Root cause: Sites don't have lat/long fields in InfluxDB query results
- Impact: Weatherbit API can't fetch PM10/PM2.5 → all sites show 1-Factor model (hours only)
- Fix: Integrate coordinate file from Bell laptop into query_sites.py
  - File location: TBD (on Bell laptop, filename unknown)
  - Solution: Load coords from separate CSV/JSON → merge into site data before Weatherbit call
  - Once fixed: 2/3-factor models available → air quality correlation analysis enabled

**Network Access** (66% of sites unreachable from Bell office):
- Root cause: Sites on isolated regional networks
- Workaround: Script gracefully skips unreachable sites, continues with successful ones
- Resolution: Would need to run from server/Pi on internal network

**Authentication** (16% of sites):
- Some sites don't use default `plc` / `sitelogic` credentials
- Workaround: Script logs failures; manual SSH testing needed
- Resolution: Document per-site credentials

## Next Steps (Priority Order)

1. **BLOCKING: Integrate site coordinates**
   - Find coordinate file on Bell laptop (filename TBD)
   - Update query_sites.py to load lat/long from that file
   - Merge coords into site data before calling Weatherbit API
   - Re-run query_sites.py to fetch air quality → unlock 2/3-factor models

2. **Dashboard improvements** (once coordinates are added):
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
