"""
Downloads NSE equity symbols from multiple sources:
- Full NSE equity list (EQUITY_L.csv) - 2300+ symbols
- Nifty 500, Nifty 100, Nifty 50 indices
- Nifty Midcap 150, Smallcap 250

Writes symbols.txt - one symbol per line, ready for instrument_lookup.py.

Usage:
  python -m ingest.build_nifty500_symbols           # Default: Nifty 500
  python -m ingest.build_nifty500_symbols --full    # All NSE EQ series (2300+)
  python -m ingest.build_nifty500_symbols --combined # Nifty 500 + Midcap 150 + Smallcap 250
  python -m ingest.build_nifty500_symbols --index NIFTY100
"""
import csv
import io
import sys
import requests

from paths import SYMBOLS_TXT

# Available index lists from NSE
INDEX_URLS = {
    "NIFTY50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "NIFTYMIDCAP150": "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "NIFTYSMALLCAP250": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

FULL_NSE_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}


def fetch_index_symbols(index_name: str) -> list:
    """Fetch symbols from a specific NSE index CSV."""
    url = INDEX_URLS.get(index_name)
    if not url:
        raise ValueError(f"Unknown index: {index_name}. Available: {list(INDEX_URLS.keys())}")
    
    with requests.Session() as session:
        session.headers.update(HEADERS)
        session.get("https://www.nseindia.com", timeout=10)
        response = session.get(url, timeout=15)
        response.raise_for_status()
    
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8")))
    # Column name varies: "Symbol" or "SYMBOL"
    col = "Symbol" if "Symbol" in reader.fieldnames else "SYMBOL"
    symbols = [row[col].strip() for row in reader if row.get(col)]
    return symbols


def fetch_full_nse_eq() -> list:
    """Fetch all NSE EQ series symbols from EQUITY_L.csv (2300+ symbols)."""
    with requests.Session() as session:
        session.headers.update(HEADERS)
        response = session.get(FULL_NSE_URL, timeout=30)
        response.raise_for_status()
    
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8")))
    # Column is ' SYMBOL' (with leading space), series is ' SERIES'
    sym_col = 'SYMBOL' if 'SYMBOL' in reader.fieldnames else ' SYMBOL'
    ser_col = 'SERIES' if 'SERIES' in reader.fieldnames else ' SERIES'
    symbols = [row[sym_col].strip() for row in reader if row.get(ser_col, '').strip() == 'EQ']
    return symbols


def fetch_combined() -> list:
    """Fetch Nifty 500 + Midcap 150 + Smallcap 250 (deduplicated)."""
    nifty500 = set(fetch_index_symbols("NIFTY500"))
    midcap150 = set(fetch_index_symbols("NIFTYMIDCAP150"))
    smallcap250 = set(fetch_index_symbols("NIFTYSMALLCAP250"))
    
    combined = nifty500 | midcap150 | smallcap250
    return sorted(combined)


def fetch_symbols(mode: str = "NIFTY500", index: str = None) -> list:
    """Fetch symbols based on mode."""
    if mode == "full":
        return fetch_full_nse_eq()
    elif mode == "combined":
        return fetch_combined()
    elif mode == "index" and index:
        return fetch_index_symbols(index)
    else:
        return fetch_index_symbols("NIFTY500")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Fetch NSE equity symbols")
    parser.add_argument("--full", action="store_true", help="All NSE EQ series (~2300 symbols)")
    parser.add_argument("--combined", action="store_true", help="Nifty 500 + Midcap 150 + Smallcap 250")
    parser.add_argument("--index", choices=list(INDEX_URLS.keys()), help="Specific index to fetch")
    args = parser.parse_args()
    
    if args.full:
        mode = "full"
    elif args.combined:
        mode = "combined"
    elif args.index:
        mode = "index"
    else:
        mode = "NIFTY500"
    
    try:
        symbols = fetch_symbols(mode, args.index)
    except Exception as e:
        print(f"Download failed: {e}")
        print("NSE occasionally changes headers/URLs - download manually if needed.")
        raise SystemExit(1)
    
    with open(SYMBOLS_TXT, "w") as f:
        f.write("\n".join(symbols))
    
    print(f"Wrote {len(symbols)} symbols to {SYMBOLS_TXT.name}")
    print("Next: python -m ingest.instrument_lookup symbols.txt")