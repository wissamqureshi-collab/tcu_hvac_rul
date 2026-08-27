#!/usr/bin/env python3
"""
Get the most recent data point to check if data is current.
"""

import os
import sys
import json
import paramiko
from dotenv import load_dotenv

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

def check_latest_data(site_ip, site_id):
    """Get the most recent data point."""
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

        # Get latest data point
        print("\n[1] Getting most recent data point...")
        cmd = 'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT * FROM hvac ORDER BY time DESC LIMIT 1"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')

        print(f"Response ({len(output)} bytes):")
        print(output[:1000])

        try:
            result = json.loads(output)
            if 'results' in result and result['results']:
                series = result['results'][0].get('series', [])
                if series and series[0].get('values'):
                    row = series[0]['values'][0]
                    print(f"\nLatest timestamp: {row[0]}")
                    print(f"Full row: {row}")
        except json.JSONDecodeError:
            pass

        ssh.close()

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    site_id = sys.argv[1] if len(sys.argv) > 1 else 'X1758'
    site_ip = sys.argv[2] if len(sys.argv) > 2 else '10.252.196.116'
    check_latest_data(site_ip, site_id)
