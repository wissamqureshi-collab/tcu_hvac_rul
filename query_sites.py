#!/usr/bin/env python3
"""
Query all Rogers HVAC sites (1020+) via parallel SSH + InfluxDB.
Extract Mode 3 (rolling median) RUL for each site.
Output aggregated sites_data.json.
"""

import os
import sys
import json
import logging
import paramiko
import socket
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from scipy.stats import linregress

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

WAQI_TOKEN = os.getenv('WAQI_TOKEN')
if not WAQI_TOKEN:
    logging.warning("WAQI_TOKEN not found in .env - WAQI analysis will be skipped")

WEATHERBIT_API_KEY = os.getenv('WEATHERBIT_API_KEY')
if not WEATHERBIT_API_KEY:
    logging.warning("WEATHERBIT_API_KEY not found in .env - Weatherbit air quality analysis will be skipped")

# Configuration
FAN_THRESHOLD = 95.0          # Minimum fan speed to trigger episode
MIN_EPISODE_MINUTES = 30.0    # Minimum episode duration
ROLLING_WINDOW = 5            # Rolling median window size
R2_THRESHOLD = 0.25           # Minimum R² to estimate RUL
FAILURE_DT = 10.0             # ΔT threshold for filter failure (data-driven in future)
SSH_TIMEOUT = 30              # SSH connection timeout
QUERY_TIMEOUT = 60            # InfluxDB query timeout
QUERY_DAYS = 90               # Days of historical data to query

# ============================================================================
# LOAD INVENTORY
# ============================================================================

def load_inventory(csv_path, coords_csv_path=None):
    """
    Load site inventory CSV and optionally merge with coordinates CSV.

    coords_csv_path should have columns: Site ID, Latitude, Longitude
    Sites without coordinates will have lat=None, lon=None.
    """
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
                'latitude': None,
                'longitude': None,
            })

    # Load coordinates if provided
    if coords_csv_path and os.path.exists(coords_csv_path):
        coords_df = pd.read_csv(coords_csv_path)
        coords_df.columns = coords_df.columns.str.strip().str.lower()

        # Normalize Site ID column name
        site_id_col = None
        for col in ['site id', 'site_id', 'site']:
            if col in coords_df.columns:
                site_id_col = col
                break

        if site_id_col:
            coords_dict = {}
            for _, row in coords_df.iterrows():
                sid = str(row[site_id_col]).strip()
                coords_dict[sid] = {
                    'latitude': row.get('latitude'),
                    'longitude': row.get('longitude'),
                }

            # Merge coordinates into sites
            sites_with_coords = 0
            for site in sites:
                if site['site_id'] in coords_dict:
                    site['latitude'] = coords_dict[site['site_id']]['latitude']
                    site['longitude'] = coords_dict[site['site_id']]['longitude']
                    if site['latitude'] and site['longitude']:
                        sites_with_coords += 1

            logging.info(f"Loaded coordinates for {sites_with_coords}/{len(sites)} sites from {coords_csv_path}")

    logging.info(f"Loaded {len(sites)} sites from inventory")
    return sites


# ============================================================================
# AIR QUALITY QUERY (WAQI)
# ============================================================================



