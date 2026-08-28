# HVAC Filter RUL Prediction System — 1020 Rogers Sites

**Project Goal**: Query filter clogging RUL across 1020 HVAC sites via parallel SSH + InfluxDB, display unified dashboard with air quality correlation analysis.

## Quick Start

```bash
pip install -r requirements.txt
python query_sites.py              # Query 1020 sites (~50 min)
streamlit run dashboard_unified.py # View dashboard locally
```

GitHub auto-deploys to: https://tcu-hvac-rul.streamlit.app

---

## Detailed Documentation

For architecture, setup, file descriptions, and deep dives, see Serena memories:
- `mem:hvac/architecture` — RUL model, physics, filter change detection
- `mem:hvac/setup` — Prerequisites, .env, configuration
- `mem:hvac/files` — Key files and locations
- `mem:hvac/dashboard_ui` — User interactions, controls, display
- `mem:hvac/data_quality` — Insufficient data handling, safeguards
- `mem:hvac/issues` — Known issues and mitigations
- `mem:hvac/csv_analysis` — CSV-based site analysis (w2567_analysis.py)
- `mem:hvac/query_implementation` — How query_sites.py works
- `mem:hvac/roadmap` — Future enhancements

---

## Current Session Notes (Aug 25)

### Query Run Results — Aug 25, 2026

**Test Run Output**:
```
🔴 URGENT (< 14d):       2 sites
🟡 WARNING (14-30d):     0 sites
🟢 OK (≥ 30d):           3 sites
⚪ Unknown/Failed:     971 sites
🌍 With Air Quality:     0 sites
📅 With Episode Dates:   49 sites
```

**Analysis**:
- **5 sites successful** (2 URGENT + 3 OK): ≥3 episodes extracted, RUL calculated
- **49 sites partial**: Extracted 1-2 episodes each (fails ≥3 episode minimum for RUL)
- **971 sites failed**: Never reached episode extraction (failed at SSH/InfluxDB/parsing)

**Status**: Script has tag extraction fix deployed, but 95% of sites still failing at data retrieval phase.

### Root Cause Investigation (In Progress)

**Issue**: 971 sites not returning InfluxDB data despite:
- Manual SSH confirmed working for tested sites
- Tag metadata extraction fix deployed (Aug 24)
- Fallback column naming chains in place

