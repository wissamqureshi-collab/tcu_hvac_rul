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

### CSV Structure & Cross-Reference (Critical)

**Location**: `~/tcu_hvac_rul/` (parent directory on Bell laptop, NOT in GitHub)

**sites_inventory.csv** (1020 data rows):
- Columns: Device Name, IP Address, Site, Site Name, Reahcable Via Jmp Server, Device Type, AQue Version, Storage Info, SNMP Version, SNMP Communities, SNMP Result, SNMP Trap IPs, SNMP Walk, Hardware Version
- Used: IP Address → queries, Site → cross-reference key

**sites_inventory_2.csv** (618 data rows):
- Columns: Site ID, Site Name, Longitude, Latitude, Province, Address
- Used: Site ID → cross-reference key, Lat/Lon → Weatherbit queries

**Cross-reference logic**:
- Match Site (sites_inventory.csv) ↔ Site ID (sites_inventory_2.csv) by uppercase normalized string
- Sites WITH coordinates (618): Query InfluxDB + Weatherbit
- Sites WITHOUT coordinates (402): Query InfluxDB only, skip Weatherbit (graceful degradation to 1-factor model)

### Known Issues & Fixes

**Bug #1: Hardcoded Pivot Column** (extract_episodes, line 329)
- Problem: Hardcoded 'display_point' but some sites use 'alias' or 'equipment_id' → KeyError
- Impact: ~960 sites fail; only ~60 sites with 'display_point' tag work
- Fix: Add fallback logic to try ['display_point', 'equipment_id', 'alias']
- Status: IN PROGRESS

**Bug #2: Weatherbit No Data** (likely related to CSV cross-reference)
- Problem: Coordinate matching might be failing silently
- Impact: 0 sites got air quality data in last run
- Status: INVESTIGATING (depends on CSV access)

---

## Token Efficiency

This folder is organized to minimize context pollution:
- Detailed docs moved to Serena memories (persistent, reusable)
- This CLAUDE.md stays focused on quick start + session-specific notes
- Open Claude in this directory: `cd hvac_rul_project && claude`
