#!/usr/bin/env python3
"""
Query all Rogers HVAC sites (1020+) via parallel SSH + InfluxDB.
Extract Mode 3 RUL for each site (linear regression on raw max ΔT data).
Output aggregated sites_data.json.
"""

import os
import sys
import json
import logging
import paramiko
import socket
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np  
from dotenv import load_dotenv

# ============================================================================
# SETUP   
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Load credentials from .env
load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')
if not SITE_PASSWORD:
    logging.error("Missing SITE_PASSWORD in .env file")
    sys.exit(1)

# Configuration
FAN_THRESHOLD = 95.0          # Minimum fan speed to trigger episode
MIN_EPISODE_MINUTES = 30.0    # Minimum episode duration
ROLLING_WINDOW = 5            # Rolling median window size (not used, for reference)
R2_THRESHOLD = 0.25           # Minimum R² to estimate RUL
FAILURE_DT = 10.0             # ΔT threshold for filter failure (data-driven in future)
SSH_TIMEOUT = 30              # SSH connection timeout
QUERY_TIMEOUT = 60            # InfluxDB query timeout
QUERY_DAYS = 90               # Days of historical data to query

# ============================================================================
# LOAD INVENTORY
# ============================================================================

def load_inventory(csv_path):
    """Load site inventory CSV."""
    if not os.path.exists(csv_path):
        logging.error(f"Inventory file not found: {csv_path}")
        sys.exit(1)

    df = pd.read_csv(csv_path)
    # Normalize column names (case-insensitive)
    df.columns = df.columns.str.strip().str.lower()

    sites = []
    for _, row in df.iterrows():
        # Handle different column name variations
        ip = row.get('ip address') or row.get('ip') or None
        site_id = row.get('site') or row.get('device name') or None
        site_name = row.get('site name') or site_id or None

        if ip and site_id:
            sites.append({
                'ip': ip,
                'site_id': site_id,
                'site_name': site_name or site_id,
            })

    logging.info(f"Loaded {len(sites)} sites from inventory")
    return sites


# ============================================================================
# INFLUXDB QUERY (InfluxDB 1.x JSON)
# ============================================================================

def query_site_influxdb(site, password):
    """   
    SSH into site and query InfluxDB 1.x for HVAC data.
    Returns parsed DataFrame or None on error.
    """
    site_ip = site['ip']
    site_id = site['site_id'] 

    try:
        # SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=site_ip, 
            port=22,
            username='plc',
            password=password,
            timeout=SSH_TIMEOUT
        )

        # InfluxDB query (InfluxDB 1.x InfluxQL format)
        query = f"SELECT * FROM hvac WHERE time > now() - {QUERY_DAYS}d"
        cmd = f'curl -s -G "http://localhost:8086/query?db=aque" --data-urlencode "q={query}"'

        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=QUERY_TIMEOUT)
        output = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')

        ssh.close() 

        if error or not output:
            logging.warning(f"{site_id}: Query failed or no output")
            return None

        # Parse JSON response          
        response = json.loads(output)
        if 'results' not in response or not response['results']:
            logging.warning(f"{site_id}: Empty results from InfluxDB")
            return None

        result = response['results'][0]
        if 'series' not in result or not result['series']:
            logging.warning(f"{site_id}: No series in InfluxDB response")
            return None

        series = result['series'][0]
        columns = series.get('columns', [])
        values = series.get('values', [])

        if not columns or not values:
            logging.warning(f"{site_id}: No data in series")
            return None

        # Convert to DataFrame
        df = pd.DataFrame(values, columns=columns)

        # Parse timestamp and convert to datetime
        if 'time' in df.columns: 
            df['time'] = pd.to_datetime(df['time'], utc=True)
        else:
            logging.warning(f"{site_id}: No time column in response")
            return None

        # Convert value to numeric
        if 'value' in df.columns:
            df['value'] = pd.to_numeric(df['value'], errors='coerce')

        logging.info(f"✓ {site_id}: Retrieved {len(df)} rows from InfluxDB")
        return df   

    except paramiko.AuthenticationException:
        logging.error(f"{site_id}: Authentication failed (check plc user/password)")
        return None
    except (socket.timeout, TimeoutError):
        logging.warning(f"{site_id}: Connection timeout (unreachable or slow site)")
        return None
    except paramiko.SSHException as e:
        logging.warning(f"{site_id}: SSH error: {e}")
        return None
    except json.JSONDecodeError: 
        logging.error(f"{site_id}: Failed to parse InfluxDB JSON response")
        return None
    except Exception as e:
        logging.error(f"{site_id}: Unexpected error: {e}")
        return None


