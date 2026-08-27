#!/usr/bin/env python3
"""
Check the actual time range of data in InfluxDB.
"""

import os
import sys
import json
import paramiko
from dotenv import load_dotenv

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

def check_time_range(site_ip, site_id):
    """Get min and max timestamp in aque.hvac."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(
            hostname=site_ip,
            port=22,
            username='plc',
            password=SITE_PASSWORD,
            timeout=30,
            auth_timeout=30,
            banner_timeout=30
        )
        print(f"✓ Connected to {site_id}")

        # Get min and max time
        print("\n[1] Checking data time range...")
        cmd = 'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT MIN(time), MAX(time) FROM hvac"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')

        try:
            result = json.loads(output)
            if 'results' in result and result['results']:
                series = result['results'][0].get('series', [])
                if series:
                    values = series[0].get('values', [])
                    if values:
                        min_time, max_time = values[0]
                        print(f"Data time range: {min_time} to {max_time}")
                    else:
                        print("No values in response")
                else:
                    print("No series in response")
            else:
                print(f"Response: {output[:300]}")
        except json.JSONDecodeError:
            print(f"Could not parse JSON: {output[:300]}")

        # Get row count
        print("\n[2] Checking total row count...")
        cmd = 'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT COUNT(*) FROM hvac"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')

        try:
            result = json.loads(output)
            if 'results' in result and result['results']:
                series = result['results'][0].get('series', [])
                if series:
                    values = series[0].get('values', [])
                    if values:
                        count = values[0][1]
                        print(f"Total rows: {count}")
                    else:
                        print("No values in response")
                else:
                    print("No series in response")
            else:
                print(f"Response: {output[:300]}")
        except json.JSONDecodeError:
            print(f"Could not parse JSON: {output[:300]}")

        ssh.close()

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    site_id = sys.argv[1] if len(sys.argv) > 1 else 'X1758'
    site_ip = sys.argv[2] if len(sys.argv) > 2 else '10.252.196.116'
    check_time_range(site_ip, site_id)
