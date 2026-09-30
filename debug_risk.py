# Debug portfolio risk calculation
from risk.portfolio_risk import calculate_portfolio_adjusted_position, calculate_position, get_open_positions, compute_correlation_matrix, _correlated_symbols
from storage.db import get_watchlist_symbols
from core.config import get_config

symbols = get_watchlist_symbols()
symbol = symbols[0]
capital = 100000.0

config = get_config()
print("Config:", config.risk.total_risk_budget_pct, config.risk.max_position_pct_of_capital, config.risk.max_cluster_exposure_pct)

base_plan = calculate_position(symbol, "BUY", capital)
print("Base plan position_size:", base_plan.position_size)
print("Base plan risk_amount:", base_plan.risk_amount)

open_positions = get_open_positions()
print("Open positions:", len(open_positions))

total_risk_used = sum(p["risk_amount"] for p in open_positions)
total_risk_used_pct = (total_risk_used / capital) * 100 if capital > 0 else 0.0
remaining_budget_pct = config.risk.total_risk_budget_pct - total_risk_used_pct
print("Total risk used:", total_risk_used, "pct:", total_risk_used_pct)
print("Remaining budget:", remaining_budget_pct)

base_plan_risk_pct = (base_plan.risk_amount / capital) * 100 if capital > 0 else 0.0
print("Base plan risk pct:", base_plan_risk_pct)
size_scale = min(1.0, remaining_budget_pct / base_plan_risk_pct) if base_plan_risk_pct > 0 else 1.0
print("Size scale (budget):", size_scale)

# Same symbol
existing_same_symbol_value = sum(p["position_value"] for p in open_positions if p["symbol"] == symbol)
existing_same_symbol_pct = (existing_same_symbol_value / capital) * 100 if capital > 0 else 0.0
remaining_same_symbol_pct = config.risk.max_position_pct_of_capital - existing_same_symbol_pct
print("Same symbol pct:", existing_same_symbol_pct, "remaining:", remaining_same_symbol_pct)
max_value_by_same_symbol = capital * (remaining_same_symbol_pct / 100)
max_size_by_same_symbol = int(max_value_by_same_symbol / base_plan.entry_price) if base_plan.entry_price > 0 else 0
print("Max size by same symbol:", max_size_by_same_symbol)
same_symbol_scale = min(1.0, max_size_by_same_symbol / base_plan.position_size) if base_plan.position_size > 0 else 1.0
print("Same symbol scale:", same_symbol_scale)

size_scale = min(size_scale, same_symbol_scale)
print("Combined size_scale:", size_scale)

# Cluster
open_symbols = [p["symbol"] for p in open_positions]
if open_symbols:
    all_symbols = list(set(open_symbols + [symbol]))
    corr_matrix = compute_correlation_matrix(all_symbols)
    correlated_with = _correlated_symbols(symbol, open_symbols, corr_matrix)
    print("Correlated with:", correlated_with)
else:
    print("No open symbols for correlation")
    correlated_with = []

# Final
approved_size = int(base_plan.position_size * size_scale)
print("Final approved_size:", approved_size)