# ============================================================================
# EPISODE EXTRACTION
# ============================================================================

def extract_episodes(df):              
    """
    Extract freecooling episodes from HVAC data.

    Episode definition:
    - fan_status >= FAN_THRESHOLD (default 95%)
    - hvac_FREE_COOL_MODE == 1
    - Duration >= MIN_EPISODE_MINUTES

    Returns: list of dicts with episode metadata
    - max_dt: maximum ΔT reached during episode
    - adjusted_runtime_hours: fan_speed²-weighted runtime in hours
    """
    if df is None or len(df) < 2:
        return []

    # Pivot by display_point to get separate columns for each sensor
    df_pivot = df.pivot_table(
        index='time',
        columns='display_point',
        values='value',
        aggfunc='first'
    ).reset_index()

    df_pivot = df_pivot.sort_values('time').reset_index(drop=True)

    # Check for critical sensors (try different naming conventions)
    fan_col = None
    for col_name in ['fan_status', 'fan', 'supply_fan_speed']:
        if col_name in df_pivot.columns:
            fan_col = col_name
            break

    fc_col = None
    for col_name in ['hvac_FREE_COOL_MODE', 'free_cool_mode', 'fc_mode']:
        if col_name in df_pivot.columns:
            fc_col = col_name
            break   

    dt_col = None
    for col_name in ['hvac_DELTA_T', 'delta_t', 'dt']:
        if col_name in df_pivot.columns:
            dt_col = col_name
            break   

    # Need at least fan status, FC mode, and delta T
    if not fan_col or not fc_col or not dt_col:
        missing = []
        if not fan_col:
            missing.append('fan_status')
        if not fc_col:
            missing.append('hvac_FREE_COOL_MODE')
        if not dt_col: 
            missing.append('hvac_DELTA_T')
        logging.warning(f"Missing critical sensors: {missing}")
        return []

    # Convert to numeric
    df_pivot[fan_col] = pd.to_numeric(df_pivot[fan_col], errors='coerce')
    df_pivot[fc_col] = pd.to_numeric(df_pivot[fc_col], errors='coerce')
    df_pivot[dt_col] = pd.to_numeric(df_pivot[dt_col], errors='coerce')

    # Detect episodes: fan >= threshold AND FC mode active
    df_pivot['in_episode'] = (
        (df_pivot[fan_col] >= FAN_THRESHOLD) &
        (df_pivot[fc_col] == 1.0)
    )

    # Group consecutive True values
    df_pivot['episode_id'] = (~df_pivot['in_episode']).cumsum()

    episodes = []   
    for ep_id, group in df_pivot[df_pivot['in_episode']].groupby('episode_id'):
        # Try to keep rows with delta_t and fan data (indoor/outdoor temp optional)
        group = group.dropna(subset=[dt_col, fan_col])

        if len(group) < 2:
            continue

        start_time = group['time'].iloc[0]
        end_time = group['time'].iloc[-1]
        duration_min = (end_time - start_time).total_seconds() / 60.0

        if duration_min < MIN_EPISODE_MINUTES:
            continue

        # Max ΔT during episode (represents peak system strain)
        max_dt = float(group[dt_col].max())

        # Calculate fan_speed²-weighted runtime in minutes, then convert to hours
        fan_speed_pct = group[fan_col].values / 100.0  # Convert % to decimal
        fan_weighting = fan_speed_pct ** 2  # Square for physics-based wear model
        adjusted_runtime_min = np.sum(fan_weighting) * (1.0 / len(group))  # Average weighting * duration
        adjusted_runtime_hours = (duration_min * np.mean(fan_weighting)) / 60.0

        ep_data = { 
            'start_time': start_time,
            'end_time': end_time,
            'duration_min': duration_min,
            'max_dt': max_dt,
            'mean_dt': float(group[dt_col].mean()),
            'adjusted_runtime_hours': adjusted_runtime_hours,
        }

        # Optional fields if available
        if 'indoor_temp' in group.columns:
            ep_data['indoor_temp'] = float(group['indoor_temp'].iloc[0])
        if 'hvac_OUTDOOR_TEMPERATURE' in group.columns:
            ep_data['outdoor_temp'] = float(group['hvac_OUTDOOR_TEMPERATURE'].iloc[0])

        episodes.append(ep_data)

    return episodes


# ============================================================================
# RUL CALCULATION (MODE 3)
# ============================================================================

