#!/usr/bin/env python3

"""
Quick 50-site test query with detailed step-by-step status messages.
Much faster than full 1020-site run for debugging.
"""

import os
import sys
import json
import logging
import paramiko
import socket
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv

# ============================================================================
# SETUP
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Simpler format for clearer output
)

# Load credentials from .env
load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')
if not SITE_PASSWORD:
    logging.error("✗ Missing SITE_PASSWORD in .env file")
    sys.exit(1)

# Configuration
FAN_THRESHOLD = 95.0
MIN_EPISODE_MINUTES = 30.0
FAILURE_DT = 10.0
SSH_TIMEOUT = 30
QUERY_TIMEOUT = 60
QUERY_DAYS = 90

# ============================================================================
# LOAD INVENTORY
# ============================================================================

def load_inventory(csv_path):
    """Load site inventory CSV."""
    possible_paths = [
        csv_path,
        os.path.join('..', csv_path),
        os.path.join('hvac_rul_project', csv_path),
    ]

    actual_path = None
    for p in possible_paths:
        if os.path.exists(p):
            actual_path = p
            break

    if not actual_path:
        logging.error(f"✗ Inventory file not found: {csv_path}")
        sys.exit(1)

    df = pd.read_csv(actual_path)
    df.columns = df.columns.str.strip().str.lower()

    sites = []
    for _, row in df.iterrows():
        ip = row.get('ip address') or row.get('ip')
        site_id = row.get('site') or row.get('device name')
        site_name = row.get('site name') or site_id

        if ip and site_id:
            sites.append({
                'ip': ip,
                'site_id': site_id,
                'site_name': site_name or site_id,
            })

    return sites

# ============================================================================
# QUERY & EXTRACT
# ============================================================================

def query_site_influxdb(site_id, site_ip, password):
    """
    SSH into site and query InfluxDB.
    Returns (success: bool, data_df: DataFrame, status_messages: list)
    """
    messages = []

    # Step 1: SSH Connection
    messages.append(f"  [1/5] Connecting to {site_ip} via SSH...")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=site_ip,
            port=22,
            username='plc',
            password=password,
            timeout=SSH_TIMEOUT
        )
        messages.append(f"        ✓ SSH connection successful")
    except paramiko.AuthenticationException:
        messages.append(f"        ✗ SSH authentication failed (check plc user/password)")
        return False, None, messages
    except (socket.timeout, TimeoutError):
        messages.append(f"        ✗ SSH timeout ({SSH_TIMEOUT}s) - site unreachable")
        return False, None, messages
    except paramiko.SSHException as e:
        messages.append(f"        ✗ SSH error: {e}")
        return False, None, messages
    except Exception as e:
        messages.append(f"        ✗ Unexpected error: {e}")
        return False, None, messages

    # Step 2-3: Try both databases
    for database in ['aque', 'hvac']:
        messages.append(f"  [2/5] Querying {database} database...")
        query = f"SELECT * FROM hvac WHERE time > now() - {QUERY_DAYS}d"
        cmd = f'curl -s -G "http://localhost:8086/query?db={database}" --data-urlencode "q={query}"'

        try:
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=QUERY_TIMEOUT)
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
        except socket.timeout:
            messages.append(f"        ✗ Query timeout ({QUERY_TIMEOUT}s) - InfluxDB unresponsive")
            continue
        except Exception as e:
            messages.append(f"        ✗ Failed to execute: {e}")
            continue

        if error:
            messages.append(f"        ✗ curl error: {error}")
            continue

        if not output:
            messages.append(f"        ✗ No output from InfluxDB (service may not be running)")
            continue

        messages.append(f"        ✓ Got {len(output)} bytes from InfluxDB")

        # Step 3: Parse JSON
        messages.append(f"  [3/5] Parsing response...")
        try:
            response = json.loads(output)
            if 'results' not in response or not response['results']:
                messages.append(f"        ✗ Invalid response structure: {list(response.keys())}")
                continue

            result = response['results'][0]

            if 'error' in result:
                messages.append(f"        ✗ InfluxDB error: {result['error']}")
                continue

            if 'series' not in result or not result['series']:
                messages.append(f"        ✗ No data series found (measurement may not exist)")
                continue

            messages.append(f"        ✓ Found {len(result['series'])} data series")

            # Step 4: Build DataFrame
            messages.append(f"  [4/5] Building DataFrame...")
            df_list = []
            for series_idx, series in enumerate(result['series']):
                columns = series.get('columns', [])
                values = series.get('values', [])
                tags = series.get('tags', {})

                if not columns or not values:
                    continue

                df = pd.DataFrame(values, columns=columns)
                for tag_key, tag_value in tags.items():
                    df[tag_key] = tag_value

                if 'time' in df.columns:
                    df['time'] = pd.to_datetime(df['time'], utc=True)
                if 'value' in df.columns:
                    df['value'] = pd.to_numeric(df['value'], errors='coerce')

                df_list.append(df)

            if not df_list:
                messages.append(f"        ✗ No usable data found")
                continue

            df = pd.concat(df_list, ignore_index=True) if len(df_list) > 1 else df_list[0]
            messages.append(f"        ✓ DataFrame built: {len(df)} rows, columns: {df.columns.tolist()}")

            ssh.close()
            return True, df, messages

        except json.JSONDecodeError as je:
            messages.append(f"        ✗ JSON parse error: {je}")
            messages.append(f"        Response preview: {output[:200]}")
            continue
        except Exception as e:
            messages.append(f"        ✗ Processing error: {e}")
            continue

    ssh.close()
    return False, None, messages

