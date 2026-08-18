# Global Project Index

**Last Updated**: 2026-08-18  
**Purpose**: Central hub for all active projects. Each project has its own dedicated folder with focused CLAUDE.md.

---

## ✅ Recent Updates

**HVAC Dashboard (Aug 18)**: Fixed data quality issues, unified display logic, removed display limits
- All sites now show consistent analysis (no 20-site cap)
- Fixed "quadrillion-day RUL" display bug
- Sites with insufficient data show "N/A" (not misleading values)
- Filter change detection unified across all sites
- See `hvac_rul_project/CLAUDE.md` for details

---

## 🎯 Token Efficiency Guidelines

- **Open Claude in project folders**: `cd hvac_rul_project && claude` (keeps context clean)
- **Inspect targeted files first**: Use grep/find before reading large files
- **Skip junk**: `node_modules`, `__pycache__`, `.cache`, etc. are ignored
- **Never commit**: `.env`, credentials, API keys, browser profiles
- **Archive aggressively**: Old simulations/tests go to `archive/` (won't pollute context)

---

## 📁 Active Projects

### 1. **HVAC Filter RUL Prediction** (Primary)
**Location**: `hvac_rul_project/`  
**Status**: 🟢 ACTIVE  
**Quick Start**:
```bash
cd hvac_rul_project
python query_sites.py          # Query 1020 HVAC sites (~50 min)
streamlit run dashboard_unified.py  # View dashboard locally
```

**What It Does**:
- Queries 1020 Rogers HVAC sites in parallel via SSH + InfluxDB (60+ sites with data, 53 displayed)
- Extracts freecooling episodes and computes filter clogging RUL
- Tracks max ΔT vs percentage-adjusted cooling hours (physics-based: fan_speed²)
- 1-factor regression (hours only) for all sites with sufficient data
- Shows "N/A" for sites with insufficient degradation trend
- Displays unified dashboard with dynamic RUL threshold slider
- Supports CSV-based analysis for sites without SSH access (e.g., W2567)

**Key Files** (note: dashboard_unified.py & sites_data.json are at `/home/aillm/` root):
- `hvac_rul_project/query_sites.py` — Parallel SSH query engine
- `/home/aillm/dashboard_unified.py` — Streamlit RUL visualization
- `/home/aillm/sites_data.json` — Pre-computed results (reads this, doesn't generate)
- `hvac_rul_project/CLAUDE.md` — Project-specific docs

**GitHub**: https://github.com/wissamqureshi-collab/tcu_hvac_rul.git  
**Dashboard**: https://tcu-hvac-rul.streamlit.app (auto-deploys from GitHub)

---

### 2. **Safety Detector — Live Monitoring**
**Location**: `safety_detector/`  
**Status**: 🟢 OPERATIONAL  
**Quick Start**:
```bash
cd safety_detector
python live_monitor.py  # Watch camera FTP uploads, detect violations
```

**What It Does**:
- Monitors camera uploads at `/home/plccamera/` (FTP)
- Groups images into bursts, detects safety violations (no helmet, no vest)
- Logs metrics every 10 seconds (CPU, RAM, temp, storage)
- Syncs data to VM (192.168.50.159) for dashboard display
- React dashboard: http://192.168.50.159:5174

**Key Files**:
- `live_monitor.py` — Core detection daemon
- `burst_processor.py` — Burst event handling
- `metrics_logger.py` — Metrics tracking
- `do_rsync.sh` — Sync to VM

**Hardware**: Raspberry Pi 4B (camera: 192.168.50.201)  
**Next Phase**: CM4 + Hailo-8L M.2 acceleration (3x faster)

---

## 📚 Reference & Archive

**Location**: `archive/`

Everything old/reference lives here:
- `presentations/` — Reports, PDFs, old dashboards
- `scripts/` — Setup/install scripts (setup_pi.sh, install_hermes.sh, etc.)
- `reference/` — Code examples (awesome-claude-code, ui-ux, etc.)
- Old simulation/test files, docs, etc.

**Won't pollute context** when you open Claude in active project folders.

---

## 🚀 How to Use This Setup

### To work on a specific project:
```bash
cd /home/aillm/hvac_rul_project
claude  # Context = only hvac_rul_project files + this global CLAUDE.md
```

```bash
cd /home/aillm/safety_detector
claude  # Context = only safety_detector files + this global CLAUDE.md
```

### Each project's CLAUDE.md contains:
- Quick start commands
- File descriptions
- Current status & issues
- Deployment info
- Physics models / algorithms

### When adding new projects:
1. Create folder: `mkdir my_new_project`
2. Create `my_new_project/CLAUDE.md` with project-specific docs
3. Add entry to this global index (below)

---

## 📋 Adding New Projects (Template)

```markdown
### 3. **Project Name**
**Location**: `project_folder/`  
**Status**: 🟢 ACTIVE / 🟡 PAUSED / 🔴 ARCHIVED  
**Quick Start**:
\`\`\`bash
cd project_folder
python script.py
\`\`\`

**What It Does**: [Brief description]
**Key Files**: [List main files]
**Dependencies**: [Tech stack]
**Next Steps**: [What's planned]
```

---

## 💾 Folder Structure

```
/home/aillm/
├── hvac_rul_project/          ← HVAC RUL system (active)
│   ├── query_sites.py
│   ├── dashboard_unified.py
│   ├── requirements.txt
│   ├── CLAUDE.md
│   └── .env (gitignored)
│
├── safety_detector/           ← Live monitoring (active)
│   ├── live_monitor.py
│   ├── burst_processor.py
│   ├── metrics_logger.py
│   └── CLAUDE.md
│
├── archive/                   ← Old stuff (doesn't pollute context)
│   ├── presentations/
│   ├── scripts/
│   ├── reference/
│   └── (old test/sim files)
│
├── burst_events_sim/          ← Detector data (keep if actively monitoring)
├── claude-mem/                ← Your memory system (auto-generated)
└── CLAUDE.md                  ← This file (global index)
```

---

## 🔑 Key Credentials (Local)

- **HVAC project**: `.env` in `hvac_rul_project/` (local, gitignored)
  - `SITE_PASSWORD=sitelogic`
  - `WEATHERBIT_API_KEY=...`

- **Safety Detector**: FTP credentials hardcoded locally (not in GitHub)
  - Pi: 192.168.50.98 (ssh: pi / 123plcgroup)
  - VM: 192.168.50.159 (aillm user)

---

## 📞 Quick Reference

| Need | Command | Location |
|------|---------|----------|
| Update HVAC data | `cd hvac_rul_project && python query_sites.py` | Bell laptop |
| View HVAC dashboard | https://tcu-hvac-rul.streamlit.app | Browser (cloud) |
| Monitor safety detector | http://192.168.50.159:5174 | Browser (internal VM) |
| Check detector logs | SSH to Pi 192.168.50.98 | Pi `/home/pi/` |
| Access old files | `cd archive/` | Local |

---

**Last Reorganized**: 2026-08-03  
**Next Review**: When adding 3rd project or quarterly
