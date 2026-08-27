#!/usr/bin/env python3

"""
Query all Rogers HVAC sites (1020+) via parallel SSH + InfluxDB.
Extract Mode 3 RUL for each site.
Output aggregated sites_data.json with pollution effect adjustment.
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

WEATHERBIT_API_KEY = os.getenv('WEATHERBIT_API_KEY')
if not WEATHERBIT_API_KEY:
    logging.warning("WEATHERBIT_API_KEY not found in .env - Weatherbit air quality analysis will be skipped")

# Configuration
FAN_THRESHOLD = 95.0          # Minimum fan speed to trigger episode
MIN_EPISODE_MINUTES = 30.0    # Minimum episode duration
R2_THRESHOLD = 0.25           # Minimum R² to estimate RUL
FAILURE_DT = 10.0             # ΔT threshold for filter failure
SSH_TIMEOUT = 30              # SSH connection timeout
QUERY_TIMEOUT = 60            # InfluxDB query timeout
QUERY_DAYS = 90               # Days of historical data to query
STALE_DATA_CUTOFF_YEAR = 2023 # Data must have entries after this year

# Failure reason codes (for detailed error reporting)
class FailureReason:
    SSH_UNREACHABLE = "SSH_UNREACHABLE"
    SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
    INFLUXDB_OFFLINE = "INFLUXDB_OFFLINE"
    STALE_DATA = "STALE_DATA"
    NO_HVAC_MEASUREMENT = "NO_HVAC_MEASUREMENT"
    MISSING_SENSORS = "MISSING_SENSORS"
    INSUFFICIENT_EPISODES = "INSUFFICIENT_EPISODES"
    INSUFFICIENT_DEGRADATION = "INSUFFICIENT_DEGRADATION"
    UNKNOWN = "UNKNOWN"

# ============================================================================
# LOAD INVENTORY
# ============================================================================

def load_inventory(csv_path, coords_csv_path=None):
    """
    Load site inventory CSV and optionally merge with coordinates CSV.

    coords_csv_path should have columns: Site ID, Latitude, Longitude
    Sites without coordinates will have lat=None, lon=None.
    """
    # Try multiple path variations (handles Windows/Linux, different run locations)
    # Priority: current dir → parent dir → script dir
    possible_paths = [
        csv_path,  # Current directory
        os.path.join('..', csv_path),  # Parent directory (if running from hvac_rul_project)
        os.path.join('hvac_rul_project', csv_path),  # Script location (if running from tcu_hvac_rul)
    ]
    
    actual_path = None
    for p in possible_paths:
        if os.path.exists(p):
            actual_path = p
            logging.info(f"✓ Found inventory at: {os.path.abspath(p)}")
            break
    
    if not actual_path:
        logging.error(f"✗ Inventory file not found: {csv_path}")
        logging.error(f"  Tried: {[os.path.abspath(p) for p in possible_paths]}")
        logging.error(f"  Current directory: {os.getcwd()}")
        sys.exit(1)
    
    csv_path = actual_path

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
    if coords_csv_path:
        # Try multiple path variations for coordinates CSV
        # Priority: current dir → parent dir → script dir
        possible_coords_paths = [
            coords_csv_path,  # Current directory
            os.path.join('..', coords_csv_path),  # Parent directory
            os.path.join('hvac_rul_project', coords_csv_path),  # Script location
        ]
        
        coords_actual_path = None
        for p in possible_coords_paths:
            if os.path.exists(p):
                coords_actual_path = p
                logging.info(f"✓ Found coordinates at: {os.path.abspath(p)}")
                break
        
        if coords_actual_path:
            coords_df = pd.read_csv(coords_actual_path)
            coords_df.columns = coords_df.columns.str.strip().str.lower()

            # Normalize Site ID column name (try multiple variations)
            site_id_col = None
            for col in ['site id', 'site_id', 'site']:
                if col in coords_df.columns:
                    site_id_col = col
                    break

            if site_id_col:
                # Build a lookup dict by Site ID (case-insensitive, whitespace-stripped)
                coords_dict = {}
                for _, row in coords_df.iterrows():
                    sid = str(row[site_id_col]).strip().upper()  # Normalize for matching
                    lat = row.get('latitude')
                    lon = row.get('longitude')
                    # Only store if coordinates are valid
                    if lat is not None and lon is not None and str(lat).lower() != 'nan' and str(lon).lower() != 'nan':
                        coords_dict[sid] = {
                            'latitude': float(lat),
                            'longitude': float(lon),
                        }

                # Merge coordinates into sites by matching Site ID
                sites_with_coords = 0
                for site in sites:
                    # Normalize site_id for matching
                    normalized_site_id = site['site_id'].strip().upper()
                    if normalized_site_id in coords_dict:
                        coords = coords_dict[normalized_site_id]
                        site['latitude'] = coords['latitude']
                        site['longitude'] = coords['longitude']
                        sites_with_coords += 1

                logging.info(f"✓ Loaded coordinates for {sites_with_coords}/{len(sites)} sites")
        else:
            logging.warning(f"✗ Coordinates CSV not found: {coords_csv_path}")
            logging.warning(f"  Tried: {[os.path.abspath(p) for p in possible_coords_paths]}")
            logging.warning(f"  Sites will be queried but without air quality data")

    logging.info(f"Loaded {len(sites)} sites from inventory")
    return sites

# ============================================================================
# WEATHERBIT AIR QUALITY QUERY
# ============================================================================

def fetch_weatherbit_90day_avg(latitude, longitude, api_key):
    """
    Fetch 90-day average PM10 and PM2.5 from Weatherbit (μg/m³).
    Splits into 3 × 30-day chunks to avoid "Request too large" error.
    Returns dict with pm10_90day_avg and pm25_90day_avg, or None on error.
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

            # Extract PM values from hourly records (already in μg/m³)
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
# INFLUXDB QUERY WITH MULTI-DATABASE FALLBACK
# ============================================================================