**Hypotheses** (priority order):
1. **InfluxDB not running** on most sites OR database empty/outside 90-day window
2. **Authentication failing** for most sites (1 manual test worked, but script may have different credentials or timeout)
3. **Response structure different** than expected (partial series data, error field, etc.)
4. **CSV cross-reference issue** (site matching failing silently, but this shouldn't prevent InfluxDB query)

**Diagnostic Needed**:
- Run manual curl test from site via SSH to see raw InfluxDB response structure
- Check if response has 'series' field, what tags are present, what columns exist
- Verify one of the 49 successful sites vs one of the 971 failing sites

### CSV Structure & Cross-Reference

**Location**: `~/tcu_hvac_rul/` (parent directory on Bell laptop, NOT in GitHub)

**sites_inventory.csv** (1020 data rows):
- Columns: Device Name, IP Address, Site, Site Name, Reachable Via Jmp Server, Device Type, AQue Version, Storage Info, SNMP Version, SNMP Communities, SNMP Result, SNMP Trap IPs, SNMP Walk, Hardware Version
- Used: IP Address → SSH queries, Site → cross-reference key

**sites_inventory_2.csv** (618 data rows):
- Columns: Site ID, Site Name, Longitude, Latitude, Province, Address
- Used: Site ID → cross-reference key, Lat/Lon → Weatherbit queries

**Cross-reference logic**:
- Match Site (sites_inventory.csv) ↔ Site ID (sites_inventory_2.csv) by uppercase normalized string
- Sites WITH coordinates (618): Query InfluxDB + Weatherbit
- Sites WITHOUT coordinates (402): Query InfluxDB only, skip Weatherbit (graceful degradation to 1-factor model)

### Next Steps

1. **Run diagnostic**: Use manual SSH + curl to test raw InfluxDB response from one failing site
   - Pick one of the 49 successful sites (verify it returns data)
   - Pick one of the 971 failing sites (see what's different)
   - Compare response structure, see if 'series' field exists, what tags/columns are present

2. **If InfluxDB returns no data**: Issue is data availability, not code
   - Check if database exists: `influx -host localhost`
   - Check measurement: `SELECT * FROM hvac LIMIT 1`
   - Check time range: Data must be within past 90 days

3. **If InfluxDB returns data but script still fails**: Debug response parsing
   - Add logging to `query_site_influxdb()` to print raw JSON response for failing site
   - Verify tags are present in series metadata
   - Verify columns include required fields (time, value, display_point/equipment_id/alias)

4. **If all checks pass**: May be script crash or exception handling swallowing errors
   - Re-run with `logging.basicConfig(level=logging.DEBUG)` for verbose output
   - Or add print statements to trace execution flow for one site

---

## Aug 27: Full 1020-Site Query Results & Action Items

### Query Results (After Refactoring with Error Categorization)
- **Success**: 4 sites (2 URGENT, 2 OK)
- **Failed**: 1016 sites with detailed failure reasons

### Failure Breakdown
| Reason | Count | Impact | Next Step |
|--------|-------|--------|-----------|
| SSH_UNREACHABLE | 436 | Sites timing out on SSH (30s limit) | Investigate: network slow, parallel load, firewall |
| INFLUXDB_OFFLINE | 294 | InfluxDB not running/responding | Check: InfluxDB process status on sites |
| **SSH_AUTH_FAILED** | **159** | **Need public key auth, not password!** | **CRITICAL: Update auth mechanism** |
| MISSING_SENSORS | 36 | Different InfluxDB column structure | Handle: graceful degradation for variant structures |
| INSUFFICIENT_DEGRADATION | 26 | Valid data but filter not degrading | OK: report as warning, not error |
| STALE_DATA | 5 | No data since 2021 | OK: properly detected and reported |

### Critical Issues to Address Next Session

1. **SSH Authentication (159 sites require public key)**
   - Error: `Bad authentication type; allowed types: ['publickey']`
   - These sites reject password auth entirely
   - **Action**: Implement public key authentication fallback in query_site_influxdb()
   - Need: Private key file path, passphrase (if needed)
   - Fallback: Try password first, if auth fails with "publickey required", try public key

2. **SSH Timeouts (436 sites, 30s limit)**
   - Possible causes:
     - Network latency from Bell laptop to sites
     - Parallel execution (10 workers) overwhelming network
     - Sites with slow SSH servers
   - **Action**: Test increasing SSH_TIMEOUT to 60s or reduce max_workers to 5
   - **Action**: Add connection retry logic (try twice before giving up)

3. **InfluxDB Offline (294 sites)**
   - Could be: InfluxDB crashed, network unreachable, port blocked
   - **Action**: Implement ping test before full query (already in code, but may need timeout adjustment)
   - **Action**: Add retry logic for transient failures

4. **Format String Bug (FIXED)**
   - ✅ Fixed in commit: Line 785 now safely handles None rul_days
   - Bug: `{None:.1f}d` → formatted as "N/A"

### Backwards Compatibility
- All failures now include `error_code` and `error_message`
- Old `error` key still present for dashboard compatibility
- Stale data properly detected via `last_data_year` field

### Success Rate Analysis
- 4/1020 sites successfully producing RUL (0.4%)
- This is LOW but expected given:
  - 159 sites need different auth method
  - 294 sites have offline InfluxDB
  - 436 sites timing out on SSH
  - 36 sites have incompatible data structure
  - 5 sites have stale data
  - Once these are fixed: likely 60-100+ sites should work

---

## Aug 28: Column Matching Fix for MISSING_SENSORS Sites

### Problem
36 sites failed with MISSING_SENSORS because InfluxDB column names varied:
- Expected exact names: `fan_status`, `hvac_FREE_COOL_MODE`, `hvac_DELTA_T`
- Actual found: `fan_speed_percent`, `damper_1_status`, `supply_air_temprature`, `indoor_temp`, etc.

### Solution Implemented
Added intelligent 2-stage column discovery system:

1. **Stage 1: Exact/Common Name Matching**
   - 5-10 known variations per sensor type (fan, free_cool, delta_t)
   - Fast, safe, case-insensitive lookup
   - Logs which pattern matched

2. **Stage 2: Keyword Pattern Matching with Safeguards**
   - Fan: requires `fan` + at least one of (speed, status, rpm, percent)
   - Free-cool: requires both `free` AND `cool`, or accepts damper columns as proxy
   - Delta-T: requires `delta` + `t`, or temp + diff (avoids false positives)

3. **Smart Transformations**
   - **Delta-T calculation**: If only `supply_air_temperature` + `outdoor_temperature` exist, calculate delta-T from them
   - **Damper mapping**: If only damper columns exist, use `damper_status == 1` as free-cool indicator

### Newly Discovered Column Patterns (36 Sites)
- **Fan**: `fan_speed_percent`, `fan_percent`, etc.
- **Free-cool proxy** (26 sites): 
  - `damper_1_status` (19 sites)
  - `damper_status` (7 sites)
  - `hvac_DAMPER_POSITION`, `hvac_DAMPER_STATUS` (variants)
- **Temperature components** (no direct delta-T):
  - `supply_air_temprature` (NOTE: typo in actual data, 26 sites!)
  - `supply_air_temperature`, `hvac_SUPPLY_AIR_TEMPERATURE`
  - `hvac_OUTDOOR_TEMPERATURE`, `indoor_temp`, `outdoor_temp`

### Expected Results After Fix
- **Before**: 36 MISSING_SENSORS failures
- **After (estimated)**:
  - ~10-15 sites now pass (damper + temperature data available)
  - ~10-15 sites become INSUFFICIENT_DEGRADATION (valid data, filter not degrading)
  - ~5-10 sites remain MISSING_SENSORS (data too divergent)

### Code Changes
**File**: `query_sites.py`
- **New functions**:
  - `find_sensor_column(df, sensor_type)` (lines 455-580) — Main 2-stage discovery
  - `calculate_delta_t(df, supply_col, outdoor_col)` (lines 582-597) — Temperature calculation
  - `map_damper_to_free_cool(df, damper_col)` (lines 600-614) — Damper status mapping
- **Updated**: `extract_episodes()` (lines 671-684 discovery, 739-769 transformations)

### Testing & Logging
- All column discoveries logged with match type (EXACT_MATCH, KEYWORD_MATCH, PROXY, etc.)
- Detailed diagnostic output on MISSING_SENSORS failure (shows all available columns)
- Easy to refine patterns based on actual column names found

### Commit
```
Handle different InfluxDB column structures across 36 MISSING_SENSORS sites
- Added find_sensor_column() with 2-stage discovery (exact + keyword patterns)
- Added calculate_delta_t() for temperature component columns
- Added map_damper_to_free_cool() for damper-based free-cool detection
- Updated extract_episodes() to apply transformations transparently
- Expected: ~10-15 MISSING_SENSORS → pass after fix
```

**Full implementation details**: See `mem:hvac/column_matching_implementation`

---

## Token Efficiency

This folder is organized to minimize context pollution:
- Detailed docs moved to Serena memories (persistent, reusable)
- This CLAUDE.md stays focused on quick start + session-specific notes
- Open Claude in this directory: `cd hvac_rul_project && claude`
