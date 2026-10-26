"""Symbol -> NSE industry and index membership, for sector breadth and screener filters.

NSE publishes every broad index as a CSV with an ``Industry`` column (NSE's 22
macro-sectors, e.g. "Capital Goods", "Healthcare"). Nifty Total Market plus
Nifty Microcap 250 covers roughly 1,000 listed names; other stored symbols stay
unclassified rather than being guessed. The map is cached under
``data/cache/sector_map.csv`` and refreshed explicitly (``trading-agent sector-map``),
never at import time or on a dashboard request.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import requests

from trading_agent.core.config import get_settings
from trading_agent.ingest.build_nifty500_symbols import HEADERS

INDEX_BASE_URL = "https://nsearchives.nseindia.com/content/indices/"
# Index id (used by the screener's index filter) -> NSE file name.
INDEX_FILES = {
    "NIFTY50": "ind_nifty50list.csv",
    "NIFTY100": "ind_nifty100list.csv",
    "NIFTY500": "ind_nifty500list.csv",
    "NIFTYMIDCAP150": "ind_niftymidcap150list.csv",
    "NIFTYSMALLCAP250": "ind_niftysmallcap250list.csv",
    "NIFTYMICROCAP250": "ind_niftymicrocap250_list.csv",
    "NIFTYTOTALMARKET": "ind_niftytotalmarket_list.csv",
}
COLUMNS = ["symbol", "company", "industry", "indices"]


class SectorMapError(RuntimeError):
    """NSE index lists could not be downloaded or parsed."""


def sector_map_path() -> Path:
    return get_settings().data_dir / "cache" / "sector_map.csv"


def parse_index_csv(text: str) -> list[dict]:
    """Rows of one NSE index CSV as {symbol, company, industry}."""
    reader = csv.DictReader(io.StringIO(text))
    fields = {name.strip().lower(): name for name in reader.fieldnames or []}
    if "symbol" not in fields or "industry" not in fields:
        raise SectorMapError("Index CSV has no Symbol/Industry columns")
    company = fields.get("company name")
    return [{"symbol": row[fields["symbol"]].strip().upper(),
             "company": row[company].strip() if company else "",
             "industry": row[fields["industry"]].strip()}
            for row in reader if row.get(fields["symbol"], "").strip()]


def build_sector_map(index_rows: dict[str, list[dict]]) -> dict[str, dict]:
    """Merge per-index rows into one entry per symbol with every index it belongs to."""
    merged: dict[str, dict] = {}
    for index_id, rows in index_rows.items():
        for row in rows:
            entry = merged.setdefault(row["symbol"], {**row, "indices": []})
            entry["industry"] = entry["industry"] or row["industry"]
            entry["indices"].append(index_id)
    return merged


def fetch_sector_map(timeout: float = 20) -> dict[str, dict]:
    """Download every index list in ``INDEX_FILES`` from NSE."""
    index_rows = {}
    try:
        with requests.Session() as session:
            session.headers.update(HEADERS)
            session.get("https://www.nseindia.com", timeout=timeout)  # sets the cookies NSE expects
            for index_id, file_name in INDEX_FILES.items():
                response = session.get(INDEX_BASE_URL + file_name, timeout=timeout)
                response.raise_for_status()
                index_rows[index_id] = parse_index_csv(response.content.decode("utf-8"))
    except requests.RequestException as error:
        raise SectorMapError(f"NSE index download failed: {type(error).__name__}") from error
    return build_sector_map(index_rows)


def save_sector_map(entries: dict[str, dict], path: Path | None = None) -> Path:
    path = path or sector_map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for symbol in sorted(entries):
            entry = entries[symbol]
            writer.writerow({"symbol": symbol, "company": entry.get("company", ""),
                             "industry": entry.get("industry", ""),
                             "indices": "|".join(sorted(entry.get("indices", [])))})
    temporary.replace(path)
    return path


def load_sector_map(path: Path | None = None) -> dict[str, dict]:
    """The cached map, or {} when it has not been fetched yet."""
    path = path or sector_map_path()
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["symbol"]: {"company": row["company"], "industry": row["industry"] or None,
                                "indices": [i for i in row["indices"].split("|") if i]}
                for row in csv.DictReader(handle)}
