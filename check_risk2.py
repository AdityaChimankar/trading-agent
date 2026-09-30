# Check portfolio risk in detail
from risk.portfolio_risk import calculate_portfolio_adjusted_position, calculate_position
from storage.db import get_watchlist_symbols
from core.config import get_config

symbols = get_watchlist_symbols()
symbol = symbols[0]

# Check base position
base = calculate_position(symbol, "BUY", 100000)
print("Base position:")
if base:
    print("  symbol:", base.symbol)
    print("  action:", base.action)
    print("  entry_price:", base.entry_price)
    print("  atr:", base.atr)
    print("  stop_loss:", base.stop_loss)
    print("  take_profit:", base.take_profit)
    print("  stop_distance:", base.stop_distance)
    print("  risk_amount:", base.risk_amount)
    print("  position_size:", base.position_size)
    print("  position_value:", base.position_value)
    print("  capped:", base.capped_by_max_position)
    print("  volatility_pctile:", base.volatility_pctile)
    print("  risk_multiplier:", base.risk_multiplier)

# Check config
config = get_config()
print("\nRisk config:")
print("  risk_per_trade_pct:", config.risk.risk_per_trade_pct)
print("  atr_stop_multiplier:", config.risk.atr_stop_multiplier)
print("  total_risk_budget_pct:", config.risk.total_risk_budget_pct)
print("  max_position_pct_of_capital:", config.risk.max_position_pct_of_capital)

# Check adjusted
adj = calculate_portfolio_adjusted_position(symbol, "BUY", 100000)
print("\nAdjusted:")
print("  blocked:", adj.blocked)
print("  block_reason:", adj.block_reason)
print("  approved_size:", adj.approved_size)
print("  approved_value:", adj.approved_value)
print("  total_risk_used_pct:", adj.total_risk_used_pct)
print("  cluster_exposure_pct:", adj.cluster_exposure_pct)
print("  correlated_with:", adj.correlated_with)