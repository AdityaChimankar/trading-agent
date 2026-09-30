print("=== API ENDPOINT CHECK ===")
print()

# Test API router imports
from api.routers import watchlist, positions, opportunities, symbols, health, digest, history, pipeline, wallet, liveness
print("1. All router modules import OK")

# Test serializers
from api.serializers import jsonable
print("2. Serializers OK")

# Test main app
from api.main import app
print("3. FastAPI app OK")

# Test specific endpoints
from api.routers.symbols import all_news
news = all_news(limit=5)
print("4. /api/symbols/news: {} items".format(len(news)))

from api.routers.opportunities import opportunities
ops = opportunities(100000, n=3)
print("5. /api/opportunities: bullish={}, bearish={}".format(len(ops["bullish"]), len(ops["bearish"])))

from api.routers.positions import list_positions
pos = list_positions()
print("6. /api/positions: {} open".format(len(pos)))

from api.routers.watchlist import watchlist
wl = watchlist()
print("7. /api/watchlist: {} symbols".format(len(wl)))

from api.routers.health import health_check
health = health_check()
print("8. /api/health: {}".format(health))

from api.routers.health import health_ready
ready = health_ready()
print("9. /api/health/ready: feed_status={}, market_open={}".format(ready["feed_status"], ready["market_open"]))

print()
print("=== ALL API ENDPOINTS WORKING ===")