# PM10 and PM2.5 AQI to concentration conversion tables (from AQICN calculator)
# Format: concentration (μg/m³) -> AQI value
PM10_CONVERSION = {
    0: 0, 2: 2, 4: 4, 6: 5, 8: 7, 10: 9, 12: 11, 14: 13, 16: 15, 18: 16, 20: 18,
    22: 20, 24: 22, 26: 24, 28: 25, 30: 27, 32: 29, 34: 31, 36: 33, 38: 35, 40: 36,
    42: 38, 44: 40, 46: 42, 48: 44, 50: 45, 52: 47, 54: 49, 56: 51, 58: 52, 60: 53,
    62: 54, 64: 55, 66: 56, 68: 57, 70: 58, 72: 59, 74: 60, 76: 61, 78: 62, 80: 63,
    82: 64, 84: 65, 86: 66, 88: 67, 90: 68, 92: 69, 94: 70, 96: 71, 98: 72, 100: 73,
    102: 74, 104: 75, 106: 76, 108: 77, 110: 78, 112: 79, 114: 80, 116: 81, 118: 82, 120: 83,
    122: 84, 124: 85, 126: 86, 128: 87, 130: 88, 132: 89, 134: 90, 136: 91, 138: 92, 140: 93,
    142: 94, 144: 95, 146: 96, 148: 97, 150: 98, 152: 99, 154: 100, 156: 101, 158: 102, 160: 103,
    162: 104, 164: 105, 166: 106, 168: 107, 170: 108, 172: 109, 174: 110, 176: 111, 178: 112, 180: 113,
    182: 114, 184: 115, 186: 116, 188: 117, 190: 118, 192: 119, 194: 120, 196: 121, 198: 122, 200: 123,
    202: 124, 204: 125, 206: 126, 208: 127, 210: 128, 212: 129, 214: 130, 216: 131, 218: 132, 220: 133,
    222: 134, 224: 135, 226: 136, 228: 137, 230: 138, 232: 139, 234: 140, 236: 141, 238: 142, 240: 143,
    242: 144, 244: 145, 246: 146, 248: 147, 250: 148, 252: 149, 254: 150, 256: 151, 258: 152, 260: 153,
    262: 154, 264: 155, 266: 156, 268: 157, 270: 158, 272: 159, 274: 160, 276: 161, 278: 162, 280: 163,
    282: 164, 284: 165, 286: 166, 288: 167, 290: 168, 292: 169, 294: 170, 296: 171, 298: 172, 300: 173,
    302: 174, 304: 175, 306: 176, 308: 177, 310: 178, 312: 179, 314: 180, 316: 181, 318: 182, 320: 183,
    322: 184, 324: 185, 326: 186, 328: 187, 330: 188, 332: 189, 334: 190, 336: 191, 338: 192, 340: 193,
    342: 194, 344: 195, 346: 196, 348: 197, 350: 198, 352: 199, 354: 200, 356: 201, 358: 204, 360: 207,
    362: 210, 364: 213, 366: 216, 368: 219, 370: 221, 372: 224, 374: 227, 376: 230, 378: 233, 380: 236,
    382: 239, 384: 241, 386: 244, 388: 247, 390: 250, 392: 253, 394: 256, 396: 259, 398: 261
}

PM25_CONVERSION = {
    0: 0, 1: 4, 2: 8, 3: 13, 4: 17, 5: 21, 6: 25, 7: 29, 8: 33, 9: 38, 10: 42,
    11: 46, 12: 50, 13: 52, 14: 54, 15: 56, 16: 59, 17: 61, 18: 63, 19: 65, 20: 67,
    21: 69, 22: 71, 23: 73, 24: 76, 25: 78, 26: 80, 27: 82, 28: 84, 29: 86, 30: 88,
    31: 90, 32: 93, 33: 95, 34: 97, 35: 99, 36: 101, 37: 104, 38: 106, 39: 109, 40: 111,
    41: 114, 42: 116, 43: 119, 44: 121, 45: 124, 46: 126, 47: 129, 48: 131, 49: 134, 50: 136,
    51: 139, 52: 141, 53: 144, 54: 146, 55: 149, 56: 150, 57: 151, 58: 151, 59: 152, 60: 152,
    61: 153, 62: 153, 63: 154, 64: 154, 65: 155, 66: 156, 67: 156, 68: 157, 69: 157, 70: 158,
    71: 158, 72: 159, 73: 159, 74: 160, 75: 160, 76: 161, 77: 161, 78: 162, 79: 162, 80: 163,
    81: 163, 82: 164, 83: 164, 84: 165, 85: 166, 86: 166, 87: 167, 88: 167, 89: 168, 90: 168,
    91: 169, 92: 169, 93: 170, 94: 170, 95: 171, 96: 171, 97: 172, 98: 172, 99: 173, 100: 173,
    101: 174, 102: 174, 103: 175, 104: 176, 105: 176, 106: 177, 107: 177, 108: 178, 109: 178, 110: 179,
    111: 179, 112: 180, 113: 180, 114: 181, 115: 181, 116: 182, 117: 182, 118: 183, 119: 183, 120: 184,
    121: 184, 122: 185, 123: 186, 124: 186, 125: 187, 126: 187, 127: 188, 128: 188, 129: 189, 130: 189,
    131: 190, 132: 190, 133: 191, 134: 191, 135: 192, 136: 192, 137: 193, 138: 193, 139: 194, 140: 194,
    141: 195, 142: 196, 143: 196, 144: 197, 145: 197, 146: 198, 147: 198, 148: 199, 149: 199, 150: 200,
    151: 201, 152: 202, 153: 203, 154: 204, 155: 205, 156: 206, 157: 207, 158: 208, 159: 209, 160: 210,
    161: 211, 162: 212, 163: 213, 164: 214, 165: 215, 166: 216, 167: 217, 168: 218, 169: 219, 170: 220,
    171: 221, 172: 222, 173: 223, 174: 224, 175: 225, 176: 226, 177: 227, 178: 228, 179: 229, 180: 230,
    181: 231, 182: 232, 183: 233, 184: 234, 185: 235, 186: 236, 187: 237, 188: 238, 189: 239, 190: 240,
    191: 241, 192: 242, 193: 243, 194: 244, 195: 245, 196: 246, 197: 247, 198: 248, 199: 249
}


