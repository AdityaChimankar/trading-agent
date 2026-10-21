import sqlite3
conn = sqlite3.connect('data/trading_agent.db')
c = conn.cursor()

# Check ingest_status schema
c.execute("PRAGMA table_info(ingest_status)")
print("=== ingest_status ===")
for r in c.fetchall():
    print(r)

# Check candles schema
c.execute("PRAGMA table_info(candles)")
print("\n=== candles ===")
for r in c.fetchall():
    print(r)