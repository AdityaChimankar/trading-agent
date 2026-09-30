# Debug portfolio risk calculation - using updated module
from risk.portfolio_risk import calculate_portfolio_adjusted_position, calculate_position
from storage.db import get_watchlist_symbols

symbols = get_watchlist_symbols()
symbol = symbols[0]
capital = 100000.0

# Test base position
base = calculate_position(symbol, "BUY", capital)
print("Base position:")
print("  position_size:", base.position_size if base else 0)
print("  risk_amount:", base.risk_amount if base else 0)

# Test adjusted
adj = calculate_portfolio_adjusted_position(symbol, "BUY", capital)
print("\nAdjusted:")
print("  blocked:", adj.blocked)
print("  block_reason:", adj.block_reason)
print("  approved_size:", adj.approved_size)
print("  approved_value:", adj.approved_value)
print("  total_risk_used_pct:", adj.total_risk_used_pct)