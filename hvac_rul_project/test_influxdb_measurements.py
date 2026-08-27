#!/usr/bin/env python3
"""
Test which measurements exist in InfluxDB aque database.
"""

import os
import sys
import paramiko
from dotenv import load_dotenv

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

def test_influxdb_measurements(site_ip, site_id):
    """Check what measurements exist in aque database."""
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

        # Test 1: List databases
        print("\n[1] Listing all databases:")
        cmd = 'curl -s -m 5 "http://localhost:8086/query?q=SHOW%20DATABASES"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')
        print(f"Response: {output}")

        # Test 2: List measurements in aque
        print("\n[2] Listing measurements in aque database:")
        cmd = 'curl -s -m 5 "http://localhost:8086/query?db=aque&q=SHOW%20MEASUREMENTS"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')
        print(f"Response: {output}")

        # Test 3: Get one row from hvac measurement
        print("\n[3] Querying first row from aque.hvac:")
        cmd = 'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT * FROM hvac LIMIT 1"'
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=15)
        output = stdout.read().decode('utf-8')
        print(f"Response ({len(output)} bytes): {output[:500]}")

        ssh.close()

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    site_id = sys.argv[1] if len(sys.argv) > 1 else 'X1758'
    site_ip = sys.argv[2] if len(sys.argv) > 2 else '10.252.196.116'
    test_influxdb_measurements(site_ip, site_id)
