"""Daily digest and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from trading_agent.analysis.opportunity_finder import OpportunityFinder
from trading_agent.analysis.sentiment import get_sentiment_analyzer
from trading_agent.analysis.technical_summary import TechnicalAnalyzer
from trading_agent.core.logging_config import get_logger
from trading_agent.storage.db import get_database
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.candidates import CandidateGenerator

logger = get_logger(__name__)


@dataclass(frozen=True)
class DailyDigest:
    """Daily trading digest."""

    date: datetime
    market_summary: dict
    top_opportunities: list[dict]
    portfolio_summary: dict
    risk_metrics: dict
    news_sentiment: dict
    technical_highlights: list[dict]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class DigestGenerator:
    """Generate daily trading digests."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        interval: str = "5minute",
        parquet_store=None,
        db=None,
    ):
        self.symbols = symbols or []
        self.interval = interval
        self.parquet = parquet_store or get_parquet_store()
        self.db = db or get_database()

        self.opportunity_finder = OpportunityFinder()
        self.technical_analyzer = TechnicalAnalyzer(self.parquet)
        self.sentiment_analyzer = get_sentiment_analyzer()
        self.candidate_generator = CandidateGenerator()

    def generate(self, date: datetime | None = None) -> DailyDigest:
        """Generate digest for a specific date."""
        target_date = date or datetime.now(UTC).date()
        start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=UTC)
        end = start + timedelta(days=1)

        digest = DailyDigest(
            date=start,
            market_summary=self._market_summary(start, end),
            top_opportunities=self._top_opportunities(),
            portfolio_summary=self._portfolio_summary(),
            risk_metrics=self._risk_metrics(),
            news_sentiment=self._news_sentiment(),
            technical_highlights=self._technical_highlights(),
        )

        return digest

    def _market_summary(self, start: datetime, end: datetime) -> dict:
        """Generate market summary."""
        # Get Nifty 50 symbols as proxy
        nifty_symbols = self.symbols[:50] if len(self.symbols) >= 50 else self.symbols

        advances = 0
        declines = 0
        unchanged = 0
        total_volume = 0

        for symbol in nifty_symbols:
            df = self.parquet.read_candles(symbol, self.interval, start=start, end=end)
            if df.empty or len(df) < 2:
                continue

            first_open = df.iloc[0]["open"]
            last_close = df.iloc[-1]["close"]
            change = (last_close - first_open) / first_open

            if change > 0.001:
                advances += 1
            elif change < -0.001:
                declines += 1
            else:
                unchanged += 1

            total_volume += df["volume"].sum()

        return {
            "date": start.date().isoformat(),
            "advances": advances,
            "declines": declines,
            "unchanged": unchanged,
            "advance_decline_ratio": advances / declines if declines > 0 else float("inf"),
            "total_volume": int(total_volume),
            "market_breadth": "positive"
            if advances > declines
            else "negative"
            if declines > advances
            else "neutral",
        }

    def _top_opportunities(self) -> list[dict]:
        """Get top opportunities."""
        opportunities = self.opportunity_finder.find_opportunities(
            self.symbols,
            self.interval,
        )

        return [
            {
                "symbol": opp.symbol,
                "rank": opp.rank,
                "score": round(opp.score, 3),
                "rule_signal": opp.rule_signal,
                "ml_signal": opp.ml_signal,
                "price": opp.price,
                "risk_reward": round(opp.risk_reward, 2),
            }
            for opp in opportunities[:10]
        ]

    def _portfolio_summary(self) -> dict:
        """Get portfolio summary from wallet."""
        # This would integrate with PaperWallet
        return {
            "total_value": 0.0,
            "cash": 0.0,
            "invested": 0.0,
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "open_positions": 0,
        }

    def _risk_metrics(self) -> dict:
        """Get risk metrics."""
        return {
            "portfolio_risk_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "var_95": 0.0,
            "positions_at_risk": 0,
        }

    def _news_sentiment(self) -> dict:
        """Get news sentiment summary."""
        # This would fetch and analyze news
        return {
            "overall": "neutral",
            "bullish_count": 0,
            "bearish_count": 0,
            "top_bullish": [],
            "top_bearish": [],
        }

    def _technical_highlights(self) -> list[dict]:
        """Get technical highlights for top symbols."""
        highlights = []

        for symbol in self.symbols[:20]:  # Top 20 symbols
            summary = self.technical_analyzer.analyze(symbol, self.interval)
            if summary and summary.overall_signal != "hold":
                highlights.append(
                    {
                        "symbol": symbol,
                        "signal": summary.overall_signal,
                        "confidence": round(summary.confidence, 2),
                        "trend": summary.trend,
                        "rsi": round(summary.rsi, 1),
                        "price": summary.current_price,
                    }
                )

        return highlights[:10]

    def format_digest(self, digest: DailyDigest) -> str:
        """Format digest as human-readable text."""
        lines = [
            "═══ DAILY TRADING DIGEST ═══",
            f"Date: {digest.date.strftime('%Y-%m-%d')}",
            f"Generated: {digest.generated_at.strftime('%H:%M:%S UTC')}",
            "",
            "📊 MARKET SUMMARY",
            f"  Advances: {digest.market_summary['advances']} | Declines: {digest.market_summary['declines']} | Unchanged: {digest.market_summary['unchanged']}",
            f"  A/D Ratio: {digest.market_summary['advance_decline_ratio']:.2f} | Breadth: {digest.market_summary['market_breadth']}",
            f"  Total Volume: {digest.market_summary['total_volume']:,}",
            "",
            "🎯 TOP OPPORTUNITIES",
        ]

        for opp in digest.top_opportunities:
            lines.append(
                f"  #{opp['rank']} {opp['symbol']}: {opp['rule_signal']}/{opp['ml_signal'] or 'N/A'} "
                f"(Score: {opp['score']}, R:R {opp['risk_reward']}) @ {opp['price']:.2f}"
            )

        if digest.technical_highlights:
            lines.append("")
            lines.append("📈 TECHNICAL HIGHLIGHTS")
            for h in digest.technical_highlights:
                lines.append(
                    f"  {h['symbol']}: {h['signal'].upper()} ({h['confidence']}) | "
                    f"Trend: {h['trend']} | RSI: {h['rsi']} @ {h['price']:.2f}"
                )

        lines.append("")
        lines.append("═══ END OF DIGEST ═══")

        return "\n".join(lines)


def generate_morning_briefing(
    symbols: list[str],
    interval: str = "5minute",
) -> str:
    """Generate morning pre-market briefing."""
    parquet = get_parquet_store()
    TechnicalAnalyzer(parquet)

    lines = [
        "═══ MORNING BRIEFING ═══",
        f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}",
        "",
        "📋 PRE-MARKET SETUP",
    ]

    # Check overnight gaps
    for symbol in symbols[:30]:
        df = parquet.read_latest(symbol, interval, n=2)
        if len(df) >= 2:
            prev_close = df.iloc[-2]["close"]
            curr_open = df.iloc[-1]["open"]
            gap = (curr_open - prev_close) / prev_close * 100

            if abs(gap) > 1.0:
                direction = "GAP UP" if gap > 0 else "GAP DOWN"
                lines.append(f"  {symbol}: {direction} {gap:+.2f}% (Open: {curr_open:.2f})")

    return "\n".join(lines)
