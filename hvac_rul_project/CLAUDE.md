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
| `w2567_analysis.py` | CSV-based analysis for sites without SSH access (e.g., W2567 Derry Road Milton) | `/home/aillm/hvac_rul_project/` |
| `dashboard_unified.py` | Streamlit RUL visualization (max ΔT vs adjusted hours); dynamic threshold slider; supports SSH & CSV sites | **`/home/aillm/` (root, used by Streamlit Cloud)** |
| `sites_data.json` | Output from latest query run + CSV analyses (pre-computed, read by dashboard) | **`/home/aillm/` (root, committed to GitHub)** |
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
- **Track max ΔT at each freecooling event** to measure filter clogging progression
- Compute max ΔT vs percentage-adjusted cooling hours (physics-based: fan_speed²)
- For sites WITH coordinates: Fetch 90-day hourly air quality from Weatherbit → calculate PM2.5/PM10 averages
- For sites WITHOUT coordinates: Skip Weatherbit, use InfluxDB data only
- Run regression across sites with air quality data to quantify pollution impact on filter degradation: fit β₁, β₂ from observed slopes vs pollutants
- **For sites WITH air quality data**: Calculate how pollution (PM2.5/PM10) deviates from site-average and predict impact on degradation rate
- **For sites WITHOUT air quality data**: Use raw slope with only max delta values (no pollution context)
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

**August 4 (Session 4-5) Updates**:
- ✅ **First successful full query run completed on Bell laptop**:
  - 60 of 1020 sites successfully queried (remaining unreachable from office network)
  - 575 sites had coordinates loaded from CSV cross-reference
  - 48 sites with complete Weatherbit air quality data (90-day averages)
  - 3-factor regression fitted: slope ~ adjusted_hours + PM10 + PM2.5
  - Regression coefficients: β_adjusted_hours=0.003935, β_PM10=-0.032911, β_PM2.5=0.047004
  - R²=0.0426 (explains ~4.3% of slope variance; site-specific factors dominate)
- ✅ **Key realization: Pollution already baked into ΔT measurements**
  - ΔT trends naturally reflect polluted vs clean air at each site
  - No need for separate pollution adjustment multiplier on RUL
  - Regression used only to contextualize: "How does this site's pollution compare to site average?"
- ✅ **Dashboard rebuilt** with controls in main content (sidebar was causing rendering issues):
  - Control panel at top with 4 sliders: Duration, Fan Speed, Rolling Window, Failure ΔT
  - All sites now use 1-factor model: ΔT = β₀ + β₁ × (adj_hours)
  - New pollution impact blurb in air quality section:
    - Shows site's PM2.5 and PM10 vs site average
    - Predicts impact: "filter degrades X days faster/slower than typical"
  - All equations formatted as readable black text with actual parameter values
- ✅ **Model simplified**: 
  - RUL calculation: Pure 1-factor (no pollution adjustment)
  - Pollution context: Informational blurb showing site-level comparison
  - Regression coefficients used only for impact prediction, not RUL modification
- ✅ **Pipeline fully functional**: Query → Regression → Dashboard Display (no RUL adjustment step)

**August 7 (Session 3) Updates — Episode Timestamp Capture**:
- ✅ **Modified query_sites.py to capture exact episode timestamps**:
  - `episode_start_times`: List of ISO datetime strings for each freecooling episode
  - `query_start_date`: Date of first episode (YYYY-MM-DD format)
  - `query_end_date`: Date of last episode (YYYY-MM-DD format)
- ✅ **Enables precise post-filter day calculation on dashboard**:
  - Dashboard can now interpolate exact date when filter change occurred
  - When user enters filter change at cumulative_adjusted_hours = X:
    - Find episode indices where X falls
    - Linear interpolate between episode timestamps to get exact change date
    - Calculate exact post-filter days: query_end_date - filter_change_date
    - Recalculate avg_adjusted_hours_per_day from post-filter segment only
  - No more estimation needed — uses actual episode timing data
- ✅ **Benefits for avg_adjusted_hours_per_day recalculation**:
  - Clogged filters cause extended fan runtime (higher avg hours/day)
  - After filter change, avg_adjusted_hours_per_day should decrease
  - Dashboard can now calculate exact post-filter average from interpolated dates
  - More accurate RUL projections after manual filter change entry