def compute_rul_mode3(episodes):
    """
    Compute RUL using Mode 3: max ΔT vs percentage-adjusted runtime hours + linear trend.

    X-axis: cumulative adjusted runtime hours (fan_speed² weighted)
    Y-axis: max ΔT per episode (raw, no smoothing)
    Returns: dict with RUL metrics or None if insufficient data
    """
    if len(episodes) < 3:
        return None

    max_deltas = [ep['max_dt'] for ep in episodes]
    adjusted_runtime_hours = [ep['adjusted_runtime_hours'] for ep in episodes]

    # Cumulative adjusted runtime hours
    cumulative_hours = np.cumsum(adjusted_runtime_hours)

    # Linear regression: max ΔT vs cumulative adjusted runtime hours (raw, no rolling median)
    coeffs = np.polyfit(cumulative_hours, max_deltas, 1)
    slope, intercept = coeffs[0], coeffs[1]

    # Calculate R²  
    y_fit = np.polyval(coeffs, cumulative_hours)
    ss_res = np.sum((np.array(max_deltas) - y_fit) ** 2)
    ss_tot = np.sum((np.array(max_deltas) - np.mean(max_deltas)) ** 2)
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Calculate average adjusted runtime hours per day
    if len(episodes) > 1:
        total_days = (episodes[-1]['start_time'] - episodes[0]['start_time']).total_seconds() / 86400.0
        avg_hours_per_day = cumulative_hours[-1] / total_days if total_days > 0 else 1.0
    else:
        avg_hours_per_day = adjusted_runtime_hours[0] if adjusted_runtime_hours else 1.0

    current_dt = max_deltas[-1]
    baseline_dt = max_deltas[0]
    current_hours = cumulative_hours[-1]

    # RUL calculation
    rul_days = None 
    urgency = "UNKNOWN"

    if r2 >= R2_THRESHOLD:
        if slope <= 0:
            # Flat or improving trend
            urgency = "OK"
            rul_days = 999  # Healthy filter
        elif current_dt >= FAILURE_DT:
            # Already at failure threshold
            urgency = "URGENT"
            rul_days = 0
        else:
            # Project adjusted runtime hours until failure
            hours_to_failure = (FAILURE_DT - intercept) / slope if slope > 0 else 999
            remaining_hours = hours_to_failure - current_hours

            # Convert to days using average hours per day
            days_to_failure = remaining_hours / avg_hours_per_day if avg_hours_per_day > 0 else 999

            rul_days = max(0, days_to_failure)

            if rul_days < 14:
                urgency = "URGENT"
            elif rul_days < 30:
                urgency = "WARNING"
            else:   
                urgency = "OK"         

    # Estimate % filter life consumed
    dt_start = float(np.polyval(coeffs, 0))
    dt_range = FAILURE_DT - dt_start
    dt_consumed = current_dt - dt_start
    pct_life = max(0, min(100, (dt_consumed / dt_range * 100))) if dt_range > 0 else 0

    return { 
        'max_deltas': [float(x) for x in max_deltas],
        'cumulative_adjusted_hours': [float(x) for x in cumulative_hours],
        'r2': float(r2),
        'slope': float(slope),
        'rul_days': float(rul_days) if rul_days is not None else None,
        'urgency': urgency,
        'episodes_count': len(episodes),
        'baseline_dt': float(baseline_dt),
        'current_dt': float(current_dt),
        'failure_dt': float(FAILURE_DT),
        'pct_life': float(pct_life),
        'avg_adjusted_hours_per_day': float(avg_hours_per_day),
        'total_adjusted_hours': float(current_hours),
        'last_episode_time': episodes[-1]['start_time'].isoformat(),
    }


# ============================================================================
# PARALLEL QUERY EXECUTOR
# ============================================================================

def query_site_complete(site, password):
    """   
    Complete workflow: SSH → query → extract episodes → compute RUL.
    Returns result dict or error dict.
    """
    site_id = site['site_id'] 
    site_ip = site['ip']

    try:  
        # Query InfluxDB
        df = query_site_influxdb(site, password)
        if df is None:
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error': 'InfluxDB query failed'
            }

        # Extract episodes
        episodes = extract_episodes(df)
        if len(episodes) < 3:
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,      
                'error': f'Insufficient episodes: {len(episodes)}'
            }

        # Compute RUL
        rul_result = compute_rul_mode3(episodes)
        if rul_result is None:
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error': 'RUL calculation failed'
            }

        return {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': True,
            **rul_result
        }

    except Exception as e:
        logging.error(f"{site_id}: Unexpected error in complete workflow: {e}")
        return {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': False,
            'error': str(e)
        } 


