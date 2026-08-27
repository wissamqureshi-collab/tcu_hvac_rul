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

## Token Efficiency

This folder is organized to minimize context pollution:
- Detailed docs moved to Serena memories (persistent, reusable)
- This CLAUDE.md stays focused on quick start + session-specific notes
- Open Claude in this directory: `cd hvac_rul_project && claude`
