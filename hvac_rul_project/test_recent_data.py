#!/usr/bin/env python3
"""
Check if there's ANY data from recent years (2024-2026).
"""

import os
import sys
import json
import paramiko
from dotenv import load_dotenv

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

def check_recent_data(site_ip, site_id):
    """Check for data from 2024-2026."""
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
        print(f"✓ Connected to {site_id}\n")

        # Test different year ranges
        year_ranges = [
            ("2026 (current year)", "2026-01-01", "2026-12-31"),
            ("2025", "2025-01-01", "2025-12-31"),
            ("2024", "2024-01-01", "2024-12-31"),
            ("2023", "2023-01-01", "2023-12-31"),
            ("2022", "2022-01-01", "2022-12-31"),
            ("2021", "2021-01-01", "2021-12-31"),
        ]

        for label, start, end in year_ranges:
            cmd = f'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT COUNT(*) FROM hvac WHERE time >= \'{start}T00:00:00Z\' AND time <= \'{end}T23:59:59Z\'"'
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
            output = stdout.read().decode('utf-8')

            try:
                result = json.loads(output)
                series = result.get('results', [{}])[0].get('series', [])
                if series and series[0].get('values'):
                    count = series[0]['values'][0][1]
                    print(f"{label}: {count} rows")
                else:
                    print(f"{label}: 0 rows (no series)")
            except:
                print(f"{label}: Error parsing response")

        ssh.close()

    except Exception as e:
        print(f"✗ Error: {e}")

if __name__ == '__main__':
    site_id = sys.argv[1] if len(sys.argv) > 1 else 'X1758'
    site_ip = sys.argv[2] if len(sys.argv) > 2 else '10.252.196.116'
    check_recent_data(site_ip, site_id)
