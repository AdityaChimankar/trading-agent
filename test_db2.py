import sqlite3
from pathlib import Path
from datetime import datetime

db_path = Path('data/trading_agent.db')
conn = sqlite3.connect(db_path, timeout=5)
conn.row_factory = sqlite3.Row

# Check latest timestamps
latest = conn.execute('SELECT MAX(timestamp) as latest FROM candles').fetchone()
print(f'Latest: {latest["latest"]}')

# Check format
sample = conn.execute('SELECT timestamp FROM candles LIMIT 5').fetchall()
for s in sample:
    print(f'  {s["timestamp"]}')

# Check if any recent (using different date format)
recent = conn.execute("SELECT COUNT(*) FROM candles WHERE timestamp > '2026-10-01'").fetchone()[0]
print(f'Since 2026-10-01: {recent}')

# Check what the max date is
max_date = conn.execute("SELECT MAX(substr(timestamp, 1, 10)) as max_date FROM candles").fetchone()
print(f'Max date prefix: {max_date["max_date"]}')

conn.close()