def query_all_sites_parallel(sites, password, max_workers=10):
    """
    Query all sites in parallel using ThreadPoolExecutor.

    With 1020 sites and 10 workers, ~102 batches of 10 sites.
    Each batch takes ~30-60 seconds, so total ~50 minutes.
    """
    results = {}
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_site = {
            executor.submit(query_site_complete, site, password): site
            for site in sites
        } 

        for i, future in enumerate(as_completed(future_to_site), 1):
            site = future_to_site[future]
            try:
                result = future.result()
                results[result['site_id']] = result

                if result.get('success'):
                    completed += 1
                    urgency = result.get('urgency', '?')
                    rul = result.get('rul_days')
                    rul_str = f"{rul:.0f}d" if rul is not None else "N/A"
                    logging.info(f"[{i}/{len(sites)}] ✓ {result['site_id']}: {urgency} ({rul_str})")
                else:
                    failed += 1
                    logging.warning(f"[{i}/{len(sites)}] ✗ {result['site_id']}: {result.get('error', 'Unknown 
error')}")

            except Exception as e:
                failed += 1
                logging.error(f"[{i}/{len(sites)}] ✗ {site['site_id']}: {e}")

    return results, completed, failed


# ============================================================================
# MAIN
# ============================================================================

def main():                            
    """Main entry point."""

    inventory_csv = 'sites_inventory.csv'
    output_file = 'sites_data.json'
    max_workers = 10

    logging.info("=" * 80)
    logging.info("Rogers HVAC RUL Multi-Site Query")
    logging.info("=" * 80)

    # Load inventory
    sites = load_inventory(inventory_csv)
    logging.info(f"Starting parallel queries ({max_workers} concurrent workers)...")
    logging.info(f"Estimated time: ~{len(sites) / max_workers / 2:.0f} minutes for {len(sites)} sites")

    # Query all sites
    start_time = datetime.now()
    results, completed, failed = query_all_sites_parallel(sites, SITE_PASSWORD, max_workers)
    elapsed = (datetime.now() - start_time).total_seconds()

    logging.info("\n" + "=" * 80)
    logging.info(f"Query Complete")
    logging.info("=" * 80)
    logging.info(f"Elapsed time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    logging.info(f"Completed: {completed}/{len(sites)} sites")
    logging.info(f"Failed: {failed}/{len(sites)} sites")

    # Aggregate and save
    successful_sites = {k: v for k, v in results.items() if v.get('success')}

    urgency_counts = { 
        'URGENT': sum(1 for r in successful_sites.values() if r.get('urgency') == 'URGENT'),
        'WARNING': sum(1 for r in successful_sites.values() if r.get('urgency') == 'WARNING'),
        'OK': sum(1 for r in successful_sites.values() if r.get('urgency') == 'OK'),
    }

    output = {
        'query_timestamp': datetime.now().isoformat(),
        'query_elapsed_seconds': elapsed,
        'sites_queried': completed,
        'sites_total': len(sites),
        'sites_failed': failed,
        'urgency_summary': urgency_counts,
        'sites': results,
    }

    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    logging.info(f"\nSaved results to {output_file}")

    # Print summary
    logging.info("\n" + "=" * 80)
    logging.info("Urgency Summary")
    logging.info("=" * 80)
    logging.info(f"🔴 URGENT (< 14d):    {urgency_counts['URGENT']:>4} sites")
    logging.info(f"🟡 WARNING (14-30d):  {urgency_counts['WARNING']:>4} sites")
    logging.info(f"🟢 OK (≥ 30d):        {urgency_counts['OK']:>4} sites")
    logging.info(f"⚪ Unknown/Failed:    {failed:>4} sites")


if __name__ == '__main__':
    main()

---
dashboard_unified.py

#!/usr/bin/env python3 
"""
Unified RUL Dashboard for 1020+ Rogers HVAC Sites.
Displays Mode 3 (Max ΔT vs Adjusted Runtime Hours) analysis only.
Linear regression on raw max ΔT data (no rolling median smoothing).
Reads from sites_data.json (generated by query_sites.py).
"""

import json
import streamlit as st
import pandas as pd
import numpy as np  
import plotly.graph_objects as go
from datetime import datetime
from pathlib import Path

st.set_page_config(
    page_title="Rogers HVAC RUL Dashboard",
    page_icon="🌡️ ",
    layout="wide",  
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .stApp { background-color: #f5f6fa; color: #1a202c; }
    body, p, span, div, li, a { color: #1a202c !important; }

    [data-testid="stSidebar"] { background-color: #1a1f2e; }
    [data-testid="stSidebar"] * { color: #e8ecf4 !important; }
    [data-testid="stSidebar"] .stSelectbox label {
        color: #a0aec0 !important; font-size: 0.78rem;
        letter-spacing: 0.08em; text-transform: uppercase;
    }

    .metric-card {
        background: white; border-radius: 10px; padding: 18px 22px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.07);
        border-left: 4px solid #4f7cff;
        min-height: 90px; box-sizing: border-box;
    }
    .metric-card.warn   { border-left-color: #f59e0b; }
    .metric-card.danger { border-left-color: #ef4444; }
    .metric-card .label {
        font-size: 0.72rem; color: #6b7280; text-transform: uppercase;
        letter-spacing: 0.07em; margin-bottom: 4px;
    }
    .metric-card .value { font-size: 1.6rem; font-weight: 700; color: #1a202c; }
    .metric-card .sub   { font-size: 0.78rem; color: #9ca3af; margin-top: 2px; }

    .section-header {
        font-size: 0.72rem; font-weight: 700; color: #1a202c;
        text-transform: uppercase; letter-spacing: 0.1em;
        margin: 24px 0 10px 0; padding-bottom: 6px;
        border-bottom: 1px solid #e5e7eb;
    }
    .mode-description {
        background: white; border-radius: 8px; padding: 12px 16px;
        border-left: 3px solid #4f7cff; margin-bottom: 1rem;
        font-size: 0.82rem; color: #1a202c; line-height: 1.6;
    }

    h1 { color: #1a202c !important; font-weight: 800 !important; letter-spacing: -0.02em !important; }
    h2 { color: #1a202c !important; margin-bottom: 1.5rem !important; margin-top: 0 !important; }
    h3 { color: #1a202c !important; font-weight: 600 !important; }
    h4, h5, h6 { color: #1a202c !important; }

    .stMarkdown { color: #1a202c !important; }
    .stExpander { color: #1a202c !important; background-color: white !important; }
    .streamlit-expanderHeader { color: #1a202c !important; background-color: white !important; }
    .streamlit-expanderHeader:hover { background-color: #f9fafb !important; }
    [data-testid="stExpanderDetails"] { background-color: white !important; color: #1a202c !important; }
    div[data-testid="stExpander"] { background-color: white !important; }
    div[data-testid="stExpander"] p { color: #1a202c !important; }
    div[data-testid="stExpander"] h1,
    div[data-testid="stExpander"] h2,
    div[data-testid="stExpander"] h3 { color: #1a202c !important; }

    #MainMenu, footer, header { visibility: hidden; }
    .block-container { padding-top: 4rem; max-width: 100% !important; }
    [data-testid="column"] { padding: 0 8px !important; }
    .js-plotly-plot { margin-top: 1rem; }
</style>
""", unsafe_allow_html=True)  

# ============================================================================
# LOAD DATA
# ============================================================================

@st.cache_data(ttl=300)
def load_sites_data(json_file='sites_data (1).json'):
    """Load aggregated site data from JSON."""
    if not Path(json_file).exists():
        st.error(f"Data file not found: {json_file}")
        st.info("Run `python3 query_sites.py` on the Bell laptop to generate this file.")
        st.stop()

    with open(json_file) as f:
        data = json.load(f)   

    return data


def recalculate_rul(site_result, new_failure_dt):
    """                                
    Recalculate RUL for a site using a custom failure ΔT threshold.
    Uses adjusted runtime hours (fan_speed² weighted) for prediction.
    Returns updated site_result with new rul_days and urgency.
    """
    site_copy = site_result.copy()

    if not site_result.get('success'):
        return site_copy

    max_deltas = site_result.get('max_deltas', [])
    slope = site_result.get('slope', 0)
    r2 = site_result.get('r2', 0)
    baseline_dt = site_result.get('baseline_dt', 0)
    avg_adjusted_hours_per_day = site_result.get('avg_adjusted_hours_per_day', 1.0)
    total_adjusted_hours = site_result.get('total_adjusted_hours', 0)

    if not max_deltas or len(max_deltas) < 2:
        site_copy['rul_days'] = None
        site_copy['urgency'] = 'UNKNOWN'
        return site_copy

    # Get current (latest) ΔT value
    current_dt = max_deltas[-1]        
    site_copy['current_dt'] = current_dt
    site_copy['failure_dt'] = new_failure_dt

    # If already at or past failure threshold, RUL is 0 or negative
    if current_dt >= new_failure_dt:
        site_copy['rul_days'] = 0
        site_copy['urgency'] = 'URGENT'
        site_copy['pct_life'] = 100
        return site_copy

    # If no degradation trend (R² too low or slope ≤ 0), cannot extrapolate
    if r2 < 0.25 or slope <= 0:
        site_copy['rul_days'] = None
        site_copy['urgency'] = 'NO_DATA'
        if r2 < 0.25 and slope <= 0:
            site_copy['data_reason'] = 'R² < 0.25 (insufficient data quality) and negative/flat trend'
        elif r2 < 0.25:
            site_copy['data_reason'] = 'R² < 0.25 (insufficient data quality)'
        else:
            site_copy['data_reason'] = 'Filter is not degrading or is improving (negative/flat trend)'
        site_copy['pct_life'] = 0
        return site_copy

    # Calculate adjusted runtime hours until failure
    hours_to_failure = (new_failure_dt - current_dt) / slope if slope > 0 else 999
    remaining_hours = hours_to_failure - total_adjusted_hours

    # Convert to days using average adjusted hours per day
    rul_days = remaining_hours / avg_adjusted_hours_per_day if avg_adjusted_hours_per_day > 0 else 999
    site_copy['rul_days'] = max(0, rul_days)

    # Calculate % of life used
    if baseline_dt > 0:
        pct_life = (current_dt - baseline_dt) / (new_failure_dt - baseline_dt) * 100
        site_copy['pct_life'] = max(0, min(100, pct_life))

    # Assign urgency based on RUL
    if rul_days < 14:
        site_copy['urgency'] = 'URGENT'
    elif rul_days < 30:
        site_copy['urgency'] = 'WARNING'
    else: 
        site_copy['urgency'] = 'OK'

    return site_copy


# ============================================================================
# SIDEBAR
# ============================================================================
                                       
with st.sidebar:
    st.markdown("### 🌡️  Rogers HVAC RUL Dashboard")
    st.markdown("---")
    st.markdown('<div class="section-header">Filters</div>', unsafe_allow_html=True)

    # Urgency filter
    urgency_filter = st.multiselect(
        "Urgency Level",
        ['URGENT', 'WARNING', 'OK', 'NO_DATA', 'UNKNOWN'],
        default=['URGENT', 'WARNING', 'OK'],
        label_visibility='visible'
    )

    # Search
    search_term = st.text_input("Search site name/ID", "", label_visibility='visible')

    # Sort order
    sort_by = st.radio(
        "Sort by",
        ['RUL (ascending)', 'Site Name (A-Z)', 'Urgency + RUL'],
        label_visibility='visible'
    )

    # Analysis parameters
    st.markdown('<div class="section-header">Analysis Parameters</div>', unsafe_allow_html=True)
    fan_threshold = st.slider("Min fan speed (%)", 80, 100, 95, step=1, label_visibility='visible')
    min_duration = st.slider("Min episode duration (min)", 10, 120, 30, step=5, label_visibility='visible')
    failure_dt = st.slider("ΔT at filter failure (°C)", 5.0, 20.0, 10.0, step=0.5, label_visibility='visible')

    st.markdown("---") 
    st.markdown('<div style="font-size:0.72rem;color:#9ca3af;line-height:1.6;">'
                '💡 Analysis assumes 90 days of InfluxDB data.<br>'
                'RUL projects when linear trend hits failure threshold (raw max ΔT data).<br>'
                'Green = filter healthy. Yellow = plan replacement soon. Red = replace now.'
                '</div>', unsafe_allow_html=True)


# ============================================================================
# MAIN CONTENT
# ============================================================================

data = load_sites_data()
sites = data['sites']

# Recalculate RUL with custom failure threshold
sites_recalc = {}   
for site_id, site_result in sites.items():
    sites_recalc[site_id] = recalculate_rul(site_result, failure_dt)

st.markdown(f"## 🌡️  Rogers HVAC Filter RUL Status — {len([s for s in sites_recalc.values() if s.get('success')])} Sites
 Analyzed")

# Show current threshold setting
threshold_info = f"**Custom Failure Threshold: {failure_dt}°C**"
if failure_dt != data.get('default_failure_dt', 10.0):
    original_threshold = data.get('default_failure_dt', 10.0)
    threshold_info += f" (Original: {original_threshold}°C)"
st.info(threshold_info)

# Metrics row
col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    urgent_count = sum(1 for s in sites_recalc.values() if s.get('success') and s.get('urgency') == 'URGENT')
    st.markdown(f'<div class="metric-card danger"><div class="label">🔴 URGENT</div><div 
class="value">{urgent_count}</div><div class="sub">< 14 days</div></div>',
                unsafe_allow_html=True)

with col2:
    warning_count = sum(1 for s in sites_recalc.values() if s.get('success') and s.get('urgency') == 'WARNING')
    st.markdown(f'<div class="metric-card warn"><div class="label">🟡 WARNING</div><div 
class="value">{warning_count}</div><div class="sub">14–30 days</div></div>',
                unsafe_allow_html=True)

with col3:
    ok_count = sum(1 for s in sites_recalc.values() if s.get('success') and s.get('urgency') == 'OK')
    st.markdown(f'<div class="metric-card"><div class="label">🟢 OK</div><div class="value">{ok_count}</div><div
class="sub">≥ 30 days</div></div>',
                unsafe_allow_html=True)

with col4:
    no_data_count = sum(1 for s in sites_recalc.values() if s.get('success') and s.get('urgency') == 'NO_DATA')
    st.markdown(f'<div class="metric-card"><div class="label">❓ NO DATA</div><div 
class="value">{no_data_count}</div><div class="sub">Cannot extrapolate</div></div>',
                unsafe_allow_html=True)

with col5:
    failed_count = sum(1 for s in sites_recalc.values() if not s.get('success'))
    st.markdown(f'<div class="metric-card"><div class="label">⚪ FAILED</div><div 
class="value">{failed_count}</div><div class="sub">Query error</div></div>',
                unsafe_allow_html=True)

st.markdown("---")

# ============================================================================
# SITES TABLE
# ============================================================================

st.markdown(f'<div class="section-header">Sites Status Table</div>', unsafe_allow_html=True)

# Prepare table data
table_data = []
for site_id, result in sites_recalc.items():
    if not result.get('success'):
        continue

    # Apply filters
    if result.get('urgency') not in urgency_filter:
        continue

    search_str = f"{site_id} {result.get('site_name', '')}".lower()
    if search_term.lower() and search_term.lower() not in search_str:
        continue

    rul = result.get('rul_days', None)
    urgency = result.get('urgency', '?')

    table_data.append({
        'Site ID': site_id,
        'Site Name': result.get('site_name', '?'),
        'IP Address': result.get('ip', '?'),
        'Urgency': urgency,
        'RUL (days)': f"{rul:.0f}" if rul is not None else "N/A",
        'Episodes': result.get('episodes_count', '?'),
        'R²': f"{result.get('r2', 0):.3f}",
        'Current ΔT (°C)': f"{result.get('current_dt', 0):.1f}",
        'Failure ΔT (°C)': f"{result.get('failure_dt', 0):.1f}",
        'Filter Life Used': f"{result.get('pct_life', 0):.0f}%",
        '_rul_raw': rul if rul is not None else float('inf'),
        '_urgency_rank': {'URGENT': 0, 'WARNING': 1, 'OK': 2, 'NO_DATA': 3, 'UNKNOWN': 4}.get(urgency, 5),
    })

if not table_data:
    st.warning("No sites match filter criteria.")
else:
    df = pd.DataFrame(table_data)

    # Apply sorting 
    if sort_by == 'RUL (ascending)':
        df = df.sort_values('_rul_raw')
    elif sort_by == 'Site Name (A-Z)':
        df = df.sort_values('Site Name')
    elif sort_by == 'Urgency + RUL':
        df = df.sort_values(['_urgency_rank', '_rul_raw'])

    # Drop internal columns
    df = df.drop(columns=['_rul_raw', '_urgency_rank'])

    # Display table
    st.dataframe(df, use_container_width=True, hide_index=True)

st.markdown("---")  

# ============================================================================
# EXPANDABLE SITE DETAILS
# ============================================================================

st.markdown(f'<div class="section-header">Site Details & Trend Analysis</div>', unsafe_allow_html=True)

# Filter sites for expandable view
detail_sites = []
for site_id, result in sorted(sites_recalc.items()):
    if not result.get('success'):
        continue
    if result.get('urgency') not in urgency_filter:
        continue
    search_str = f"{site_id} {result.get('site_name', '')}".lower()
    if search_term.lower() and search_term.lower() not in search_str:
        continue
    detail_sites.append((site_id, result))

# Limit to first 20 for performance
detail_sites = detail_sites[:20] 

if not detail_sites:
    st.info("No sites to display. Adjust filters to see site details.")
else:
    for site_id, result in detail_sites:
        urgency = result.get('urgency', '?')
        rul = result.get('rul_days', '?')
        color_map = {'URGENT': '🔴', 'WARNING': '🟡', 'OK': '🟢', 'UNKNOWN': '⚪'}
        emoji = color_map.get(urgency, '❓')

        rul_display = f"{rul:.0f}d" if isinstance(rul, float) else "N/A"
        with st.expander(f"{emoji} {site_id} — {result.get('site_name', '?')} (RUL: {rul_display})"):
            col1, col2 = st.columns([1, 2])

            with col1: 
                if urgency == 'NO_DATA':
                    st.markdown(f"""
**📍 Site Info**
- IP: `{result.get('ip')}`
- Episodes: {result.get('episodes_count')}

**📈 Trend Analysis**
- R²: {result.get('r2', '?'):.3f}
- Slope: {result.get('slope', '?'):.4f} °C/adj-hour

**⚠️  Unable to Extrapolate**
{result.get('data_reason', 'Insufficient data')}

**🌡️  Temperature**  
- Current ΔT: {result.get('current_dt', '?'):.1f}°C
- Failure ΔT: {result.get('failure_dt', '?'):.1f}°C
- Baseline ΔT: {result.get('baseline_dt', '?'):.1f}°C
""")
                else:
                    st.markdown(f"""
**📍 Site Info**
- IP: `{result.get('ip')}`
- Episodes: {result.get('episodes_count')}

**📈 Trend Analysis**
- R²: {result.get('r2', '?'):.3f}
- Slope: {result.get('slope', '?'):.4f} °C/adj-hour

**⏱️  RUL Estimate** 
- Days: {rul if isinstance(rul, str) else f'{rul:.0f}'}
- % Life Used: {result.get('pct_life', '?'):.0f}%
- Avg Runtime: {result.get('avg_adjusted_hours_per_day', '?'):.2f} hrs/day

**🌡️  Temperature**  
- Current ΔT: {result.get('current_dt', '?'):.1f}°C
- Failure ΔT: {result.get('failure_dt', '?'):.1f}°C
- Baseline ΔT: {result.get('baseline_dt', '?'):.1f}°C
""")

            with col2:
                if urgency != 'NO_DATA':
                    max_deltas = result.get('max_deltas', [])
                    cumulative_adjusted_hours = result.get('cumulative_adjusted_hours', [])

                    if len(max_deltas) > 2 and len(cumulative_adjusted_hours) > 2:
                        fig = go.Figure()

                        fig.add_trace(go.Scatter(
                            x=cumulative_adjusted_hours, y=max_deltas,
                            mode='markers',
                            marker=dict(size=7, color='lightblue', opacity=0.7, line=dict(color='steelblue', width=1)),
                            name='Max ΔT (raw)',
                            hovertemplate='Adjusted Hours: %{x:.1f}<br>Max ΔT: %{y:.2f}°C<extra></extra>'
                        ))

                        fig.add_hline(
                            y=result.get('failure_dt', 10.0),
                            line_dash='dot',
                            line_color='orange',
                            annotation_text=f"Failure: {result.get('failure_dt', 10.0):.1f}°C",
                            annotation_position='right'
                        )

                        if len(cumulative_adjusted_hours) >= 2:
                            r2 = result.get('r2', 0)
                            slope = result.get('slope', 0)
                            coeffs = np.polyfit(cumulative_adjusted_hours, max_deltas, 1)
                            trend_line = np.polyval(coeffs, cumulative_adjusted_hours)
                            fig.add_trace(go.Scatter(
                                x=cumulative_adjusted_hours, y=trend_line,
                                mode='lines',
                                line=dict(color='red', width=2.5, dash='dash'),
                                name=f'Linear Trend (R²={r2:.3f})',
                                hovertemplate='Adjusted Hours: %{x:.1f}<br>Trend: %{y:.2f}°C<extra></extra>'
                            ))

                        fig.update_layout(
                            title=dict(
                                text=f"Max ΔT Degradation Trend — {site_id}",
                                font=dict(color='#1a202c', size=14)
                            ),
                            xaxis_title='Cumulative Adjusted Runtime Hours (fan² weighted)',
                            xaxis=dict(
                                title_font=dict(color='#1a202c', size=12),
                                tickfont=dict(color='#1a202c'),
                                gridcolor='#e5e7eb'
                            ),
                            yaxis_title='ΔT (°C)',
                            yaxis=dict(
                                title_font=dict(color='#1a202c', size=12),
                                tickfont=dict(color='#1a202c'),
                                gridcolor='#e5e7eb'
                            ),
                            height=350,
                            hovermode='x unified',
                            paper_bgcolor='white',
                            plot_bgcolor='#f8f9fb',
                            font=dict(color='#1a202c'),
                            legend=dict(
                                font=dict(color='#1a202c'),
                                bgcolor='rgba(255,255,255,0.8)'
                            )
                        )

                        st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False})
                    else:
                        st.info("Not enough episodes for trend plot.")

st.markdown("---")  
st.markdown(f"""
<div style="text-align:center;font-size:0.75rem;color:#9ca3af;">
    Last updated: {data.get('query_timestamp', 'unknown')}<br>
    Query time: {data.get('query_elapsed_seconds', '?'):.1f}s for {data.get('sites_queried', '?')} sites<br>
    Dashboard: Mode 3 Max ΔT vs Adjusted Runtime Hours (fan² weighted) — Linear regression on raw data
</div>
""", unsafe_allow_html=True)  
