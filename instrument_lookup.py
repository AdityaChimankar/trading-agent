"""
Resolves instrument tokens for a whole list of symbols at once, so you
don't manually grep instruments.csv for 500 stocks one at a time.

Usage:
  python instrument_lookup.py symbols.txt
  (symbols.txt: one NSE symbol per line, e.g. RELIANCE / TCS / INFY)
"""
import sys
import csv
import urllib.request
from pathlib import Path

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
INSTRUMENTS_CSV = Path(__file__).parent / "instruments.csv"


def download_instruments(force: bool = False) -> Path:
    if INSTRUMENTS_CSV.exists() and not force:
        return INSTRUMENTS_CSV
    print("Downloading instrument dump (~10-20MB, all exchanges)...")
    urllib.request.urlretrieve(INSTRUMENTS_URL, INSTRUMENTS_CSV)
    return INSTRUMENTS_CSV


def build_nse_equity_lookup() -> dict:
    """Returns {symbol: instrument_token} for NSE equity (EQ) only."""
    path = download_instruments()
    lookup = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["exchange"] == "NSE" and row["segment"] == "NSE" and row["instrument_type"] == "EQ":
                lookup[row["tradingsymbol"]] = int(row["instrument_token"])
    return lookup


def resolve_watchlist(symbols: list) -> tuple:
    """Returns (resolved_list, missing_list). resolved_list items match
    the WATCHLIST dict shape used elsewhere in the project."""
    lookup = build_nse_equity_lookup()
    resolved, missing = [], []
    for symbol in symbols:
        token = lookup.get(symbol.strip().upper())
        if token:
            resolved.append({"symbol": symbol.strip().upper(), "instrument_token": token, "exchange": "NSE"})
        else:
            missing.append(symbol.strip())
    return resolved, missing


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python instrument_lookup.py symbols.txt")
        sys.exit(1)

    with open(sys.argv[1]) as f:
        symbols = [line.strip() for line in f if line.strip()]

    resolved, missing = resolve_watchlist(symbols)
    print(f"Resolved {len(resolved)}/{len(symbols)} symbols")
    if missing:
        print(f"Could not find: {missing}")

    # Write out as a Python-importable watchlist for fetch_historical.py
    with open("watchlist_resolved.py", "w") as f:
        f.write("WATCHLIST = " + repr(resolved))
    print("Saved to watchlist_resolved.py - import WATCHLIST from this in fetch_historical.py")
