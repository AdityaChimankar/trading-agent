with open('src/trading_agent/core/freshness.py', 'r') as f:
    content = f.read()

constants = '''
from datetime import time as dtime
from zoneinfo import ZoneInfo

# Market hours constants
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

MINUTE_FMT = "%Y-%m-%dT%H:%M:00"
MINUTE_KEY_FMT = "%Y-%m-%dT%H:%M"

'''

# Insert after the imports section (after logger = get_logger(__name__))
new_content = content.replace('logger = get_logger(__name__)', 'logger = get_logger(__name__)\n\n' + constants)

with open('src/trading_agent/core/freshness.py', 'w') as f:
    f.write(new_content)
print('Done')