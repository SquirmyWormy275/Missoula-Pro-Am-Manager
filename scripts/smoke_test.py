#!/usr/bin/env python3
"""Post-deploy smoke test.

Hits key endpoints on the production URL and verifies they return expected
status codes. Run after every Railway deploy to catch "deployed but broken."

Usage:
    python scripts/smoke_test.py                          # uses PRODUCTION_URL env var
    python scripts/smoke_test.py https://example.up.railway.app

Exit code 0 = all checks passed, 1 = one or more failed.
"""
from __future__ import annotations

import sys
import time
import urllib.request
import urllib.error
import json


def check(base_url: str, path: str, expected_status: int = 200,
          json_key: str | None = None, json_value=None) -> dict:
    """Hit one endpoint and return a result dict."""
    url = base_url.rstrip('/') + path
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ProAm-SmokeTest/1.0'})
        resp = urllib.request.urlopen(req, timeout=15)
        status = resp.status
        body = resp.read().decode('utf-8', errors='replace')
        elapsed = time.time() - start
    except urllib.error.HTTPError as e:
        status = e.code
        body = ''
        elapsed = time.time() - start
    except Exception as exc:
        return {
            'path': path, 'ok': False, 'status': 0,
            'error': str(exc), 'elapsed_ms': int((time.time() - start) * 1000),
        }

    ok = (status == expected_status)

    # Optional JSON field check
    if ok and json_key is not None:
        try:
            data = json.loads(body)
            if data.get(json_key) != json_value:
                ok = False
        except (json.JSONDecodeError, KeyError):
            ok = False

    return {
        'path': path, 'ok': ok, 'status': status,
        'elapsed_ms': int(elapsed * 1000), 'error': None,
    }


def main():
    import os
    base_url = (
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get('PRODUCTION_URL', 'https://missoula-pro-am-manager-production.up.railway.app')
    )

    print(f'Smoke testing: {base_url}')
    print('=' * 60)

    checks = [
        {'path': '/health', 'json_key': 'db', 'json_value': True},
        {'path': '/', 'expected_status': 200},
        {'path': '/api/public/tournaments', 'expected_status': 200},
        {'path': '/portal/', 'expected_status': 200},
    ]

    results = []
    for c in checks:
        result = check(base_url, **c)
        results.append(result)
        icon = 'PASS' if result['ok'] else 'FAIL'
        status_str = f"HTTP {result['status']}" if result['status'] else result['error']
        print(f"  [{icon}] {result['path']:40s} {status_str:10s} ({result['elapsed_ms']}ms)")

    print('=' * 60)
    passed = sum(1 for r in results if r['ok'])
    total = len(results)
    print(f'Result: {passed}/{total} checks passed')

    if passed < total:
        print('\nFAILED CHECKS:')
        for r in results:
            if not r['ok']:
                print(f"  - {r['path']}: {r.get('error') or f'HTTP {r['status']}'}")
        sys.exit(1)
    else:
        print('All smoke tests passed.')
        sys.exit(0)


if __name__ == '__main__':
    main()