def extract_episodes(df):
    """Extract freecooling episodes from DataFrame."""
    messages = []

    if df is None or len(df) < 2:
        messages.append(f"  [5/5] Extracting episodes...")
        messages.append(f"        ✗ DataFrame is None or too small")
        return [], messages

    messages.append(f"  [5/5] Extracting episodes...")

    # Find pivot column
    pivot_col = None
    for col_name in ['display_point', 'equipment_id', 'alias']:
        if col_name in df.columns:
            pivot_col = col_name
            break

    if not pivot_col:
        messages.append(f"        ✗ No pivot column found (tried: display_point, equipment_id, alias)")
        messages.append(f"        Available columns: {df.columns.tolist()}")
        return [], messages

    messages.append(f"        ✓ Pivot column: {pivot_col}")

    # Pivot
    try:
        df_pivot = df.pivot_table(
            index='time',
            columns=pivot_col,
            values='value',
            aggfunc='first'
        ).reset_index()
    except Exception as e:
        messages.append(f"        ✗ Pivot failed: {e}")
        return [], messages

    df_pivot = df_pivot.sort_values('time').reset_index(drop=True)

    # Find critical sensors
    fan_col = next((c for c in ['fan_status', 'fan', 'supply_fan_speed'] if c in df_pivot.columns), None)
    fc_col = next((c for c in ['hvac_FREE_COOL_MODE', 'free_cool_mode', 'fc_mode'] if c in df_pivot.columns), None)
    dt_col = next((c for c in ['hvac_DELTA_T', 'delta_t', 'dt'] if c in df_pivot.columns), None)

    if not (fan_col and fc_col and dt_col):
        missing = []
        if not fan_col: missing.append('fan_status')
        if not fc_col: missing.append('hvac_FREE_COOL_MODE')
        if not dt_col: missing.append('hvac_DELTA_T')
        messages.append(f"        ✗ Missing critical sensors: {missing}")
        messages.append(f"        Available after pivot: {df_pivot.columns.tolist()}")
        return [], messages

    messages.append(f"        ✓ Found sensors: fan_col={fan_col}, fc_col={fc_col}, dt_col={dt_col}")

    # Convert to numeric
    df_pivot[fan_col] = pd.to_numeric(df_pivot[fan_col], errors='coerce')
    df_pivot[fc_col] = pd.to_numeric(df_pivot[fc_col], errors='coerce')
    df_pivot[dt_col] = pd.to_numeric(df_pivot[dt_col], errors='coerce')

    # Extract episodes
    df_pivot['in_episode'] = (
        (df_pivot[fan_col] >= FAN_THRESHOLD) &
        (df_pivot[fc_col] == 1.0)
    )
    df_pivot['episode_id'] = (~df_pivot['in_episode']).cumsum()

    episodes = []
    for ep_id, group in df_pivot[df_pivot['in_episode']].groupby('episode_id'):
        group = group.dropna(subset=[dt_col, fan_col])
        if len(group) < 2:
            continue

        start_time = group['time'].iloc[0]
        end_time = group['time'].iloc[-1]
        duration_min = (end_time - start_time).total_seconds() / 60.0

        if duration_min < MIN_EPISODE_MINUTES:
            continue

        max_dt = float(group[dt_col].max())
        fan_speed_pct = group[fan_col].values / 100.0
        fan_weighting = fan_speed_pct ** 2
        adjusted_runtime_hours = (duration_min * np.mean(fan_weighting)) / 60.0

        episodes.append({
            'start_time': start_time,
            'end_time': end_time,
            'duration_min': duration_min,
            'max_dt': max_dt,
            'mean_dt': float(group[dt_col].mean()),
            'adjusted_runtime_hours': adjusted_runtime_hours,
        })

    messages.append(f"        ✓ Extracted {len(episodes)} episodes")
    return episodes, messages

