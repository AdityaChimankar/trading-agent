"""Transaction cost model for NSE intraday (MIS) equity orders.

Rates follow Zerodha's published equity-intraday schedule. Statutory rates
change with budgets and exchange circulars, so they live in one dataclass:
check them against https://zerodha.com/charges before relying on net P&L.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntradayRates:
    brokerage_pct: float = 0.0003        # 0.03% of turnover ...
    brokerage_cap: float = 20.0          # ... capped at INR 20 per executed order
    stt_sell_pct: float = 0.00025        # 0.025% on the sell side only
    exchange_txn_pct: float = 0.0000297  # NSE transaction charge
    sebi_per_crore: float = 10.0         # INR 10 per crore of turnover
    stamp_buy_pct: float = 0.00003       # 0.003% on the buy side only
    gst_pct: float = 0.18                # on brokerage + exchange + SEBI


DEFAULT_RATES = IntradayRates()


def order_charges(side: str, price: float, quantity: int, rates: IntradayRates = DEFAULT_RATES) -> dict:
    """Itemised charges for one executed order."""
    turnover = price * quantity
    brokerage = min(rates.brokerage_cap, turnover * rates.brokerage_pct)
    stt = turnover * rates.stt_sell_pct if side == "SELL" else 0.0
    exchange = turnover * rates.exchange_txn_pct
    sebi = turnover * rates.sebi_per_crore / 1e7
    stamp = turnover * rates.stamp_buy_pct if side == "BUY" else 0.0
    gst = (brokerage + exchange + sebi) * rates.gst_pct
    items = {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
             "stamp": stamp, "gst": gst}
    items = {k: round(v, 4) for k, v in items.items()}
    return {**items, "total": round(sum(items.values()), 2), "turnover": round(turnover, 2)}


def round_trip_charges(action: str, entry: float, exit_: float, quantity: int,
                       rates: IntradayRates = DEFAULT_RATES) -> float:
    """Total charges to open with ``action`` and close with the opposite side."""
    closing = "SELL" if action == "BUY" else "BUY"
    return round(order_charges(action, entry, quantity, rates)["total"]
                 + order_charges(closing, exit_, quantity, rates)["total"], 2)


def breakeven_move_pct(price: float, quantity: int, rates: IntradayRates = DEFAULT_RATES) -> float:
    """Percent move a round trip needs just to pay its charges."""
    if price <= 0 or quantity <= 0:
        return float("inf")
    return round_trip_charges("BUY", price, price, quantity, rates) / (price * quantity) * 100


def slipped_price(side: str, price: float, slippage_bps: float) -> float:
    """Adverse fill: buys pay up, sells receive less."""
    factor = slippage_bps / 10_000
    return round(price * (1 + factor if side == "BUY" else 1 - factor), 2)


def rates_dict(rates: IntradayRates = DEFAULT_RATES) -> dict:
    return asdict(rates)
