from datetime import UTC, datetime
from pathlib import Path
import sqlite3

root = Path('/app/data/backups')
root.mkdir(parents=True, exist_ok=True)
target = root / ('pre-schema-002-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.db')
with sqlite3.connect('file:/app/data/trading.db?mode=ro', uri=True) as source:
    with sqlite3.connect(target) as destination:
        source.backup(destination)
        assert destination.execute('PRAGMA integrity_check').fetchone() == ('ok',)
print(target)