**August 7 (Session 2) Updates — Negative Slope Handling**:
- ✅ **Filtered negative slopes from regression analysis**: query_sites.py now excludes sites with slope ≤ 0 from air quality regression (insufficient data)
- ✅ **Dashboard displays "Data Insufficient" for negative slopes**:
  - Sites with slope ≤ 0 show warning message instead of analysis
  - Still display IP address and air quality data (if available)
  - Skip pollution impact blurb for these sites
  - Don't show model configuration, trend analysis, RUL estimate sections
  - Trend plot shown for all sites (including negative slopes) to enable manual filter change entry
- ✅ **Filter change can "rescue" negative-slope sites**: If user confirms filter change and post-change slope becomes positive:
  - Show full analysis using post-change regression
  - Display pollution impact blurb if air quality data available
  - Include in regression analysis for future query runs

**August 7 (Session 1) Updates — Dashboard Enhancements**:
- ✅ **Linear regression-based urgency**: Urgency status now derived from trend line projection
  - Current ΔT calculated from linear fit: `current_dt = intercept + slope × current_hours`
  - When ΔT reaches failure threshold (10°C default) determines RUL days
  - Offers clearer predictive warning: "filter will fail in X days based on degradation trend"
- ✅ **Automatic filter change detection**:
  - Scans max_deltas for sudden drops ≥5°C (symptom of filter replacement)
  - If detected, presents confirmation UI with estimated filter change time in hours
  - Allows user to manually adjust the detected time via number input
- ✅ **Dual regression model for confirmed filter changes**:
  - Splits data at filter change point into pre-change and post-change segments
  - Fits separate linear regression to each segment
  - Displays both trend lines on graph (orange dashed for pre-change, green dashed for post-change)
  - RUL calculation uses post-change regression (more relevant for current condition)
- ✅ **Manual filter change input** (for sites without detected changes):
  - Checkbox to confirm "Yes, filter was changed at this site"
  - Number input to specify when (in cumulative adjusted hours)
  - Creates dual regression model with user-specified change time
- ✅ **Rolling median window reset**:
  - When filter change is entered, rolling median smoothing applied separately to each segment
  - Pre-segment: smoothed independently with rolling window
  - Post-segment: smoothed independently with rolling window (fresh start, no carryover)
  - Prevents smoothing artifacts across the filter change boundary
- ✅ **Cleaner layout**:
  - Model architecture explanation moved to expandable section (default closed)
  - Control sliders in sidebar: Duration, Fan Speed, Rolling Window, Failure ΔT
  - Metrics cards show URGENT/WARNING/OK/FAILED counts and average RUL
  - Sites table displays all key metrics with sortable columns
  - Individual site expandable cards with dual-regression trend plot
  - Better visual hierarchy and reduced clutter

**August 10 (Current Session) Updates — W2567 CSV Analysis & Dashboard Support**:
- ✅ **New CSV-based analysis module** (w2567_analysis.py):
  - Analyzes minute-level HVAC data without SSH access
  - Robust episode extraction from dense time-series (129k rows over 3 months)
  - Auto-detects filter changes as sudden ΔT drops (≥5°C)
  - Handles noisy/variable fan speed with configurable thresholds
- ✅ **W2567 (Derry Road Milton, Mississauga) analysis complete**:
  - **Data span**: May 12 – Aug 10, 2026 (3 months, minute-level CSV)
  - **Episodes extracted**: 49 sustained freecooling periods (fan ≥95%, free-cool active, ≥30 min)
  - **Adjusted cooling hours**: 516.9 total, averaging 5.74 hrs/day
  - **Filter change detected**: Episode 2 (May 19 at 12:10 UTC)
    - Before: 29.0°C (clogged filter nearing end-of-life)
    - After: 7.5°C (clean replacement filter)
    - Drop: 21.5°C (clear maintenance event)
  - **Post-change data quality**: 47 episodes, ΔT ranging 3.6–9.5°C
  - **Analysis status**: Insufficient degradation yet (R²=0.0001, slope≈0)
    - New filter needs 1-2 more months for trend detection
    - Recommendation: Monitor over next quarterly run
  - **Coordinates**: 43.541944, -79.826389 (Mississauga, ON)
  - **Status note**: "Filter change detected. Post-change data insufficient for RUL yet; monitor over next 1-2 months for degradation trend."
- ✅ **Dashboard support for CSV sites**:
  - Displays CSV-based sites (data_source='csv') in detail view even without RUL
  - Shows filter change metadata in expandable cards
  - Includes all episode data (timestamps, cumulative hours, max deltas) for trend visualization
  - Converts W2567's native filter_change schema to dashboard's event format
  - "📊 Filter Change Detected" label in site cards for easy identification
  - Air quality context available if site has coordinates
