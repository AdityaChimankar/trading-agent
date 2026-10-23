import json
import sqlite3
from trading_agent.storage.db import Base

with sqlite3.connect('file:/app/data/trading.db?mode=ro', uri=True) as conn:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    print(json.dumps({'revision': conn.execute('SELECT version_num FROM alembic_version').fetchall() if 'alembic_version' in tables else None}))
    for table in Base.metadata.sorted_tables:
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table.name}")')}
        expected = set(table.columns.keys())
        print(json.dumps({'table': table.name, 'missing': sorted(expected-actual), 'extra': sorted(actual-expected)}))
