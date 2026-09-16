"""
Downloads the current Nifty 500 constituent list directly from NSE and
writes symbols.txt - one symbol per line, ready for instrument_lookup.py.

NSE's site blocks plain scripted requests (no browser session) - this
mimics a browser with a warm-up request first, which is the standard
workaround. If NSE changes their URL structure, update NIFTY500_URL
below (search "NSE nifty500 list csv" to find the current one).

Run this on YOUR machine, not in a sandboxed environment - NSE's
domain needs to be reachable and unblocked.

Usage: python build_nifty500_symbols.py
"""
import csv
import io
import requests

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}


def fetch_nifty500_symbols() -> list:
    with requests.Session() as session:
        session.headers.update(HEADERS)
        session.get("https://www.nseindia.com", timeout=10)  # warm-up: sets cookies NSE expects
        response = session.get(NIFTY500_URL, timeout=15)
        response.raise_for_status()

    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8")))
    symbols = [row["Symbol"].strip() for row in reader if row.get("Symbol")]
    return symbols


if __name__ == "__main__":
    try:
        symbols = fetch_nifty500_symbols()
    except Exception as e:
        print(f"Download failed: {e}")
        print("NSE occasionally changes headers/URLs it accepts - if this keeps failing, "
              "download the CSV manually from nseindia.com's Nifty 500 index page instead "
              "and save the 'Symbol' column as symbols.txt, one per line.")
        raise SystemExit(1)

    with open("symbols.txt", "w") as f:
        f.write("\n".join(symbols))

    print(f"Wrote {len(symbols)} symbols to symbols.txt")
    print("Next: python instrument_lookup.py symbols.txt")