- ✅ **Workflow for adding CSV sites**:
  1. Upload CSV to GitHub repo with naming: `{SITE_ID}_months_info.csv`
  2. Create site metadata (IP, lat/lon, address)
  3. Run w2567_analysis.py locally to generate JSON output
  4. Merge result into sites_data.json
  5. Dashboard auto-displays on next page load
  6. No SSH credentials required

**Model Architecture (1-Factor Only with Dual Regression)**:

**Core Methodology**:
- **Filter degradation tracking**: Analyze max ΔT recorded at each freecooling event to measure filter clogging progression over time
- **Single-factor RUL model**: All sites use identical 1-factor model based on percentage-adjusted cooling hours
- **Air quality analysis**: Separate regression layer that quantifies how pollution correlates with filter degradation rates across the site population
- **Differentiated treatment**:
  - **Sites WITH air quality data** (full or partial): Display pollution impact context showing how this site's PM2.5/PM10 deviates from population average
  - **Sites WITHOUT air quality data**: Use only max delta values; no pollution context available

**RUL Calculation** (all sites identical, single or dual model):
- **Base equation**: ΔT = β₀ + β₁ × (cumulative_adjusted_hours)
- **Fitted from**: max_ΔT values across all freecooling episodes
- **For single regression** (no filter change): Uses all data points
- **For dual regression** (filter change confirmed): Uses only post-filter-change data for RUL calculation
- **Current ΔT state**: Calculated from trend line at current cumulative hours
- **RUL projection**: Calculates when trend line will cross failure threshold (10°C default):
  - hours_to_failure = (failure_ΔT - intercept) / slope
  - remaining_hours = hours_to_failure - current_hours
  - rul_days = remaining_hours / avg_adjusted_hours_per_day
- Uses raw slope (no pollution adjustment multiplier on RUL itself)

**Pollution Impact Context** (informational, sites with air quality data only):
- Air quality is already baked into the ΔT measurements (polluted air naturally increases ΔT faster at that site)
- Regression fitted across all sites WITH air quality: slope ~ adjusted_hours + PM10 + PM2.5
- For each site with data, calculate deviation from population average: (site_PM25 - avg_PM25) and (site_PM10 - avg_PM10)
- Predicted impact = β_pm25 × ΔPM2.5 + β_pm10 × ΔPM10 (in degradation rate days)
- Display as blurb: "This site's air quality means filter degrades **X days faster/slower than typical**"
- Sites without air quality data show no pollution impact context (only RUL based on max delta values)

**Key insight**: Pollution effect is already in each site's observed slope; regression contextualizes how this site's pollution compares to the population average.

**Urgency Logic** (based on linear trend projection):
- Uses fitted linear regression line: ΔT = intercept + slope × (cumulative_adjusted_hours)
- **Current ΔT state** = intercept + slope × (current_hours) from trend line
- **Urgency determined by when ΔT will reach failure threshold** (default 10°C):
  - 🔴 URGENT: RUL < 14 days (projected to hit threshold within 2 weeks)
  - 🟡 WARNING: RUL 14–30 days (threshold within 2–4 weeks)
  - 🟢 OK: RUL ≥ 30 days (at least 30 days until failure)
- ⚪ Context: Air quality blurb shows if site's pollution would add/subtract days vs typical site

**Filter Change Detection & Dual Regression**:
- **Automatic detection**: System identifies sudden ≥5°C drops in max ΔT values
- **If detected**: Dashboard asks for confirmation and estimates the filter change time (in cumulative adjusted hours)
- **If confirmed**: Fits two separate linear regressions:
  - Pre-filter-change segment: Trend line before the drop (shown as dashed orange line)
  - Post-filter-change segment: Trend line after the drop (shown as dashed green line, used for RUL calculation)
- **Manual override**: User can adjust the filter change time via a number input that updates in real-time
- **If no detection**: Dashboard provides option to manually input filter change time
- **Rolling median reset**: When a filter change is entered, rolling median smoothing is applied separately to pre-change and post-change segments (no carryover between segments)
- **RUL recalculation**: Always uses post-filter-change regression for current RUL projection (most relevant for remaining life)