# ============================================================================
# MAIN
# ============================================================================

def main():
    logging.info("")
    logging.info("=" * 80)
    logging.info("HVAC RUL 50-Site Quick Test (Fast Debugging)")
    logging.info("=" * 80)
    logging.info("")

    # Load inventory
    sites = load_inventory('../sites_inventory.csv')
    logging.info(f"Loaded {len(sites)} sites from inventory")
    logging.info(f"Testing first 50 sites...\n")

    # Test first 50 sites
    results = {
        'success': [],
        'failed': [],
    }

    for idx, site in enumerate(sites[:50], 1):
        site_id = site['site_id']
        site_ip = site['ip']

        logging.info(f"[{idx:2d}/50] {site_id} ({site_ip})")

        # Query InfluxDB
        success, df, msg_list = query_site_influxdb(site_id, site_ip, SITE_PASSWORD)
        for msg in msg_list:
            logging.info(msg)

        if not success:
            logging.info(f"        ✗ FAILED at data retrieval\n")
            results['failed'].append({
                'site_id': site_id,
                'reason': msg_list[-1] if msg_list else 'Unknown'
            })
            continue

        # Extract episodes
        episodes, ep_msg_list = extract_episodes(df)
        for msg in ep_msg_list:
            logging.info(msg)

        if len(episodes) < 3:
            logging.info(f"        ✗ FAILED: insufficient episodes ({len(episodes)}/3)\n")
            results['failed'].append({
                'site_id': site_id,
                'reason': f'Insufficient episodes: {len(episodes)}/3'
            })
            continue

        logging.info(f"        ✓ SUCCESS - {len(episodes)} episodes extracted\n")
        results['success'].append(site_id)

    # Summary
    logging.info("=" * 80)
    logging.info("Summary")
    logging.info("=" * 80)
    logging.info(f"✓ Successful: {len(results['success'])}/50 sites")
    logging.info(f"✗ Failed:    {len(results['failed'])}/50 sites")
    logging.info("")

    if results['failed']:
        logging.info("Failed Sites:")
        failure_reasons = {}
        for item in results['failed']:
            reason = item['reason']
            if reason not in failure_reasons:
                failure_reasons[reason] = []
            failure_reasons[reason].append(item['site_id'])

        for reason, sites_list in sorted(failure_reasons.items(), key=lambda x: -len(x[1])):
            logging.info(f"  [{len(sites_list):2d}] {reason}")
            if len(sites_list) <= 10:
                logging.info(f"        Sites: {', '.join(sites_list)}")

    logging.info("")

if __name__ == '__main__':
    main()
