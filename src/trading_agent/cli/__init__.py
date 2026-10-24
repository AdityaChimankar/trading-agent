"""Main CLI entry point."""

import asyncio

import typer

from trading_agent.api.main import run as run_api
from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger, setup_logging
from trading_agent.ingest.fetch_news import fetch_latest_news
from trading_agent.ingest.kite_auto_login import ensure_valid_token
from trading_agent.ingest.watchlist_resolved import resolve_watchlist
from trading_agent.optimize.cli import app as optimize_app
from trading_agent.research.backtest import run_backtest
from trading_agent.research.diagnose_edge import diagnose_edge
from trading_agent.storage.parquet_store import get_parquet_store

app = typer.Typer(
    name="trading-agent",
    help="NSE Intraday Trading Signal Agent",
    add_completion=False,
)

app.add_typer(optimize_app, name="optimize")


def _resolve_symbols(symbols: list[str] | None) -> list[str]:
    """Resolve explicit symbols or the configured watchlist."""
    return asyncio.run(resolve_watchlist(symbols=symbols))


@app.callback()
def main(
    debug: bool = typer.Option(False, "--debug", "-d", help="Enable debug logging"),
):
    """Trading Agent - NSE Intraday Trading Signal System."""
    settings = get_settings()
    settings.debug = debug
    setup_logging()


@app.command()
def ingest(
    symbols: list[str] = typer.Argument(None, help="Symbols to fetch (default: watchlist)"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    days: int = typer.Option(100, help="Days of history to fetch"),
    parallel: bool = typer.Option(True, help="Fetch in parallel"),
):
    """Fetch historical data."""
    if not symbols:
        symbols = _resolve_symbols(symbols)

    from trading_agent.ingest.fetch_historical import HistoricalFetcher
    fetcher = HistoricalFetcher()

    typer.echo(f"Fetching {interval} data for {len(symbols)} symbols ({days} days)...")
    if len(symbols) == 1:
        results = {symbols[0]: fetcher.fetch_symbol(symbols[0], interval, days)}
    else:
        results = fetcher.fetch_watchlist(symbols, interval, days, parallel=parallel)

    for symbol, df in results.items():
        typer.echo(f"  {symbol}: {len(df)} candles")


@app.command()
def backfill(
    symbols: list[str] = typer.Argument(None, help="Symbols to backfill (default: watchlist)"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    years: int = typer.Option(2, help="Years of history"),
    days: int = typer.Option(None, help="Days of history (overrides --years)"),
    missing_only: bool = typer.Option(False, help="Fetch only the head/tail outside stored candles"),
):
    """Backfill historical data."""
    if not symbols:
        symbols = _resolve_symbols(symbols)

    from trading_agent.ingest.fetch_historical import HistoricalFetcher

    fetcher = HistoricalFetcher()

    span = f"{days} days" if days is not None else f"{years} years"
    typer.echo(f"Backfilling {span} of {interval} data for {len(symbols)} symbols...")
    results = fetcher.backfill(symbols, interval, years, days=days, missing_only=missing_only)

    for symbol, count in results.items():
        typer.echo(f"  {symbol}: {count} candles")


@app.command()
def login():
    """Authenticate with Kite Connect."""
    typer.echo("Authenticating with Kite Connect...")
    try:
        token = ensure_valid_token()
        typer.echo("Success! Access token saved.")
    except Exception as e:
        typer.echo(f"Failed: {e}")
        raise typer.Exit(1)


@app.command("heal-gaps")
def heal_gaps(
    symbols: list[str] = typer.Argument(None),
    interval: str = typer.Option("minute"),
    limit: int | None = typer.Option(None, min=1, help="Maximum symbols to check"),
):
    """Catch up trailing gaps; return a failing exit status if any symbol fails."""
    import json
    from trading_agent.ingest.gap_healer import GapHealer

    selected = list(dict.fromkeys(_resolve_symbols(symbols)))
    if limit is not None:
        selected = selected[:limit]
    healer = GapHealer(selected, interval)
    try:
        results = asyncio.run(healer.heal_to_current())
    except Exception as exc:
        typer.echo(f"Catch-up stopped: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"checked": len(results), "rows_written": sum(results.values()),
                           "errors": healer.errors, "skipped": healer.skipped}))
    if healer.errors:
        raise typer.Exit(1)


