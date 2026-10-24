"""Read-only paper account projection from the persisted position ledger.

This is a gross, entry-cost book, not a broker cash balance or mark-to-market
valuation. Execution fees and slippage are not recorded by manual positions.
"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from trading_agent.core.config import get_settings
from trading_agent.risk.wallet import PaperWallet, TradeRecord
from trading_agent.storage.db import Position, get_database


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def load_paper_book() -> PaperWallet:
    db = get_database()
    async with db.session() as session:
        rows = list((await session.execute(select(Position).order_by(Position.exit_time, Position.id))).scalars())
    book = PaperWallet(initial_cash=get_settings().paper.initial_capital, db=db)
    for row in rows:
        if row.status.lower() == "open":
            book.open_positions[row.symbol] = {"quantity": row.quantity, "entry_price": row.entry_price}
            book.invested += row.position_value
        elif row.status.lower() == "closed" and row.exit_time is not None:
            pnl = float(row.pnl or 0)
            book.trades.append(TradeRecord(
                symbol=row.symbol, action=row.action, entry_time=aware(row.entry_time),
                exit_time=aware(row.exit_time), entry_price=row.entry_price,
                exit_price=row.exit_price, quantity=row.quantity, gross_pnl=pnl,
                charges=0.0, net_pnl=pnl,
                holding_minutes=max(0, int((aware(row.exit_time) - aware(row.entry_time)).total_seconds() / 60)),
            ))
    book.total_pnl = sum(trade.net_pnl for trade in book.trades)
    market_zone = ZoneInfo("Asia/Kolkata")
    today = datetime.now(market_zone).date()
    book.daily_pnl = sum(trade.net_pnl for trade in book.trades if trade.exit_time.astimezone(market_zone).date() == today)
    book.cash = book.initial_cash + book.total_pnl - book.invested
    return book
