import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

RENAMES = {'open_positions': 'position_metadata', 'signals': 'signal_metadata',
           'opportunities': 'opportunity_metadata', 'system_events': 'event_metadata'}

def snapshot(path, normalize=False):
    result = {}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'alembic_version'")]
        for table in tables:
            digest = hashlib.sha256()
            count = 0
            for record in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                row = dict(record)
                if normalize and table in RENAMES and 'metadata' in row:
                    row[RENAMES[table]] = row.pop('metadata')
                if normalize and table == 'trades' and 'updated_at' not in row:
                    row['updated_at'] = row['created_at']
                digest.update(json.dumps(row, sort_keys=True, default=str).encode())
                count += 1
            result[table] = (count, digest.hexdigest())
    return result

with tempfile.TemporaryDirectory() as folder:
    target = Path(folder) / 'migration-test.db'
    with sqlite3.connect('file:/backup.db?mode=ro', uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
    before = snapshot(target, normalize=True)
    env = os.environ | {'DB_URL': f'sqlite:///{target}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], env=env, check=True)
    assert snapshot(target) == before, 'Existing records changed unexpectedly'
    with sqlite3.connect(target) as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone() == ('002',)
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], env=env, check=True)
    assert snapshot(target) == before
    print('PASS: actual database copy migrated; all existing records preserved; integrity and repeat upgrade passed')
