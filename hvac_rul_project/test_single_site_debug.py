#!/usr/bin/env python3
"""
Test a single site with full debug output.
NO Weatherbit calls - just SSH + InfluxDB + episode extraction.
"""

import os
import sys
import json
import logging
import paramiko
import socket
from dotenv import load_dotenv
from datetime import datetime
import pandas as pd
import numpy as np

# Enable DEBUG logging to see everything
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

# Configuration
SSH_TIMEOUT = 30
QUERY_TIMEOUT = 60
QUERY_DAYS = 90
FAN_THRESHOLD = 95.0
MIN_EPISODE_MINUTES = 30.0

def query_site_influxdb(site_ip, site_id, password):
    """SSH into site and query InfluxDB 1.x for HVAC data."""
    try:
        logging.info(f"\n{'='*80}")
        logging.info(f"Testing {site_id} ({site_ip})")
        logging.info(f"{'='*80}\n")

        # SSH connection
        logging.info(f"[1] Connecting to {site_ip}...")
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
            logging.info(f"[1] ✓ SSH connection successful")
        except Exception as e:
            logging.error(f"[1] ✗ SSH failed: {type(e).__name__}: {e}")
            return None

        # Test 1.5: Check if InfluxDB is running
        logging.info(f"\n[1.5] Testing InfluxDB connectivity...")
        cmd_test = 'curl -s -m 5 http://localhost:8086/ping'
        logging.debug(f"[1.5] Command: {cmd_test}")
        try:
            stdin, stdout, stderr = ssh.exec_command(cmd_test, timeout=15)
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            logging.info(f"[1.5] ping response ({len(output)} bytes): {output[:200]}")
            if error:
                logging.warning(f"[1.5] stderr: {error[:200]}")
        except Exception as e:
            logging.warning(f"[1.5] ping failed: {e}")

        # Try both databases
        for database in ['aque', 'hvac']:
            logging.info(f"\n[2] Querying {database} database...")
            query = f"SELECT * FROM hvac WHERE time > now() - {QUERY_DAYS}d"
            cmd = f'curl -s -m 10 -G "http://localhost:8086/query?db={database}" --data-urlencode "q={query}"'

            logging.debug(f"[2] Command: {cmd}")

            try:
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=QUERY_TIMEOUT)
                output = stdout.read().decode('utf-8')
                error = stderr.read().decode('utf-8')

                logging.info(f"[2] curl returned {len(output)} bytes")

                if error:
                    logging.warning(f"[2] curl stderr: {error[:200]}")

                if not output:
                    logging.warning(f"[2] {database}: No output (InfluxDB not running or no data)")
                    continue

                # Show first 500 chars of response
                logging.debug(f"[2] Response (first 500 chars): {output[:500]}")

                # Parse JSON
                response = json.loads(output)
                logging.info(f"[2] ✓ JSON parsed successfully")

                if 'results' not in response:
                    logging.warning(f"[2] No 'results' key in response")
                    continue

                if not response['results']:
                    logging.warning(f"[2] Empty results array")
                    continue

                result = response['results'][0]

                if 'error' in result:
                    logging.warning(f"[2] InfluxDB error: {result['error']}")
                    continue

                if 'series' not in result:
                    logging.warning(f"[2] No 'series' in result. Keys: {list(result.keys())}")
                    continue

                if not result['series']:
                    logging.warning(f"[2] 'series' is empty")
                    continue

                series = result['series'][0]
                logging.info(f"[2] ✓ Got series with {len(series.get('values', []))} rows")
                logging.info(f"[2] Columns: {series.get('columns', [])}")
                logging.info(f"[2] Tags: {list(series.get('tags', {}).keys())}")

                # Build DataFrame
                columns = series.get('columns', [])
                values = series.get('values', [])
                tags = series.get('tags', {})

                df = pd.DataFrame(values, columns=columns)
                for tag_key, tag_value in tags.items():
                    df[tag_key] = tag_value

                if 'time' in df.columns:
                    df['time'] = pd.to_datetime(df['time'], utc=True)

                logging.info(f"[2] ✓ DataFrame created: {df.shape}")
                logging.info(f"[2] Columns: {df.columns.tolist()}")

                ssh.close()
                return df

            except json.JSONDecodeError as e:
                logging.warning(f"[2] JSON parse error: {e}")
                logging.warning(f"[2] Response: {output[:500]}")
                continue
            except Exception as e:
                logging.warning(f"[2] Error: {type(e).__name__}: {e}")
                continue

        ssh.close()
        logging.error(f"✗ Failed to retrieve data from both databases")
        return None

    except Exception as e:
        logging.error(f"✗ Unexpected error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None

if __name__ == '__main__':
    site_id = sys.argv[1] if len(sys.argv) > 1 else 'X1758'
    site_ip = sys.argv[2] if len(sys.argv) > 2 else '10.252.196.116'

    logging.info(f"Testing site: {site_id} ({site_ip})")

    df = query_site_influxdb(site_ip, site_id, SITE_PASSWORD)

    if df is not None:
        logging.info(f"\n✓ Success! Got {len(df)} rows")
        logging.info(f"  Time range: {df['time'].min()} to {df['time'].max()}")
        logging.info(f"  Columns: {df.columns.tolist()}")
    else:
        logging.error(f"\n✗ Failed to get data")