@app.command()
def backtest(
    symbols: list[str] = typer.Argument(..., help="Symbols to backtest"),
    start: str = typer.Option(..., help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., help="End date (YYYY-MM-DD)"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    capital: float = typer.Option(1000000, help="Initial capital"),
):
    """Run backtest."""
    from datetime import datetime

    start_date = datetime.fromisoformat(start)
    end_date = datetime.fromisoformat(end)

    typer.echo(f"Running backtest on {len(symbols)} symbols from {start} to {end}...")
    result = run_backtest(symbols, start_date, end_date, interval)

    typer.echo("\nResults:")
    typer.echo(f"  Total Trades: {result.total_trades}")
    typer.echo(f"  Win Rate: {result.win_rate:.1f}%")
    typer.echo(f"  Total PnL: {result.total_pnl:,.0f} ({result.total_pnl_pct:.2f}%)")
    typer.echo(f"  Max Drawdown: {result.max_drawdown_pct:.2f}%")
    typer.echo(f"  Sharpe Ratio: {result.sharpe_ratio:.2f}")
    typer.echo(f"  Profit Factor: {result.profit_factor:.2f}")


@app.command()
def diagnose(
    symbols: list[str] = typer.Argument(None, help="Symbols to diagnose (default: watchlist)"),
    interval: str = typer.Option("5minute", help="Candle interval"),
):
    """Diagnose signal edge."""
    if not symbols:
        symbols = _resolve_symbols(symbols)

    typer.echo(f"Diagnosing edge for {len(symbols)} symbols...")
    results = diagnose_edge(symbols, interval)

    from trading_agent.research.diagnose_edge import EdgeDiagnoser

    diagnoser = EdgeDiagnoser()
    diagnoser.print_summary(results)


@app.command()
def news(
    symbols: list[str] = typer.Argument(None, help="Symbols to filter (optional)"),
):
    """Fetch latest financial news."""
    typer.echo("Fetching latest news...")
    import asyncio

    articles = asyncio.run(fetch_latest_news(symbols=symbols))

    for article in articles[:20]:
        typer.echo(f"[{article.source}] {article.title}")
        typer.echo(f"  Symbols: {article.symbols}")
        typer.echo(
            f"  Sentiment: {article.sentiment:.2f}" if article.sentiment else "  Sentiment: N/A"
        )
        typer.echo()


@app.command()
def api(
    host: str = typer.Option("0.0.0.0", help="Host to bind"),
    port: int = typer.Option(8000, help="Port to bind"),
    reload: bool = typer.Option(False, help="Auto-reload"),
):
    """Run API server."""
    import os

    os.environ["API_HOST"] = host
    os.environ["API_PORT"] = str(port)
    os.environ["API_RELOAD"] = str(reload).lower()
    run_api()


@app.command()
def pipeline(
    symbols: list[str] = typer.Argument(None, help="Symbols to process (default: watchlist)"),
    interval: str = typer.Option("5minute", help="Candle interval"),
):
    """Run trading pipeline once."""
    if not symbols:
        symbols = _resolve_symbols(symbols)

    typer.echo(f"Running pipeline for {len(symbols)} symbols...")
    from trading_agent.analysis.opportunity_finder import OpportunityFinder
    from trading_agent.strategy.candidates import CandidateGenerator

    generator = CandidateGenerator()
    finder = OpportunityFinder()

    candidates = generator.generate_all(symbols, interval)
    opportunities = finder.find_opportunities(symbols, interval)

    typer.echo(f"Generated {len(candidates)} candidates, found {len(opportunities)} opportunities")
    for opp in opportunities[:10]:
        typer.echo(
            f"  #{opp.rank} {opp.symbol}: {opp.rule_signal}/{opp.ml_signal or 'N/A'} "
            f"(Score: {opp.score:.3f}, R:R {opp.risk_reward:.2f}) @ {opp.price:.2f}"
        )


@app.command()
def storage_stats():
    """Show storage statistics."""
    parquet = get_parquet_store()
    stats = parquet.get_stats()

    typer.echo(f"Symbols: {stats['symbols']}")
    typer.echo(f"Total Rows: {stats['total_rows']:,}")
    typer.echo(f"Total Size: {stats['total_size_mb']:.2f} MB")
    typer.echo("\nTop 10 by size:")
    for symbol, info in sorted(
        stats["by_symbol"].items(), key=lambda x: x[1]["size_mb"], reverse=True
    )[:10]:
        typer.echo(f"  {symbol}: {info['rows']:,} rows, {info['size_mb']:.2f} MB")


@app.command()
def compact(
    symbol: str = typer.Argument(..., help="Symbol to compact"),
    interval: str = typer.Option("5minute", help="Candle interval"),
):
    """Compact Parquet files for a symbol."""
    parquet = get_parquet_store()
    rows = parquet.compact(symbol, interval)
    typer.echo(f"Compacted {symbol} {interval}: {rows} rows")


if __name__ == "__main__":
    app()