**Exact Post-Filter Day Calculation (NEW)**:
- When user enters filter change at cumulative_adjusted_hours = X:
  1. Find episodes where X falls between cumulative_hours[i] and cumulative_hours[i+1]
  2. Linear interpolate between episode_start_times[i] and episode_start_times[i+1]:
     - Fraction: f = (X - cumul_hours[i]) / (cumul_hours[i+1] - cumul_hours[i])
     - filter_change_date = episode_start_times[i] + f × (episode_start_times[i+1] - episode_start_times[i])
  3. Calculate post-filter days: 
     - post_filter_days = (query_end_date - filter_change_date).days
  4. Calculate post-filter adjusted hours:
     - post_filter_adjusted_hours = cumulative_adjusted_hours[-1] - X
  5. New average adjusted hours per day:
     - new_avg_adjusted_hours_per_day = post_filter_adjusted_hours / post_filter_days
- **Why this matters**: Clogged filters force extended fan runtime, inflating the overall avg_adjusted_hours_per_day. After filter replacement, the new average is lower, so RUL projections become more conservative and accurate.

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

## Dashboard Architecture

**Data Flow**:
1. Load pre-computed `sites_data.json` (generated by query_sites.py)
2. For each site, call `recalculate_rul()` function:
   - Apply rolling median smoothing to max_deltas (user-adjustable window)
   - Detect filter changes (≥5°C drops)
   - If filter change confirmed/manual: split data and fit dual regression
   - If no filter change: fit single regression to all data
   - Calculate current ΔT from trend line at current cumulative hours
   - Project RUL as days until trend line hits failure threshold
   - Assign urgency (URGENT/WARNING/OK) based on projected RUL
3. All recalculations happen client-side on Streamlit; original JSON unchanged
4. User interactions (sliders, checkboxes, number inputs) trigger instant recalculation

**Stateful Elements (session_state)**:
- `fc_confirm_{site_id}`: Boolean for filter change confirmation
- `fc_hours_{site_id}`: Filter change time (cumulative adjusted hours)
- `expand_{site_id}`: Track which site cards are expanded
- `fc_adjust_{site_id}`: Temporary input value for manual filter change time

## Dashboard User Interactions

**Main Controls (Sidebar)**:
- **Min episode duration** (10–120 min, default 30): Filters out short episodes; higher values use only sustained cooling periods
- **Min fan speed** (80–100%, default 95%): Only count episodes where fan runs above this threshold
- **Rolling median window** (3–10 episodes, default 5): Smooths noisy max_ΔT values; resets at filter change boundaries
- **ΔT at filter failure** (5–20°C, default 10°C): Adjusts failure threshold; updates urgency/RUL dynamically

**Site Display & Filtering**:
- **Urgency filter**: View by URGENT/WARNING/OK/UNKNOWN (multi-select)
- **Search**: Filter by site ID or name (partial match)
- **Sort options**: By RUL (ascending), Site Name (A-Z), or Urgency + RUL

**Per-Site Expandable Cards**:
- **Filter change detection UI**:
  - If 5°C+ drop detected: Shows message with episode index, hours, before/after ΔT values
    - Checkbox: "Confirm filter change at this site" — triggers dual regression
    - Number input: "Adjust hours" — allows manual correction of detected time
  - If no drop detected: Offers manual entry via checkbox + number input
- **Trend plot**:
  - Blue dashed line: Single regression fit (no filter change)
  - Orange dashed line: Pre-filter-change regression (if dual model)
  - Green dashed line: Post-filter-change regression (if dual model)
  - Red dotted horizontal: Failure ΔT threshold line
  - Gray dotted vertical: Filter change point (if present)

