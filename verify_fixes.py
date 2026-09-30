# Verify the position monitoring fixes
print("=== FIX VERIFICATION ===\n")

# 1. available_capital from wallet
from risk.wallet import available_capital
from storage.db import get_connection
conn = get_connection()
ac = available_capital(conn)
print("1. available_capital (wallet):", ac)
conn.close()

# 2. Open a paper position in a symbol, then check re-sizing is blocked
from storage.db import get_connection
from risk.portfolio_risk import calculate_portfolio_adjusted_position, get_open_positions
from analysis.opportunity_finder import find_opportunities
from storage.db import get_watchlist_symbols

symbols = get_watchlist_symbols()

# Check current open positions before
open_before = get_open_positions()
print("\n2. Open positions before test:", len(open_before), [p["symbol"] for p in open_before])

# Pick a symbol that is NOT already open so the test opens a clean position
held_before = set(p["symbol"] for p in open_before)
candidates = [s for s in symbols if s not in held_before]
test_symbol = "RELIANCE" if "RELIANCE" in candidates else (candidates[0] if candidates else None)
if not test_symbol:
    print("   No symbol free to test (all have open positions) - skipping position test")

# Open a paper position in test_symbol via API path
from api.routers.positions import OpenPositionRequest, open_position
try:
    result = open_position(OpenPositionRequest(symbol=test_symbol, action="BUY", capital=ac))
    print("   Opened position:", result)
except Exception as e:
    print("   Could not open position:", getattr(e, "detail", e))

# Now try to size a NEW position in the same symbol -> should be blocked
adj = calculate_portfolio_adjusted_position(test_symbol, "BUY", ac)
print("   Re-sizing same symbol BUY -> blocked:", adj.blocked)
print("   Reason:", adj.block_reason)

adj2 = calculate_portfolio_adjusted_position(test_symbol, "SELL", ac)
print("   Re-sizing same symbol SELL -> blocked:", adj2.blocked)
print("   Reason:", adj2.block_reason)

# 3. find_opportunities should exclude held symbols
ops = find_opportunities(ac, n=5)
all_syms = [o.symbol for o in ops["bullish"] + ops["bearish"]]
print("\n3. find_opportunities with wallet capital")
print("   Bullish:", [o.symbol for o in ops["bullish"]])
print("   Bearish:", [o.symbol for o in ops["bearish"]])
held = set(p["symbol"] for p in get_open_positions())
overlap = [s for s in all_syms if s in held]
print("   Held symbols appearing in opportunities:", overlap, "(should be empty)")

# Verify ranking is by technical score
for direction in ("bullish", "bearish"):
    scores = [o.technical_score for o in ops[direction]]
    print(f"   {direction} technical scores (desc):", scores, "- sorted desc:", scores == sorted(scores, reverse=True))

# 4. Clean up: close the test position we opened
from api.routers.wallet import close_position_wallet
open_positions = get_open_positions()
for p in open_positions:
    if p["symbol"] == test_symbol:
        print(f"\n4. Closing test position {test_symbol} (id={p['id']})...")
        close_position_wallet(p["id"])

open_after = get_open_positions()
print("   Open positions after cleanup:", [p["symbol"] for p in open_after])

print("\n=== VERIFICATION COMPLETE ===")