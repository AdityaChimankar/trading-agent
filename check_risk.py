# Check portfolio risk block reason
from risk.portfolio_risk import calculate_portfolio_adjusted_position
from storage.db import get_watchlist_symbols

symbols = get_watchlist_symbols()
adj = calculate_portfolio_adjusted_position(symbols[0], "BUY", 100000)
print("Blocked:", adj.blocked)
print("Block reason:", adj.block_reason)
print("Total risk used:", adj.total_risk_used_pct)
print("Cluster exposure:", adj.cluster_exposure_pct)

# Check open positions
from risk.portfolio_risk import get_open_positions
ops = get_open_positions()
print("\nOpen positions:", len(ops))
for p in ops:
    print(" ", p["symbol"], p["action"], p["position_size"], "risk:", p["risk_amount"])