_debug_sites_printed = set()

def query_site_influxdb(site, password, debug_first_failure=False):
    """
    SSH into site and query InfluxDB 1.x for HVAC data.
    Tries both 'aque' (modern tag-based) and 'hvac' (older field-based) databases.
    Returns: DataFrame on success, or error dict on failure.
    Error dict has keys: error_code, error_message, and optionally last_data_timestamp, last_data_year.
    """
    site_ip = site['ip']
    site_id = site['site_id']

    try:
        # SSH connection
        logging.info(f"{site_id}: Attempting SSH connection to {site_ip}...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            ssh.connect(
                hostname=site_ip,
                port=22,
                username='plc',
                password=password,
                timeout=SSH_TIMEOUT,
                auth_timeout=SSH_TIMEOUT,
                banner_timeout=SSH_TIMEOUT
            )
            logging.info(f"{site_id}: ✓ SSH connection successful")
        except paramiko.AuthenticationException as auth_err:
            logging.error(f"{site_id}: ✗ SSH authentication failed - check plc user/password.")
            return {'error_code': FailureReason.SSH_AUTH_FAILED, 'error_message': f'SSH authentication failed: {auth_err}'}
        except (socket.timeout, TimeoutError) as timeout_err:
            logging.error(f"{site_id}: ✗ SSH connection timeout ({SSH_TIMEOUT}s) - site unreachable.")
            return {'error_code': FailureReason.SSH_UNREACHABLE, 'error_message': f'SSH connection timeout after {SSH_TIMEOUT}s'}
        except paramiko.SSHException as ssh_err:
            logging.error(f"{site_id}: ✗ SSH error: {ssh_err}")
            return {'error_code': FailureReason.SSH_UNREACHABLE, 'error_message': f'SSH error: {ssh_err}'}

        # Check data recency in aque database before full query
        logging.info(f"{site_id}: Checking data recency in aque database...")
        try:
            cmd_latest = 'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT * FROM hvac ORDER BY time DESC LIMIT 1"'
            stdin, stdout, stderr = ssh.exec_command(cmd_latest, timeout=15)
            latest_output = stdout.read().decode('utf-8')

            if latest_output:
                try:
                    latest_resp = json.loads(latest_output)
                    if 'results' in latest_resp and latest_resp['results']:
                        series = latest_resp['results'][0].get('series', [])
                        if series and series[0].get('values'):
                            last_timestamp_str = series[0]['values'][0][0]
                            last_dt = pd.to_datetime(last_timestamp_str, utc=True)
                            last_year = last_dt.year

                            logging.info(f"{site_id}: Latest data timestamp: {last_timestamp_str} (year {last_year})")

                            if last_year < STALE_DATA_CUTOFF_YEAR:
                                logging.warning(f"{site_id}: ✗ Stale data - last update {last_year} (cutoff: {STALE_DATA_CUTOFF_YEAR})")
                                ssh.close()
                                return {
                                    'error_code': FailureReason.STALE_DATA,
                                    'error_message': f'No data since {last_year}. Last record: {last_timestamp_str}',
                                    'last_data_timestamp': last_timestamp_str,
                                    'last_data_year': last_year,
                                }
                except (json.JSONDecodeError, IndexError, KeyError, TypeError):
                    logging.debug(f"{site_id}: Could not parse latest data check (continuing with full query)")
        except Exception as e:
            logging.debug(f"{site_id}: Data recency check failed: {e} (continuing with full query)")

        # Try both databases: aque (modern) then hvac (legacy)
        for database in ['aque', 'hvac']:
            logging.info(f"{site_id}: Querying {database} database...")
            query = f"SELECT * FROM hvac WHERE time > now() - {QUERY_DAYS}d"
            cmd = f'curl -s -G "http://localhost:8086/query?db={database}" --data-urlencode "q={query}"'

            try:
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=QUERY_TIMEOUT)
                output = stdout.read().decode('utf-8')
                error = stderr.read().decode('utf-8')
            except socket.timeout as t:
                logging.warning(f"{site_id}: Query timeout on {database} ({QUERY_TIMEOUT}s). InfluxDB may be unresponsive.")
                continue
            except Exception as e:
                logging.warning(f"{site_id}: Failed to execute query on {database}: {e}")
                continue

            # Check for SSH/curl errors
            if error:
                logging.warning(f"{site_id}: {database} curl error: {error}")
                continue
            
            if not output:
                logging.warning(f"{site_id}: {database} returned no output (InfluxDB not running or no data)")
                continue

            logging.info(f"{site_id}: {database} curl returned {len(output)} bytes")

            try:
                # Parse JSON response
                response = json.loads(output)
                if 'results' not in response:
                    logging.warning(f"{site_id}: {database} - Invalid response structure (no 'results' key). Response: {output[:300]}")
                    continue
                
                if not response['results']:
                    logging.warning(f"{site_id}: {database} - Empty results array")
                    continue

                result = response['results'][0]
                
                if 'error' in result:
                    logging.warning(f"{site_id}: {database} - InfluxDB returned error: {result['error']}")
                    continue
                
                if 'series' not in result:
                    logging.warning(f"{site_id}: {database} - No 'series' key in result. Keys present: {list(result.keys())}")
                    continue
                
                if not result['series']:
                    logging.warning(f"{site_id}: {database} - 'series' array is empty (no hvac measurement data found)")
                    continue

                logging.info(f"{site_id}: {database} - Got {len(result['series'])} series")

                # Combine all series into one DataFrame (InfluxDB may return multiple series from tag-based queries)
                df_list = []
                for series_idx, series in enumerate(result['series']):
                    columns = series.get('columns', [])
                    values = series.get('values', [])
                    tags = series.get('tags', {})

                    logging.info(f"{site_id}: {database} series[{series_idx}] - columns: {columns}, tags: {list(tags.keys())}, value rows: {len(values)}")

                    if not columns:
                        logging.warning(f"{site_id}: {database} series[{series_idx}] - no columns")
                        continue
                    
                    if not values:
                        logging.warning(f"{site_id}: {database} series[{series_idx}] - no values")
                        continue

                    # Create DataFrame from columns and values
                    df = pd.DataFrame(values, columns=columns)

                    # Add tag columns (tags are in metadata, not in values array)
                    for tag_key, tag_value in tags.items():
                        df[tag_key] = tag_value

                    # Parse timestamp and convert to datetime
                    if 'time' in df.columns:
                        df['time'] = pd.to_datetime(df['time'], utc=True)
                    else:
                        logging.warning(f"{site_id}: {database} series[{series_idx}] - no 'time' column. Available: {df.columns.tolist()}")
                        continue

                    # Convert value to numeric (if exists)
                    if 'value' in df.columns:
                        df['value'] = pd.to_numeric(df['value'], errors='coerce')

                    logging.info(f"{site_id}: {database} series[{series_idx}] - parsed {len(df)} rows. Columns: {df.columns.tolist()}")
                    df_list.append(df)

                if not df_list:
                    logging.warning(f"{site_id}: {database} - no usable series after parsing")
                    continue

                # Combine all series into single DataFrame
                df = pd.concat(df_list, ignore_index=True) if len(df_list) > 1 else df_list[0]

                logging.info(f"✓ {site_id}: Successfully retrieved {len(df)} rows from {database}")
                ssh.close()
                return df

            except json.JSONDecodeError as je:
                logging.warning(f"{site_id}: {database} - Failed to parse JSON response. First 300 chars: {output[:300]}")
                continue
            except Exception as e:
                logging.warning(f"{site_id}: {database} - Error processing response: {e}")
                continue

        ssh.close()
        logging.error(f"✗ {site_id}: Failed to retrieve data from both aque and hvac databases")
        return {'error_code': FailureReason.INFLUXDB_OFFLINE, 'error_message': 'InfluxDB query failed for both aque and hvac databases'}

    except paramiko.AuthenticationException:
        logging.error(f"{site_id}: Authentication failed (check plc user/password)")
        return {'error_code': FailureReason.SSH_AUTH_FAILED, 'error_message': 'SSH authentication failed'}
    except (socket.timeout, TimeoutError):
        logging.error(f"{site_id}: Connection timeout (unreachable or slow site)")
        return {'error_code': FailureReason.SSH_UNREACHABLE, 'error_message': 'SSH connection timeout'}
    except paramiko.SSHException as e:
        logging.error(f"{site_id}: SSH error: {e}")
        return {'error_code': FailureReason.SSH_UNREACHABLE, 'error_message': f'SSH error: {e}'}
    except Exception as e:
        logging.error(f"{site_id}: Unexpected error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return {'error_code': FailureReason.UNKNOWN, 'error_message': str(e)}

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

    Returns: (episodes, error_code, error_message)
      - On success: (list of dicts, None, None)
      - On failure: ([], FailureReason.XXX, error_message)
    """
    if df is None or len(df) < 2:
        logging.warning("extract_episodes: DataFrame is None or too small")
        return [], FailureReason.INSUFFICIENT_EPISODES, "DataFrame too small or empty"

    # Find pivot column (sensor identifier) - try multiple tag names
    pivot_col = None
    for col_name in ['display_point', 'equipment_id', 'alias']:
        if col_name in df.columns:
            pivot_col = col_name
            break

    if not pivot_col:
        available_cols = df.columns.tolist()
        logging.error(f"extract_episodes: No pivot column found. Available columns: {available_cols}")
        return [], FailureReason.MISSING_SENSORS, f"No pivot column found (tried: display_point, equipment_id, alias). Available: {available_cols}"

    logging.info(f"extract_episodes: Using pivot column '{pivot_col}'")

    # Pivot by sensor identifier to get separate columns for each sensor
    try:
        df_pivot = df.pivot_table(
            index='time',
            columns=pivot_col,
            values='value',
            aggfunc='first'
        ).reset_index()
        logging.info(f"extract_episodes: Pivot successful. Shape: {df_pivot.shape}, columns: {df_pivot.columns.tolist()}")
    except Exception as e:
        logging.error(f"extract_episodes: Failed to pivot on column '{pivot_col}': {e}")
        return [], FailureReason.MISSING_SENSORS, f"Failed to pivot on column '{pivot_col}': {e}"

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
        logging.error(f"extract_episodes: Missing critical sensors: {missing}")
        return [], FailureReason.MISSING_SENSORS, f"Missing critical sensors: {', '.join(missing)}"

    logging.info(f"extract_episodes: Found critical sensors - fan_col='{fan_col}', fc_col='{fc_col}', dt_col='{dt_col}'")

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
    episode_groups = df_pivot[df_pivot['in_episode']].groupby('episode_id')
    logging.info(f"extract_episodes: Found {len(episode_groups)} potential episode groups")
    
    for ep_id, group in episode_groups:
        group = group.dropna(subset=[dt_col, fan_col])

        if len(group) < 2:
            continue

        start_time = group['time'].iloc[0]
        end_time = group['time'].iloc[-1]
        duration_min = (end_time - start_time).total_seconds() / 60.0

        if duration_min < MIN_EPISODE_MINUTES:
            logging.debug(f"extract_episodes: Episode {ep_id} skipped (duration {duration_min:.1f}min < {MIN_EPISODE_MINUTES}min)")
            continue

        # Max ΔT during episode
        max_dt = float(group[dt_col].max())

        # Calculate fan_speed²-weighted runtime in hours
        fan_speed_pct = group[fan_col].values / 100.0
        fan_weighting = fan_speed_pct ** 2
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

    logging.info(f"extract_episodes: Extracted {len(episodes)} valid episodes (≥{MIN_EPISODE_MINUTES}min)")
    return episodes, None, None

# ============================================================================
# RUL CALCULATION
# ============================================================================

def compute_rul_mode3(episodes, pollution_effect=None):
    """
    Compute RUL: max ΔT vs percentage-adjusted runtime hours + linear trend.

    If pollution_effect provided (β₁×PM2.5 + β₂×PM10), adjusts slope as:
    adjusted_slope = raw_slope × (1 + pollution_effect)

    Returns: dict with RUL metrics or None if insufficient data
    """
    if len(episodes) < 3:
        return None

    max_deltas = [ep['max_dt'] for ep in episodes]
    adjusted_runtime_hours = [ep['adjusted_runtime_hours'] for ep in episodes]
    episode_start_times = [ep['start_time'].isoformat() for ep in episodes]

    # Cumulative adjusted runtime hours
    cumulative_hours = np.cumsum(adjusted_runtime_hours)

    # Linear regression: max ΔT vs cumulative adjusted runtime hours
    coeffs = np.polyfit(cumulative_hours, max_deltas, 1)
    slope, intercept = coeffs[0], coeffs[1]

    # Apply pollution effect multiplier if available
    if pollution_effect is not None:
        adjusted_slope = slope * (1.0 + pollution_effect)
    else:
        adjusted_slope = slope

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

    # Use trend line to determine current ΔT (not noisy last reading)
    current_hours = cumulative_hours[-1]
    current_dt = intercept + slope * current_hours
    baseline_dt = intercept  # Trend line at zero hours

    # RUL calculation using adjusted slope
    rul_days = None
    urgency = "UNKNOWN"

    if r2 >= R2_THRESHOLD:
        if adjusted_slope <= 0:
            urgency = "OK"
            rul_days = 999
        elif current_dt >= FAILURE_DT:
            urgency = "URGENT"
            rul_days = 0
        else:
            hours_to_failure = (FAILURE_DT - intercept) / adjusted_slope if adjusted_slope > 0 else 999
            remaining_hours = hours_to_failure - current_hours

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
        'episode_start_times': episode_start_times,
        'r2': float(r2),
        'intercept': float(intercept),
        'slope': float(slope),
        'adjusted_slope': float(adjusted_slope) if pollution_effect is not None else None,
        'pollution_effect': float(pollution_effect) if pollution_effect is not None else None,
        'rul_days': float(rul_days) if rul_days is not None else None,
        'urgency': urgency,
        'episodes_count': len(episodes),
        'baseline_dt': float(baseline_dt),
        'current_dt': float(current_dt),
        'failure_dt': float(FAILURE_DT),
        'pct_life': float(pct_life),
        'avg_adjusted_hours_per_day': float(avg_hours_per_day),
        'total_adjusted_hours': float(current_hours),
        'query_start_date': episodes[0]['start_time'].date().isoformat(),
        'query_end_date': episodes[-1]['start_time'].date().isoformat(),
        'last_episode_time': episodes[-1]['start_time'].isoformat(),
    }

# ============================================================================
# PARALLEL QUERY EXECUTOR
# ============================================================================

def query_site_complete(site, password, weatherbit_token=None, debug_first_failure=False):
    """
    Complete workflow: SSH → query → extract episodes → compute RUL → fetch air quality.
    Returns result dict or error dict.
    """
    site_id = site['site_id']
    site_ip = site['ip']

    try:
        # Query InfluxDB
        result = query_site_influxdb(site, password, debug_first_failure=debug_first_failure)

        # Check if result is an error dict
        if isinstance(result, dict) and 'error_code' in result:
            logging.error(f"{site_id}: ✗ InfluxDB query failed: {result['error_code']}")
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error_code': result['error_code'],
                'error_message': result['error_message'],
                'error': result['error_message'],  # Backwards compatibility
                'last_data_timestamp': result.get('last_data_timestamp'),
                'last_data_year': result.get('last_data_year'),
            }

        df = result  # It's a DataFrame if we got here

        # Extract episodes
        episodes, error_code, error_message = extract_episodes(df)
        if len(episodes) < 3:
            logging.error(f"{site_id}: ✗ Insufficient episodes: extracted {len(episodes)}, need ≥3 for RUL model")
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error_code': error_code if error_code else FailureReason.INSUFFICIENT_EPISODES,
                'error_message': error_message if error_message else f'Only {len(episodes)} episodes extracted (need ≥3)',
                'error': error_message if error_message else f'Insufficient episodes: {len(episodes)}/3',  # Backwards compatibility
            }

        # Compute RUL
        rul_result = compute_rul_mode3(episodes)
        if rul_result is None:
            logging.error(f"{site_id}: ✗ RUL calculation failed")
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error_code': FailureReason.UNKNOWN,
                'error_message': 'RUL calculation failed',
                'error': 'RUL calculation failed (see logs for details)',
            }

        # Check for insufficient degradation (negative slope indicates insufficient data)
        if rul_result.get('slope') is not None and rul_result['slope'] <= 0:
            logging.warning(f"{site_id}: ⚠️ Filter not degrading (negative trend - may need more time or recent filter change)")
            return {
                'site_id': site_id,
                'site_name': site['site_name'],
                'ip': site_ip,
                'success': False,
                'error_code': FailureReason.INSUFFICIENT_DEGRADATION,
                'error_message': 'Filter not degrading (negative trend - may indicate insufficient data or recent filter change)',
                'error': 'Insufficient degradation trend',
                'slope': float(rul_result['slope']),
                'episodes_count': len(episodes),
            }

        result = {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': True,
            **rul_result
        }
        
        rul_days = rul_result.get('rul_days')
        rul_str = f"{rul_days:.1f}d" if rul_days is not None else "N/A"
        logging.info(f"✓ {site_id}: RUL calculated successfully - {rul_result.get('urgency')} (RUL: {rul_str})")

        # Fetch 90-day air quality from Weatherbit if coordinates available
        if site.get('latitude') and site.get('longitude') and weatherbit_token:
            air_quality_90day = fetch_weatherbit_90day_avg(site['latitude'], site['longitude'], weatherbit_token)
            if air_quality_90day:
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
        logging.error(f"{site_id}: ✗ Unexpected error in workflow: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return {
            'site_id': site_id,
            'site_name': site['site_name'],
            'ip': site_ip,
            'success': False,
            'error_code': FailureReason.UNKNOWN,
            'error_message': str(e),
            'error': str(e),
        }

def query_all_sites_parallel(sites, password, weatherbit_token=None, max_workers=10, debug_first_failure=False):
    """
    Query all sites in parallel using ThreadPoolExecutor.
    """
    results = {}
    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_site = {
            executor.submit(query_site_complete, site, password, weatherbit_token, debug_first_failure): site
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
# AIR QUALITY REGRESSION & RUL ADJUSTMENT
# ============================================================================

def run_air_quality_regression(results):
    """
    Run flexible linear regression: Slope ~ adjusted_fan_hours_per_day + [PM10] + [PM2.5]
    Excludes sites with negative slopes (insufficient data for analysis).
    Returns regression results dict or None if insufficient data.
    """
    regression_data = []
    for site_id, result in results.items():
        if (result.get('success') and result.get('slope') is not None and
            result.get('slope') > 0 and  # Exclude negative slopes: insufficient data
            result.get('avg_adjusted_hours_per_day') is not None):
            regression_data.append({
                'site_id': site_id,
                'slope': result['slope'],
                'avg_adjusted_hours': result['avg_adjusted_hours_per_day'],
                'pm10': result.get('air_quality', {}).get('pm10') if result.get('air_quality') else None,
                'pm25': result.get('air_quality', {}).get('pm25') if result.get('air_quality') else None,
            })

    # Count excluded sites (negative slopes)
    excluded_count = sum(1 for r in results.values() if r.get('success') and r.get('slope') is not None and r.get('slope') <= 0)
    if excluded_count > 0:
        logging.info(f"Excluded {excluded_count} sites with non-positive slopes from regression analysis (insufficient data)")

    if len(regression_data) < 2:
        logging.warning(f"Insufficient sites with positive slope for regression ({len(regression_data)} sites with positive slope out of {len(results)} total)")
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
        valid_idx = (~np.isnan(pm10_vals)) & (~np.isnan(pm25_vals))
        X = np.column_stack([adjusted_hours[valid_idx], pm10_vals[valid_idx], pm25_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM10', 'PM2.5']
        model_type = "3-factor (adjusted_hours + PM10 + PM2.5)"
    elif has_pm25:
        valid_idx = ~np.isnan(pm25_vals)
        X = np.column_stack([adjusted_hours[valid_idx], pm25_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM2.5']
        model_type = "2-factor (adjusted_hours + PM2.5)"
    elif has_pm10:
        valid_idx = ~np.isnan(pm10_vals)
        X = np.column_stack([adjusted_hours[valid_idx], pm10_vals[valid_idx]])
        y = slopes[valid_idx]
        feature_names = ['adjusted_hours', 'PM10']
        model_type = "2-factor (adjusted_hours + PM10)"
    else:
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

    result = {
        'model_type': model_type,
        'sites_analyzed': len(y),
        'intercept': float(intercept),
        'r_squared': float(r_squared),
        'residual_std_error': float(residual_std_error),
    }

    result['coefficient_adjusted_hours'] = float(coefs[feature_names.index('adjusted_hours')])
    result['coefficient_pm10'] = float(coefs[feature_names.index('PM10')]) if 'PM10' in feature_names else None
    result['coefficient_pm25'] = float(coefs[feature_names.index('PM2.5')]) if 'PM2.5' in feature_names else None

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

def apply_pollution_effect_to_rul(results, regression_results):
    """
    For each site with air quality data, recalculate RUL using the pollution effect multiplier.
    effect = β_pm25 × PM2.5 + β_pm10 × PM10
    adjusted_slope = raw_slope × (1 + effect)
    """
    if not regression_results or not regression_results.get('coefficient_pm10') and not regression_results.get('coefficient_pm25'):
        logging.info("No air quality coefficients available; using raw slopes for all sites")
        return

    beta_pm10 = regression_results.get('coefficient_pm10') or 0.0
    beta_pm25 = regression_results.get('coefficient_pm25') or 0.0

    adjusted_count = 0
    for site_id, result in results.items():
        if not result.get('success'):
            continue

        air_quality = result.get('air_quality')
        if not air_quality:
            continue

        pm10 = air_quality.get('pm10')
        pm25 = air_quality.get('pm25')

        if pm10 is None and pm25 is None:
            continue

        # Calculate pollution effect
        pm10_effect = (beta_pm10 * pm10) if pm10 is not None else 0.0
        pm25_effect = (beta_pm25 * pm25) if pm25 is not None else 0.0
        pollution_effect = pm10_effect + pm25_effect

        raw_slope = result.get('slope')
        if raw_slope is None:
            continue

        adjusted_slope = raw_slope * (1.0 + pollution_effect)
        result['adjusted_slope'] = float(adjusted_slope)
        result['pollution_effect'] = float(pollution_effect)

        # Recalculate RUL using adjusted slope
        if result.get('r2', 0) >= R2_THRESHOLD:
            current_dt = result.get('current_dt')
            current_hours = result.get('total_adjusted_hours')
            avg_hours_per_day = result.get('avg_adjusted_hours_per_day')

            if adjusted_slope <= 0:
                result['rul_days'] = 999.0
                result['urgency'] = 'OK'
            elif current_dt >= FAILURE_DT:
                result['rul_days'] = 0.0
                result['urgency'] = 'URGENT'
            else:
                hours_to_failure = (FAILURE_DT - result.get('intercept', 0)) / adjusted_slope if adjusted_slope > 0 else 999
                remaining_hours = hours_to_failure - current_hours

                days_to_failure = remaining_hours / avg_hours_per_day if avg_hours_per_day > 0 else 999

                rul_days = max(0, days_to_failure)

                result['rul_days'] = float(rul_days)

                if rul_days < 14:
                    result['urgency'] = 'URGENT'
                elif rul_days < 30:
                    result['urgency'] = 'WARNING'
                else:
                    result['urgency'] = 'OK'

            adjusted_count += 1

    logging.info(f"Applied pollution effect adjustment to {adjusted_count} sites with air quality data")

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main entry point."""
    import sys

    inventory_csv = '../sites_inventory.csv'
    coords_csv = '../sites_inventory_2.csv'
    output_file = 'sites_data.json'
    max_workers = 10

    # Check for test mode: --test-site SITE_ID
    test_site_id = None
    if len(sys.argv) > 1 and sys.argv[1] == '--test-site' and len(sys.argv) > 2:
        test_site_id = sys.argv[2]
        max_workers = 1

    logging.info("=" * 80)
    logging.info("Rogers HVAC RUL Multi-Site Query + Air Quality Analysis")
    logging.info("=" * 80)

    # Load inventory with coordinates
    sites = load_inventory(inventory_csv, coords_csv)
    sites_with_coords = sum(1 for s in sites if s.get('latitude') and s.get('longitude'))
    logging.info(f"Total sites loaded: {len(sites)}")
    logging.info(f"Sites with coordinates: {sites_with_coords}/{len(sites)}")

    # Test mode: run single site with verbose output
    if test_site_id:
        logging.info("")
        logging.info("=" * 80)
        logging.info(f"TEST MODE: Querying single site {test_site_id}")
        logging.info("=" * 80)
        matching_sites = [s for s in sites if s['site_id'] == test_site_id]
        if not matching_sites:
            logging.error(f"Site {test_site_id} not found in inventory")
            return
        site = matching_sites[0]
        result = query_site_complete(site, SITE_PASSWORD, WEATHERBIT_API_KEY, debug_first_failure=True)
        logging.info("")
        logging.info("=" * 80)
        logging.info(f"Test Result for {test_site_id}:")
        logging.info("=" * 80)
        for key, value in result.items():
            if key not in ['episodes', 'air_quality_data_points']:  # Skip large nested data
                logging.info(f"  {key}: {value}")
        return

    logging.info(f"Starting parallel queries ({max_workers} concurrent workers)...")
    logging.info(f"Estimated time: ~{len(sites) / max_workers / 2:.0f} minutes for {len(sites)} sites")
    logging.info("")

    # Query all sites (debug_first_failure=True shows raw curl responses for first few failures)
    start_time = datetime.now()
    results, completed, failed = query_all_sites_parallel(sites, SITE_PASSWORD, WEATHERBIT_API_KEY, max_workers, debug_first_failure=True)
    elapsed = (datetime.now() - start_time).total_seconds()

    logging.info("")
    logging.info("=" * 80)
    logging.info("Query Complete")
    logging.info("=" * 80)
    logging.info(f"Elapsed time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
    logging.info(f"Completed: {completed}/{len(sites)} sites")
    logging.info(f"Failed: {failed}/{len(sites)} sites")

    # Aggregate and save
    successful_sites = {k: v for k, v in results.items() if v.get('success')}
    failed_sites = {k: v for k, v in results.items() if not v.get('success')}
    
    sites_with_aq = sum(1 for r in successful_sites.values() if r.get('air_quality'))
    sites_with_episode_times = sum(1 for r in successful_sites.values() if r.get('episode_start_times'))

    urgency_counts = {
        'URGENT': sum(1 for r in successful_sites.values() if r.get('urgency') == 'URGENT'),
        'WARNING': sum(1 for r in successful_sites.values() if r.get('urgency') == 'WARNING'),
        'OK': sum(1 for r in successful_sites.values() if r.get('urgency') == 'OK'),
    }

    # Run air quality regression and apply pollution effect to RUL
    regression_results = None
    if WEATHERBIT_API_KEY and sites_with_aq > 0:
        regression_results = run_air_quality_regression(results)
        if regression_results:
            logging.info("")
            logging.info("=" * 80)
            logging.info("Air Quality Regression Analysis (Slope ~ PM10 + PM2.5)")
            logging.info("=" * 80)
            logging.info(f"Sites with air quality data: {regression_results['sites_analyzed']}")
            logging.info(f"R² (variance explained): {regression_results['r_squared']:.4f}")
            logging.info(f"Model: {regression_results['model_type']}")
            logging.info(f"Coefficients: {regression_results['interpretation']}")

            logging.info("Applying pollution effect multiplier to RUL calculations...")
            apply_pollution_effect_to_rul(results, regression_results)

    output = {
        'query_timestamp': datetime.now().isoformat(),
        'query_elapsed_seconds': elapsed,
        'sites_queried': completed,
        'sites_total': len(sites),
        'sites_failed': failed,
        'sites_with_air_quality': sites_with_aq,
        'sites_with_episode_dates': sites_with_episode_times,
        'urgency_summary': urgency_counts,
        'air_quality_regression': regression_results,
        'sites': results,
    }

    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    logging.info(f"\nSaved results to {output_file}")

    # Print summary
    logging.info("")
    logging.info("=" * 80)
    logging.info("Success Summary")
    logging.info("=" * 80)
    logging.info(f"🔴 URGENT (< 14d):    {urgency_counts['URGENT']:>4} sites")
    logging.info(f"🟡 WARNING (14-30d):  {urgency_counts['WARNING']:>4} sites")
    logging.info(f"🟢 OK (≥ 30d):        {urgency_counts['OK']:>4} sites")
    logging.info(f"⚪ Unknown/Failed:    {failed:>4} sites")
    logging.info(f"🌍 With Air Quality:  {sites_with_aq:>4} sites")
    logging.info(f"📅 With Episode Dates: {sites_with_episode_times:>4} sites")

    # Failure analysis
    if failed_sites:
        logging.info("")
        logging.info("=" * 80)
        logging.info("Failure Analysis by Category")
        logging.info("=" * 80)

        failure_codes = {}
        for site_id, result in failed_sites.items():
            code = result.get('error_code', 'UNKNOWN')
            message = result.get('error_message', result.get('error', 'Unknown'))
            key = f"{code}: {message}"
            if key not in failure_codes:
                failure_codes[key] = []
            failure_codes[key].append(site_id)

        for reason, site_ids in sorted(failure_codes.items(), key=lambda x: -len(x[1])):
            logging.error(f"{len(site_ids):>4} sites: {reason}")
            if len(site_ids) <= 3:
                logging.error(f"        Examples: {', '.join(site_ids[:3])}")
    
    logging.info("")
    logging.info("=" * 80)
    logging.info("Query Complete")
    logging.info("=" * 80)
if __name__ == '__main__':
    main()
