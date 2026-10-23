import hashlib
import json
import os
from pathlib import Path
from time import perf_counter

import httpx

headers = {}
token = os.environ.get('API_AUTH_TOKEN', '')
if token:
    headers['Authorization'] = f'Bearer {token}'
with httpx.Client(base_url='http://127.0.0.1:8000', headers=headers, timeout=15) as client:
    for route in ['/api/v1/health', '/api/v1/ready', '/api/v1/research/readiness', '/api/v1/liveness/feed']:
        start = perf_counter()
        response = client.get(route)
        result = {'route': route, 'status': response.status_code, 'seconds': round(perf_counter() - start, 3)}
        if response.is_success:
            body = response.json()
            if route.endswith('/research/readiness'):
                result['coverage'] = body.get('coverage')
            elif route.endswith('/feed'):
                result['feed'] = body
        print(json.dumps(result))
        response.raise_for_status()
    schema = client.get('/openapi.json')
    schema.raise_for_status()
    print(json.dumps({'strategy_lab_route': '/api/v1/strategy-lab/compare' in schema.json()['paths']}))
root = Path('/app/src/trading_agent')
digest = hashlib.sha256()
files = sorted(root.rglob('*.py'))
for path in files:
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(path.read_bytes().replace(b'\r\n', b'\n'))
print(json.dumps({'python_files': len(files), 'source_hash': digest.hexdigest()}))