def aqi_to_concentration(aqi_value, pollutant):
    """
    Convert AQI value to pollutant concentration using reverse lookup of AQICN tables.
    Uses linear interpolation between table points.
    """
    aqi = float(aqi_value)
    conversion_table = PM10_CONVERSION if pollutant == 'pm10' else PM25_CONVERSION

    # Get sorted concentration values
    concentrations = sorted(conversion_table.keys())
    aqi_values = [conversion_table[c] for c in concentrations]

    # Find bracketing AQI values
    if aqi <= aqi_values[0]:
        return concentrations[0]
    if aqi >= aqi_values[-1]:
        return concentrations[-1]

    # Find the two AQI values that bracket our target AQI
    for i in range(len(aqi_values) - 1):
        if aqi_values[i] <= aqi <= aqi_values[i + 1]:
            # Linear interpolation
            aqi_low, aqi_high = aqi_values[i], aqi_values[i + 1]
            conc_low, conc_high = concentrations[i], concentrations[i + 1]

            if aqi_high == aqi_low:
                return conc_low

            concentration = conc_low + (aqi - aqi_low) / (aqi_high - aqi_low) * (conc_high - conc_low)
            return concentration

    return None


def fetch_air_quality(latitude, longitude, token):
    """
    Fetch PM10 and PM2.5 AQI from WAQI API and convert to concentration (μg/m³).
    WAQI returns AQI values in iaqi.{pm10,pm25}.v fields.
    Returns dict with pm10 and pm25 in μg/m³, or None on error.
    """
    if not token or not latitude or not longitude:
        return None

    try:
        url = f"https://api.waqi.info/feed/geo:{latitude};{longitude}/?token={token}"
        response = requests.get(url, timeout=10)
        data = response.json()

        if data.get('status') != 'ok' or 'data' not in data:
            return None

        iaqi = data['data'].get('iaqi', {})
        pm10_aqi = iaqi.get('pm10', {}).get('v')
        pm25_aqi = iaqi.get('pm25', {}).get('v')

        # Convert AQI values to concentration (μg/m³)
        if pm10_aqi is not None and pm25_aqi is not None:
            pm10_conc = aqi_to_concentration(pm10_aqi, 'pm10')
            pm25_conc = aqi_to_concentration(pm25_aqi, 'pm25')

            if pm10_conc is not None and pm25_conc is not None:
                return {
                    'pm10': float(pm10_conc),  # μg/m³
                    'pm25': float(pm25_conc),  # μg/m³
                }

        return None

    except Exception as e:
        logging.debug(f"WAQI fetch failed at ({latitude}, {longitude}): {e}")
        return None


