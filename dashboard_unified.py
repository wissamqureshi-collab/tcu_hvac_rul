#!/usr/bin/env python3
"""
Unified RUL Dashboard for 1020+ Rogers HVAC Sites.
Fresh rewrite with guaranteed working sidebar.
"""

import json
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime
from pathlib import Path

st.set_page_config(page_title="Rogers HVAC RUL Dashboard", page_icon="🌡️", layout="wide")

# ============================================================================
# CSS STYLING
# ============================================================================
st.markdown("""
<style>
  .stApp { background-color: #ffffff; }

  /* SIDEBAR - Dark blue background */
  [data-testid="stSidebar"] { background-color: #1e3a5f !important; }
  [data-testid="stSidebar"] .stMarkdown { color: #ffffff !important; }
  [data-testid="stSidebar"] .stMarkdown * { color: #ffffff !important; }
  [data-testid="stSidebar"] label { color: #e0e7ff !important; font-weight: 600 !important; }
  [data-testid="stSidebar"] .stSlider label { color: #e0e7ff !important; }
  [data-testid="stSidebar"] .stSlider { color: #ffffff !important; }
  [data-testid="stSidebar"] .stRadio label { color: #e0e7ff !important; }
  [data-testid="stSidebar"] .stRadio span { color: #ffffff !important; }
  [data-testid="stSidebar"] .stMultiSelect label { color: #e0e7ff !important; }
  [data-testid="stSidebar"] .stTextInput label { color: #e0e7ff !important; }
  [data-testid="stSidebar"] input { background-color: #2d4a7a !important; color: #ffffff !important; border-color: #4f7cff !important; }
  [data-testid="stSidebar"] input::placeholder { color: #a5b4fc !important; }

  /* MAIN CONTENT - Dark text */
  body, .stApp, .stMarkdown, .stMarkdown * { color: #1a202c !important; }
  .stMarkdown p, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 { color: #1a202c !important; }
  h1, h2, h3 { color: #1a202c !important; }

  /* DROPDOWNS - White background, black text */
  .stSelectbox { color: #1a202c !important; }
  .stSelectbox label { color: #1a202c !important; font-weight: 600 !important; }
  .stSelectbox [role="button"] { background-color: #ffffff !important; color: #1a202c !important; border: 1px solid #d1d5db !important; }
  .stSelectbox [role="button"]:hover { background-color: #f9fafb !important; }

  .stMultiSelect { color: #1a202c !important; }
  .stMultiSelect label { color: #1a202c !important; font-weight: 600 !important; }
  .stMultiSelect [role="button"] { background-color: #ffffff !important; color: #1a202c !important; border: 1px solid #d1d5db !important; }
  .stMultiSelect [role="button"]:hover { background-color: #f9fafb !important; }

  /* DROPDOWN POPOVER - Prevent black */
  div[data-baseweb="popover"] { background-color: #ffffff !important; }
  div[data-baseweb="popover"] * { background-color: #ffffff !important; color: #1a202c !important; }
  div[role="listbox"] { background-color: #ffffff !important; color: #1a202c !important; }
  div[role="option"] { background-color: #ffffff !important; color: #1a202c !important; }
  div[role="option"]:hover { background-color: #f0f4ff !important; color: #1a202c !important; }

  /* TEXT INPUT */
  .stTextInput input { background-color: #ffffff !important; color: #1a202c !important; border: 1px solid #d1d5db !important; }
  .stTextInput input:focus { border-color: #4f7cff !important; background-color: #ffffff !important; color: #1a202c !important; }

  /* EXPANDERS */
  .stExpander { background-color: #ffffff !important; border: 1px solid #e5e7eb !important; }
  [data-testid="stExpander"] > button { color: #1a202c !important; font-weight: 600 !important; background-color: #ffffff !important; }
  [data-testid="stExpander"] > button p { color: #1a202c !important; }

  /* HIDE DEFAULT ELEMENTS */
  #MainMenu, footer, header { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ============================================================================
# SIDEBAR - CREATED IMMEDIATELY
# ============================================================================
st.sidebar.markdown("### ⚙️ Analysis Controls")

min_duration = st.sidebar.slider("Min episode duration (min)", 10, 120, 30, step=5)
fan_threshold = st.sidebar.slider("Min fan speed (%)", 80, 100, 95, step=1)
rolling_window = st.sidebar.slider("Rolling median window (episodes)", 3, 10, 5, step=1)
failure_dt = st.sidebar.slider("ΔT at filter failure (°C)", 5.0, 20.0, 10.0, step=0.5)

st.sidebar.markdown("---")
st.sidebar.markdown("### 🔍 Filter & Sort")

urgency_filter = st.sidebar.multiselect(
    "Urgency Level",
    ['URGENT', 'WARNING', 'OK', 'UNKNOWN'],
    default=['URGENT', 'WARNING', 'OK']
)

search_term = st.sidebar.text_input("Search site name/ID", "")

sort_by = st.sidebar.radio(
    "Sort by",
    ['RUL (ascending)', 'Site Name (A-Z)', 'Urgency + RUL']
)

# ============================================================================
# MAIN CONTENT
# ============================================================================

st.markdown("<h1 style='color: #1a202c;'>🌡️ Rogers HVAC Filter RUL Dashboard</h1>", unsafe_allow_html=True)

# Load data
@st.cache_data(ttl=300)
def load_sites_data(json_file='sites_data.json'):
    if not Path(json_file).exists():
        st.error(f"Data file not found: {json_file}")
        st.stop()
    with open(json_file) as f:
        return json.load(f)

data = load_sites_data()
sites = data.get('sites', {})

# Functions
def recalculate_rul(site_result, new_failure_dt):
    site_copy = site_result.copy()
    if not site_result.get('success'):
        return site_copy

    current_dt = site_result.get('current_dt', 0)
    slope = site_result.get('slope', 0)
    r2 = site_result.get('r2', 0)
    baseline_dt = site_result.get('baseline_dt', 0)
    intercept = site_result.get('intercept', 0)

    if current_dt <= 0 or slope <= 0:
        site_copy['rul_days'] = None
        site_copy['urgency'] = 'UNKNOWN'
        return site_copy

    site_copy['failure_dt'] = new_failure_dt

    if current_dt >= new_failure_dt:
        site_copy['rul_days'] = 0
        site_copy['urgency'] = 'URGENT'
        site_copy['pct_life'] = 100
        return site_copy

    if r2 < 0.25:
        site_copy['rul_days'] = 999
        site_copy['urgency'] = 'OK'
        site_copy['pct_life'] = 0
        return site_copy

    avg_hours_per_day = site_result.get('avg_adjusted_hours_per_day', 1.0)
    hours_to_failure = (new_failure_dt - intercept) / slope if slope > 0 else 999
    remaining_hours = hours_to_failure - site_result.get('total_adjusted_hours', 0)
    rul_days = remaining_hours / avg_hours_per_day if avg_hours_per_day > 0 else 999
    site_copy['rul_days'] = max(0, rul_days)

    if baseline_dt > 0:
        pct_life = (current_dt - baseline_dt) / (new_failure_dt - baseline_dt) * 100
        site_copy['pct_life'] = max(0, min(100, pct_life))

    if rul_days < 14:
        site_copy['urgency'] = 'URGENT'
    elif rul_days < 30:
        site_copy['urgency'] = 'WARNING'
    else:
        site_copy['urgency'] = 'OK'

    return site_copy

# Recalculate with custom failure threshold
sites_recalc = {}
for site_id, site_result in sites.items():
    sites_recalc[site_id] = recalculate_rul(site_result, failure_dt)

# Filter and sort
filtered_sites = []
for site_id, result in sites_recalc.items():
    if not result.get('success'):
        continue
    if result.get('urgency') not in urgency_filter:
        continue
    if search_term.lower() and search_term.lower() not in (result.get('site_id', '') + result.get('site_name', '')).lower():
        continue
    filtered_sites.append((site_id, result))

# Sort
if sort_by == 'RUL (ascending)':
    filtered_sites.sort(key=lambda x: x[1].get('rul_days', 999))
elif sort_by == 'Site Name (A-Z)':
    filtered_sites.sort(key=lambda x: x[1].get('site_id', ''))
else:  # Urgency + RUL
    urgency_order = {'URGENT': 0, 'WARNING': 1, 'OK': 2, 'UNKNOWN': 3}
    filtered_sites.sort(key=lambda x: (urgency_order.get(x[1].get('urgency', 'UNKNOWN'), 4), x[1].get('rul_days', 999)))

success_count = len([s for s in sites_recalc.values() if s.get('success')])
st.markdown(f"<h2 style='color: #1a202c;'>Status — {success_count} Sites Analyzed</h2>", unsafe_allow_html=True)

# Display methodology
with st.expander("📚 **Model Architecture & Methodology**", expanded=False):
    st.markdown("""
<div style="color: #1a202c; line-height: 1.9;">

### Filter Degradation Tracking
We track filter clogging by monitoring temperature differential (ΔT) during freecooling episodes. As filters clog, ΔT increases because air must be forced harder through restricted filter media.

### Linear RUL Model
- **Equation:** ΔT = β₀ + β₁ × (cumulative adjusted hours)
- **β₀ (intercept):** baseline ΔT when filter is clean
- **β₁ (slope):** degradation rate per adjusted hour
- **Higher slope = faster clogging = shorter RUL**

### Adjusted Hours Calculation
Air resistance ∝ fan_speed². We account for this:
- **Formula:** adjusted_hours = duration × (fan_speed_pct)²
- 1 hour @ 100% = 1.0 adjusted hours
- 1 hour @ 50% = 0.25 adjusted hours

### RUL Projection
- **Failure point:** When ΔT reaches threshold (default 10°C)
- **RUL (days)** = (Failure ΔT - Current ΔT) / Slope ÷ (Avg Adjusted Hours/Day)

### Urgency Classification
- 🔴 **URGENT:** RUL < 14 days
- 🟡 **WARNING:** RUL 14–30 days
- 🟢 **OK:** RUL ≥ 30 days

</div>
    """, unsafe_allow_html=True)

# Display sites
if not filtered_sites:
    st.info("No sites match your filters. Adjust settings to see results.")
else:
    st.markdown(f"### {len(filtered_sites)} Sites Matching Filters")

    for site_id, result in filtered_sites[:50]:  # Limit to 50 for performance
        urgency = result.get('urgency', 'UNKNOWN')
        rul = result.get('rul_days', 0)

        if urgency == 'URGENT':
            emoji = "🔴"
        elif urgency == 'WARNING':
            emoji = "🟡"
        else:
            emoji = "🟢"

        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            st.markdown(f"**{emoji} {result.get('site_id', site_id)}** — {result.get('site_name', 'N/A')}")
        with col2:
            st.markdown(f"**RUL:** {rul:.1f}d" if rul is not None else "**RUL:** —")
        with col3:
            st.markdown(f"**Status:** {urgency}")

st.markdown("---")
st.caption("Data updated: " + data.get('query_timestamp', 'N/A'))