**Model & Equation Display**:
- All sites show their linear regression equation: ΔT = β₀ + β₁ × (adjusted_hours)
- Current ΔT state calculated from this line
- Model architecture explanation in expandable section (covers full methodology, adjusted hours physics, urgency logic, pollution impact)

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
4. **Compute RUL** → Linear regression of max_ΔT vs cumulative adjusted hours, R² ≥ 0.25 required; all sites use same 1-factor model
5. **Fetch air quality** → Weatherbit 90-day historical for sites with coordinates (chunked 30-day requests); skip for sites without coordinates
6. **Fit regression** → For sites WITH air quality data, fit slope ~ adjusted_hours ± PM10 ± PM2.5 (flexible 1/2/3-factor) to quantify pollution impact
7. **Calculate impact context** → For each site with air quality data, compute deviation from population average and predicted pollution effect on degradation rate
8. **Output JSON** → sites_data.json with full metrics, regression results, urgency summary, per-site air quality impact

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
      "ip": "192.168.1.50",
      "success": true,
      "slope": 0.08,
      "rul_days": 18.5,
      "urgency": "WARNING",
      "max_deltas": [2.1, 2.3, 2.5, 2.8, 3.0, 3.2],
      "cumulative_adjusted_hours": [1.5, 3.2, 5.8, 8.2, 11.5, 15.3],
      "episode_start_times": ["2026-07-08T14:30:00", "2026-07-09T10:15:00", "2026-07-10T09:45:00", ...],
      "query_start_date": "2026-07-08",
      "query_end_date": "2026-08-07",
      "episodes_count": 28,
      "r2": 0.87,
      "intercept": 1.8,
      "avg_adjusted_hours_per_day": 0.52,
      "total_adjusted_hours": 45.8,
      "air_quality": {"pm10": 45.2, "pm25": 12.8},
      "latitude": 40.123,
      "longitude": -75.456,
      ...
    },
    "SITE-002": {
      "site_id": "SITE-002",
      "ip": "192.168.1.51",
      "success": true,
      "slope": -0.02,
      "rul_days": null,
      "urgency": "UNKNOWN",
      "max_deltas": [2.1, 1.9, 2.0],
      "cumulative_adjusted_hours": [1.5, 3.2, 5.8],
      "episode_start_times": ["2026-07-15T14:30:00", "2026-07-16T10:15:00", "2026-07-17T09:45:00"],
      "query_start_date": "2026-07-15",
      "query_end_date": "2026-08-07",
      "episodes_count": 8,
      "r2": 0.12,
      "intercept": 2.3,
      "avg_adjusted_hours_per_day": 0.35,
      "total_adjusted_hours": 18.2,
      "air_quality": null,
      "latitude": null,
      "longitude": null,
      ...
    }
  }
}
```

## w2567_analysis.py — CSV-Based Site Analysis

**Purpose**: Analyze HVAC sites with no SSH access but available minute-level CSV data.

**Usage**:
```bash
python3 w2567_analysis.py  # Fetches CSV from GitHub, analyzes, merges into sites_data.json
```

**Expected CSV Format**:
```
System Time,System Current Mode,Indoor Temperature,Outdoor Temperature,Supply Air Temperature,Delta_T,Free-cool Mode,Supply Fan Speed,Damper Position,Damper Status,Compressor Status,Heater Status,Supply Fan 1 Run Time
2026-05-12 15:27:01,6,36.6,24.8,33.1,11.8,1.0,100.0,...
2026-05-12 15:28:01,6,36.7,25.4,33.2,11.2,1.0,99.0,...
```

**Column Requirements**:
- **System Time**: DateTime (will be parsed with pd.to_datetime)
- **Delta_T**: Temperature difference (°C) — core metric for filter clogging
- **Free-cool Mode**: Binary (0/1/True/False) — identifies freecooling operation
- **Supply Fan Speed**: Percentage (0–100%) — used for percentage-adjusted hours calculation
- **Optional**: Damper Position, Heater Status (logged but not used for RUL)

**Workflow**:
1. **Load CSV from GitHub**: Tries GitHub raw content URL, falls back to local path
2. **Parse & clean**: Handle missing values, normalize column names, drop unused fields
3. **Extract episodes**: Groups consecutive rows where fan ≥95% AND free-cool=1
   - Minimum duration: 30 min
   - Records: start_time, end_time, max_ΔT, avg_fan_speed, adjusted_hours
4. **Fit regression**: Linear fit of max_ΔT vs cumulative adjusted hours
   - Detects filter changes as ≥5°C drops in max_ΔT
   - If post-change data too flat: Falls back to full history with filter_change notation
   - Flag sites with insufficient trend (slope ≤ 0 or low R²)
5. **Fetch air quality**: Weatherbit 90-day historical if coordinates available
   - Chunked into 30-day requests to avoid API "Request too large"
   - Calculates PM2.5/PM10 averages
6. **Output JSON**: Merges into sites_data.json with schema:
   ```json
   {
     "site_id": "W2567",
     "site_name": "DERRY ROAD MILTON",
     "ip": "10.252.61.101",
     "data_source": "csv",
     "success": false,  // If insufficient degradation trend
     "episodes_count": 49,
     "max_deltas": [...],
     "cumulative_adjusted_hours": [...],
     "episode_start_times": [...],
     "filter_change": {
       "detected": true,
       "episode_index": 2,
       "change_time": "2026-05-19T12:10:00",
       "pre_change_delta_t": 29.0,
       "post_change_delta_t": 7.5,
       ...
     },
     "analysis_error": "...",
     "analysis_note": "..."
   }
   ```

**Configuration** (tunable in script):
- `FAN_THRESHOLD = 95.0` — Minimum fan % to trigger episode
- `MIN_EPISODE_MINUTES = 30.0` — Minimum episode duration
- `R2_THRESHOLD = 0.25` — Minimum R² to estimate RUL
- `FAILURE_DT = 10.0` — ΔT threshold for filter failure
- `WEATHERBIT_API_KEY = "..."` — Loaded from env or hardcoded

**Adding New CSV Sites**:
1. Upload CSV to GitHub repo: `{SITE_ID}_months_info.csv`
2. Create site metadata in script (or parameterize):
   ```python
   SITE_CONFIG = {
       "site_id": "W9999",
       "site_name": "Example Site",
       "ip": "192.168.x.x",
       "latitude": 45.123,
       "longitude": -75.456,
       "address": "123 Main St"
   }
   ```
3. Run: `python3 w2567_analysis.py`
4. Script auto-merges into sites_data.json
5. Dashboard displays on next load

**Dashboard Integration**:
- CSV sites show even with `success=false` if they have filter changes
- Filter change detected label: "📊 Filter Change Detected"
- Displays episode data, cumulative hours, max deltas for trend visualization
- Air quality context available if coordinates provided

## Data Quality & Insufficient Data Handling

**Negative or Flat Slopes (No Clear Degradation Trend)**:
- **Definition**: Sites with slope ≤ 0 lack clear filter degradation progression
- **Root causes**: 
  - Filter recently installed or replaced (baseline data)
  - Filter degradation data too noisy or inconsistent
  - Site running under variable conditions (humidity, temperature swings)
  - Insufficient freecooling episodes for trend fitting
- **Dashboard treatment**:
  - Show "Data Insufficient for Analysis" warning message
  - Display IP address for identification
  - Show air quality data if available (for reference)
  - Skip RUL projection, trend analysis, and pollution impact blurb
  - Offer filter change confirmation option (if post-change slope becomes positive, site becomes analyzable)
- **Regression analysis exclusion**:
  - Sites with slope ≤ 0 excluded from air quality regression (query_sites.py)
  - Prevents negative slopes from biasing pollution coefficient estimates
  - Only sites with positive slopes used to fit: slope ~ adjusted_hours ± PM10 ± PM2.5
- **Future monitoring**:
  - Re-query negative-slope sites in subsequent runs
  - If degradation trend emerges, site automatically becomes analyzable
  - Users can manually input filter change to "reset" baseline

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

1. **Implement episode timestamp interpolation in dashboard** (IMMEDIATE after script completes):
   - When user enters filter change at cumulative_adjusted_hours = X:
     - Find episodes bracketing X, interpolate exact date from episode_start_times
     - Calculate exact post-filter days from filter_change_date to query_end_date
     - Recalculate avg_adjusted_hours_per_day using post-filter segment only
   - Update RUL projection with new average (will be more conservative after filter replacement)
   - This enables accurate degradation forecasting for sites after manual filter change entry

2. **Validate pollution impact analysis** (ongoing):
   - Inspect which pollutants drive filter degradation (β_PM2.5 > 0 suggests pollution accelerates clogging)
   - β_PM10 < 0 is counterintuitive; possible confounding factors (e.g., sites in polluted areas run cooler)
   - Verify predicted impact values align with observed degradation differences between clean vs polluted sites
   - Monitor: Check if R² improves as more sites accumulate air quality data
   - Consider: add humidity/temperature normalization in future iterations to improve correlation

3. **Dashboard monitoring** (ongoing):
   - Verify RUL values make physical sense based on max delta degradation trends
   - Correlate urgency categories with air quality data: sites in clean air should show slower degradation than polluted sites
   - Watch for outliers: Sites with unexpected degradation rates that don't align with observed pollution levels

4. **Operational** (after current test confirmed stable):
   - Schedule daily runs: `0 */6 * * * cd /home/aillm/hvac_rul_project && python query_sites.py && cd /home/aillm && git add sites_data.json && git commit -m "Auto-update RUL data" && git push`
   - Network expansion: Test running from internal server to reach blocked sites (currently 66% unreachable)
   - Monitor Weatherbit API quota: Track cumulative requests vs 1500/day limit

5. **Future enhancements** (after pipeline stable):
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