def fetch_weatherbit_90day_avg(latitude, longitude, api_key):
    """
    Fetch 90-day average PM10 and PM2.5 from Weatherbit.
    Splits into 3 × 30-day chunks to avoid "Request too large" error.
    Returns dict with pm10_90day_avg and pm25_90day_avg (μg/m³), or None on error.
    """
    if not api_key or not latitude or not longitude:
        return None

    try:
        all_pm25 = []
        all_pm10 = []

        # Fetch in 3 × 30-day chunks (90 - 60, 60 - 30, 30 - 0 days ago)
        for chunk in range(3):
            end_offset = 30 * (2 - chunk)      # 60, 30, 0 days ago
            start_offset = 30 * (3 - chunk)    # 90, 60, 30 days ago

            end_date = (datetime.now() - timedelta(days=end_offset)).strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=start_offset)).strftime('%Y-%m-%d')

            url = "https://api.weatherbit.io/v2.0/history/airquality"
            params = {
                'lat': latitude,
                'lon': longitude,
                'start_date': start_date,
                'end_date': end_date,
                'key': api_key
            }

            response = requests.get(url, params=params, timeout=30)

            if response.status_code != 200:
                logging.debug(f"Weatherbit chunk {chunk+1} failed at ({latitude}, {longitude}): {response.status_code}")
                continue

            data = response.json()
            if 'data' not in data or not data['data']:
                continue

            # Extract PM values from hourly records
            for record in data['data']:
                if record.get('pm25') is not None:
                    all_pm25.append(record['pm25'])
                if record.get('pm10') is not None:
                    all_pm10.append(record['pm10'])

        # Calculate 90-day averages
        if all_pm25 and all_pm10:
            return {
                'pm25_90day_avg': float(np.mean(all_pm25)),
                'pm10_90day_avg': float(np.mean(all_pm10)),
                'pm25_data_points': len(all_pm25),
                'pm10_data_points': len(all_pm10),
            }

        return None

    except Exception as e:
        logging.debug(f"Weatherbit fetch failed at ({latitude}, {longitude}): {e}")
        return None


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
# RUL CALCULATION (MODE 3 - ROLLING MEDIAN)
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

