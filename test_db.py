import sqlite3
from pathlib import Path

db_path = Path('data/trading_agent.db')
conn = sqlite3.connect(db_path, timeout=5)
conn.row_factory = sqlite3.Row

# Check candle count quickly
c = conn.execute('SELECT COUNT(*) FROM candles LIMIT 1').fetchone()[0]
print(f'Candle count: {c}')

# Check watchlist
w = conn.execute('SELECT COUNT(*) FROM watchlist').fetchone()[0]
print(f'Watchlist: {w}')

# Check active symbols in last 24h with limit
active = conn.execute('SELECT COUNT(DISTINCT symbol) FROM candles WHERE timestamp > datetime("now", "-24 hours") LIMIT 1000').fetchone()[0]
print(f'Active 24h: {active}')

conn.close()