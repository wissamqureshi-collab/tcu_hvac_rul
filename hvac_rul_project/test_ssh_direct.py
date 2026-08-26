#!/usr/bin/env python3
"""
Direct SSH test - helps diagnose if paramiko is the issue.
Tests a few sites with both paramiko and manual SSH commands.
"""

import os
import sys
import paramiko
import socket
import subprocess
from datetime import datetime
from dotenv import load_dotenv
import time

load_dotenv()
SITE_PASSWORD = os.getenv('SITE_PASSWORD')

if not SITE_PASSWORD:
    print("Missing SITE_PASSWORD in .env")
    sys.exit(1)

# Test these problematic sites from the 50-site run
test_sites = [
    ('T0991', '10.252.180.197'),      # SSH timeout in script
    ('X1758', '10.252.196.116'),      # SSH timeout in script
    ('W0244', '10.252.90.70'),        # SUCCESS
    ('E1094', '10.252.164.149'),      # SSH OK but InfluxDB timeout
]

print("=" * 80)
print("SSH Connectivity Test")
print("=" * 80)
print()

for site_id, site_ip in test_sites:
    print(f"{site_id} ({site_ip})")

    # Test 1: Native SSH command
    print("  [1] Native SSH (timeout 10s)...")
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=no',
             f'plc@{site_ip}', 'echo', 'SSH_OK'],
            capture_output=True,
            text=True,
            timeout=15
        )
        if result.returncode == 0:
            print("        ✓ Native SSH works")
        else:
            print(f"        ✗ Native SSH failed: {result.stderr[:100]}")
    except subprocess.TimeoutExpired:
        print("        ✗ Native SSH timeout")
    except Exception as e:
        print(f"        ✗ Native SSH error: {e}")

    # Test 2: Paramiko with short timeout
    print("  [2] Paramiko SSH (timeout 10s)...")
    try:
        start = time.time()
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=site_ip,
            port=22,
            username='plc',
            password=SITE_PASSWORD,
            timeout=10,
            auth_timeout=10,
            banner_timeout=10,
        )
        elapsed = time.time() - start
        print(f"        ✓ Paramiko SSH works ({elapsed:.1f}s)")
        ssh.close()
    except socket.timeout:
        elapsed = time.time() - start
        print(f"        ✗ Paramiko timeout after {elapsed:.1f}s")
    except paramiko.AuthenticationException as e:
        print(f"        ✗ Paramiko auth failed: {e}")
    except Exception as e:
        print(f"        ✗ Paramiko error: {type(e).__name__}: {e}")

    # Test 3: Quick curl test
    print("  [3] SSH + curl to InfluxDB (timeout 15s)...")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=site_ip,
            port=22,
            username='plc',
            password=SITE_PASSWORD,
            timeout=10,
        )

        start = time.time()
        stdin, stdout, stderr = ssh.exec_command(
            'curl -s -m 5 -G "http://localhost:8086/query?db=aque" --data-urlencode "q=SELECT * FROM hvac LIMIT 1"',
            timeout=15
        )
        output = stdout.read().decode('utf-8')
        error = stderr.read().decode('utf-8')
        elapsed = time.time() - start

        if output:
            print(f"        ✓ InfluxDB responds ({elapsed:.1f}s, {len(output)} bytes)")
        elif error:
            print(f"        ✗ InfluxDB error ({elapsed:.1f}s): {error[:80]}")
        else:
            print(f"        ✗ InfluxDB no response ({elapsed:.1f}s)")

        ssh.close()
    except Exception as e:
        print(f"        ✗ SSH+curl failed: {type(e).__name__}: {e}")

    print()

print("=" * 80)
print("Summary:")
print("- If Native SSH works but Paramiko times out: paramiko is the bottleneck")
print("- If both fail: network issue or site unreachable")
print("- If SSH works but curl times out: InfluxDB is unresponsive")
print("=" * 80)
