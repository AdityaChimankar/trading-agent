# Quick connectivity check for all major components
print("=== COMPONENT CONNECTIVITY CHECK ===")
print()

# 1. Database connection
from storage.db import get_connection, get_watchlist_symbols
conn = get_connection()
print("1. Database: OK")
conn.close()

# 2. Watchlist
symbols = get_watchlist_symbols()
print("2. Watchlist: {} symbols".format(len(symbols)))

# 3. Core indicators
from core.quant_indicators import latest_indicators
result = latest_indicators(symbols[0])
status = "OK" if "error" not in result else "ERROR: " + result["error"]
print("3. Quant Indicators: " + status)

# 4. Pattern detection
from core.pattern_detection import detect_patterns
patterns = detect_patterns(symbols[0])
print("4. Pattern Detection: OK (bias: {})".format(patterns["bias"]))

# 5. Signal generation
from strategy.decision_agent import decide
signal = decide(symbols[0])
print("5. Decision Agent: OK (action: {})".format(signal["action"]))

# 6. Position sizing
from core.position_sizing import calculate_position
size = calculate_position(symbols[0], "BUY", 100000)
print("6. Position Sizing: {}".format("OK" if size else "NO DATA"))

# 7. Portfolio risk
from risk.portfolio_risk import calculate_portfolio_adjusted_position
adj = calculate_portfolio_adjusted_position(symbols[0], "BUY", 100000)
print("7. Portfolio Risk: OK (blocked: {})".format(adj.blocked))

# 8. Opportunity finder
from analysis.opportunity_finder import find_opportunities
ops = find_opportunities(100000, n=5)
print("8. Opportunity Finder: OK (bullish: {}, bearish: {})".format(len(ops["bullish"]), len(ops["bearish"])))

# 9. Position monitor
from risk.position_monitor import get_all_position_statuses
positions = get_all_position_statuses()
print("9. Position Monitor: OK ({} open positions)".format(len(positions)))

# 10. Sentiment
from storage.db import get_connection
conn = get_connection()
sentiment_count = conn.execute("SELECT COUNT(*) as c FROM sentiment").fetchone()["c"]
news_count = conn.execute("SELECT COUNT(*) as c FROM news").fetchone()["c"]
print("10. News/Sentiment: {} news, {} scored".format(news_count, sentiment_count))
conn.close()

# 11. Freshness
from core.freshness import feed_summary
summary = feed_summary(symbols[:10])
print("11. Freshness: {} (stale: {})".format(summary["status"], summary["stale_symbols"]))

print()
print("=== ALL COMPONENTS CONNECTED ===")