def query_site_complete(site, password, waqi_token=None, weatherbit_token=None):
    """
    Complete workflow: SSH → query → extract episodes → compute RUL → fetch 90-day air quality.
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

        result = {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': True,
            **rul_result
        }

        # Fetch 90-day air quality from Weatherbit if coordinates available
        if site.get('latitude') and site.get('longitude') and weatherbit_token:
            air_quality_90day = fetch_weatherbit_90day_avg(site['latitude'], site['longitude'], weatherbit_token)
            if air_quality_90day:
                # Map 90-day averages to pm10/pm25 keys for regression compatibility
                result['air_quality'] = {
                    'pm10': air_quality_90day.get('pm10_90day_avg'),
                    'pm25': air_quality_90day.get('pm25_90day_avg'),
                }
                result['air_quality_source'] = 'weatherbit_90day'
                result['air_quality_data_points'] = {
                    'pm10': air_quality_90day.get('pm10_data_points'),
                    'pm25': air_quality_90day.get('pm25_data_points'),
                }
                result['latitude'] = site['latitude']
                result['longitude'] = site['longitude']

        return result

    except Exception as e:
        logging.error(f"{site_id}: Unexpected error in complete workflow: {e}")
        return {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': False,
            'error': str(e)
        }


def query_all_sites_parallel(sites, password, waqi_token=None, weatherbit_token=None, max_workers=10):
    """
    Query all sites in parallel using ThreadPoolExecutor.
    Fetches InfluxDB RUL data + 90-day Weatherbit air quality for each site.

    With 1020 sites and 10 workers, ~102 batches of 10 sites.
    Each batch takes ~30-60 seconds, so total ~50 minutes.
    """
    results = {}
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_site = {
            executor.submit(query_site_complete, site, password, waqi_token, weatherbit_token): site
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
                    has_aq = "✓AQ" if result.get('air_quality') else ""
                    logging.info(f"[{i}/{len(sites)}] ✓ {result['site_id']}: {urgency} ({rul_str}) {has_aq}")
                else:
                    failed += 1
                    logging.warning(f"[{i}/{len(sites)}] ✗ {result['site_id']}: {result.get('error', 'Unknown error')}")

            except Exception as e:
                failed += 1
                logging.error(f"[{i}/{len(sites)}] ✗ {site['site_id']}: {e}")

    return results, completed, failed


# ============================================================================
# MAIN
# ============================================================================

def run_air_quality_regression(results):
    """
    Run flexible linear regression: Slope ~ adjusted_fan_hours_per_day + [PM10] + [PM2.5]
    Always includes adjusted fan runtime as base factor.
    Adds PM10/PM2.5 based on availability.
    Returns regression results dict or None if insufficient data.
    """
    # Filter sites with RUL slope and adjusted hours data
    regression_data = []
    for site_id, result in results.items():
        if (result.get('success') and result.get('slope') is not None and
            result.get('avg_adjusted_hours_per_day') is not None):
            regression_data.append({
                'site_id': site_id,
                'slope': result['slope'],
                'avg_adjusted_hours': result['avg_adjusted_hours_per_day'],
                'pm10': result.get('air_quality', {}).get('pm10') if result.get('air_quality') else None,
                'pm25': result.get('air_quality', {}).get('pm25') if result.get('air_quality') else None,
            })

    if len(regression_data) < 2:
        logging.warning(f"Insufficient sites with slope data for regression ({len(regression_data)} sites)")
        return None

    slopes = np.array([d['slope'] for d in regression_data])
    adjusted_hours = np.array([d['avg_adjusted_hours'] for d in regression_data])
    pm10_vals = np.array([d['pm10'] if d['pm10'] is not None else np.nan for d in regression_data])
    pm25_vals = np.array([d['pm25'] if d['pm25'] is not None else np.nan for d in regression_data])

    # Determine which air quality features are available (non-NaN)
    has_pm10 = np.sum(~np.isnan(pm10_vals)) >= 2
    has_pm25 = np.sum(~np.isnan(pm25_vals)) >= 2

    # Build regression: always include adjusted_hours, add air quality if available
    if has_pm10 and has_pm25:
        # 3-factor: adjusted_hours + PM10 + PM2.5
        valid_idx = (~np.isnan(pm10_vals)) & (~np.isnan(pm25_vals))
        X = np.column_stack([adjusted_hours[valid_idx], pm10_vals[valid_idx], pm25_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM10', 'PM2.5']
        model_type = "3-factor (adjusted_hours + PM10 + PM2.5)"
    elif has_pm25:
        # 2-factor: adjusted_hours + PM2.5 (PM10 unavailable)
        valid_idx = ~np.isnan(pm25_vals)
        X = np.column_stack([adjusted_hours[valid_idx], pm25_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM2.5']
        model_type = "2-factor (adjusted_hours + PM2.5)"
    elif has_pm10:
        # 2-factor: adjusted_hours + PM10 (PM2.5 unavailable)
        valid_idx = ~np.isnan(pm10_vals)
        X = np.column_stack([adjusted_hours[valid_idx], pm10_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM10']
        model_type = "2-factor (adjusted_hours + PM10)"
    else:
        # 1-factor: adjusted_hours only (no air quality data)
        X = adjusted_hours.reshape(-1, 1)
        y = slopes
        feature_names = ['adjusted_hours']
        model_type = "1-factor (adjusted_hours only)"

    if len(y) < 2:
        logging.warning("Insufficient valid data points for regression after filtering NaNs")
        return None

    # Fit model with intercept
    coefficients = np.linalg.lstsq(np.column_stack([np.ones(len(y)), X]), y, rcond=None)[0]
    intercept = coefficients[0]
    coefs = coefficients[1:]

    # Calculate predictions and R²
    y_fit = intercept + np.dot(X, coefs)
    ss_res = np.sum((y - y_fit) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    # Calculate residual std error
    n_features = len(feature_names)
    residuals = y - y_fit
    residual_std_error = np.sqrt(np.sum(residuals ** 2) / (len(y) - n_features - 1)) if len(y) > n_features + 1 else 0.0

    # Build result dict with flexible structure
    result = {
        'model_type': model_type,
        'sites_analyzed': len(y),
        'intercept': float(intercept),
        'r_squared': float(r_squared),
        'residual_std_error': float(residual_std_error),
    }

    # Add coefficients based on available features
    result['coefficient_adjusted_hours'] = float(coefs[feature_names.index('adjusted_hours')])
    result['coefficient_pm10'] = float(coefs[feature_names.index('PM10')]) if 'PM10' in feature_names else None
    result['coefficient_pm25'] = float(coefs[feature_names.index('PM2.5')]) if 'PM2.5' in feature_names else None

    # Build interpretation based on available features
    interp_parts = [f"adjusted_hours={result['coefficient_adjusted_hours']:.6f}"]
    if result['coefficient_pm10'] is not None:
        interp_parts.append(f"PM10={result['coefficient_pm10']:.6f}")
    if result['coefficient_pm25'] is not None:
        interp_parts.append(f"PM2.5={result['coefficient_pm25']:.6f}")

    result['interpretation'] = (
        f"{model_type}: {', '.join(interp_parts)}. "
        f"R²={r_squared*100:.1f}% ({len(y)} sites)."
    )

    logging.info(f"Air Quality Regression: {result['interpretation']}")
    return result


def main():
    """Main entry point."""

    inventory_csv = 'sites_inventory.csv'
    coords_csv = 'sites_inventory_2.csv'
    output_file = 'sites_data.json'
    max_workers = 10

    logging.info("=" * 80)
    logging.info("Rogers HVAC RUL Multi-Site Query + Air Quality Analysis")
    logging.info("=" * 80)

    # Load inventory with coordinates
    sites = load_inventory(inventory_csv, coords_csv)
    sites_with_coords = sum(1 for s in sites if s.get('latitude') and s.get('longitude'))
    logging.info(f"Sites with coordinates: {sites_with_coords}/{len(sites)}")
    logging.info(f"Starting parallel queries ({max_workers} concurrent workers)...")
    logging.info(f"Estimated time: ~{len(sites) / max_workers / 2:.0f} minutes for {len(sites)} sites")

    # Query all sites
    start_time = datetime.now()
    results, completed, failed = query_all_sites_parallel(sites, SITE_PASSWORD, WAQI_TOKEN, WEATHERBIT_API_KEY, max_workers)
    elapsed = (datetime.now() - start_time).total_seconds()

    logging.info("\n" + "=" * 80)
    logging.info(f"Query Complete")
    logging.info("=" * 80)
    logging.info(f"Elapsed time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    logging.info(f"Completed: {completed}/{len(sites)} sites")
    logging.info(f"Failed: {failed}/{len(sites)} sites")

    # Aggregate and save
    successful_sites = {k: v for k, v in results.items() if v.get('success')}
    sites_with_aq = sum(1 for r in successful_sites.values() if r.get('air_quality'))

    urgency_counts = {
        'URGENT': sum(1 for r in successful_sites.values() if r.get('urgency') == 'URGENT'),
        'WARNING': sum(1 for r in successful_sites.values() if r.get('urgency') == 'WARNING'),
        'OK': sum(1 for r in successful_sites.values() if r.get('urgency') == 'OK'),
    }

    # Run air quality regression if enough data
    regression_results = None
    if WAQI_TOKEN:
        regression_results = run_air_quality_regression(results)
        if regression_results:
            logging.info("\n" + "=" * 80)
            logging.info("Air Quality Regression Analysis (Slope ~ PM10 + PM2.5)")
            logging.info("=" * 80)
            logging.info(f"Sites with air quality data: {regression_results['sites_analyzed']}")
            logging.info(f"R² (variance explained): {regression_results['r_squared']:.4f}")
            logging.info(f"PM10 coefficient: {regression_results['coefficient_pm10']:.6f} (SE: {regression_results['se_pm10']:.6f})")
            logging.info(f"PM2.5 coefficient: {regression_results['coefficient_pm25']:.6f} (SE: {regression_results['se_pm25']:.6f})")
            logging.info(f"Interpretation: {regression_results['interpretation']}")

    output = {
        'query_timestamp': datetime.now().isoformat(),
        'query_elapsed_seconds': elapsed,
        'sites_queried': completed,
        'sites_total': len(sites),
        'sites_failed': failed,
        'sites_with_air_quality': sites_with_aq,
        'urgency_summary': urgency_counts,
        'air_quality_regression': regression_results,
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
    logging.info(f"🌍 With Air Quality:  {sites_with_aq:>4} sites")


if __name__ == '__main__':
    main()
