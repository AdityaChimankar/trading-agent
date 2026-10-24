"""Optimization CLI and utilities."""

from __future__ import annotations

import typer

from trading_agent.research.walk_forward_optimizer import run_walk_forward, run_walk_forward_multi
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.train_ml_model import train_model
from trading_agent.strategy.train_ml_model_v2 import train_enhanced_model

app = typer.Typer(name="optimize", help="Optimization commands")


@app.command()
def walk_forward(
    symbols: list[str] = typer.Argument(None, help="Symbols to optimize (default: all with data)"),
    parallel: bool = typer.Option(True, help="Run in parallel"),
):
    """Run walk-forward optimization."""
    if not symbols:
        parquet = get_parquet_store()
        symbols = parquet.get_symbols()

    if not symbols:
        typer.echo("No symbols with data found")
        return

    run_walk_forward_multi(symbols, parallel=parallel)


@app.command()
def walk_forward_single(
    symbol: str = typer.Argument(..., help="Symbol to optimize"),
):
    """Run walk-forward optimization for a single symbol."""
    run_walk_forward(symbol)


@app.command()
def ml_model(
    symbols: list[str] = typer.Argument(..., help="Symbols to train"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    lookback_days: int = typer.Option(60, help="Lookback days"),
    target_type: str = typer.Option("direction", help="Target type"),
):
    """Train ML models for symbols."""
    result = train_model(symbols, interval, lookback_days)
    typer.echo(f"Training completed: {result}")


@app.command()
def ml_model_v2(
    symbols: list[str] = typer.Argument(..., help="Symbols to train"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    lookback_days: int = typer.Option(60, help="Lookback days"),
    target_type: str = typer.Option("cost_aware", help="Target type"),
):
    """Train enhanced ML models (v2) for symbols."""
    results = train_enhanced_model(symbols, interval, lookback_days, target_type)
    for symbol, result in results.items():
        if "error" in result:
            typer.echo(f"{symbol}: ERROR - {result['error']}")
        else:
            metrics = result.get("metrics", {})
            typer.echo(
                f"{symbol}: AUC={metrics.get('test_auc', 0):.3f}, "
                f"Acc={metrics.get('test_accuracy', 0):.3f}, "
                f"Threshold={metrics.get('optimal_threshold', 0):.2f}"
            )


@app.command()
def strategy_lab(
    symbol: str = typer.Argument(..., help="Symbol to test"),
    interval: str = typer.Option("5minute", help="Candle interval"),
    max_bars: int = typer.Option(5000, min=250, max=10000, help="Maximum completed local candles"),
):
    """Compare fixed profiles and unconditional baselines; exploratory only."""
    from trading_agent.research.strategy_lab import INDEX_KEY, MAX_BARS, compare_profiles, read_completed

    symbol = symbol.strip().upper()
    try:
        result = compare_profiles(read_completed(symbol, interval, max_bars), symbol, interval,
                                  index_frame=read_completed(INDEX_KEY, interval, MAX_BARS))
    except ValueError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo("Exploratory comparison; no profile is qualified or promoted.")
    if result["status"] != "completed":
        typer.echo(f"Insufficient history: {result['candles']} / {result['required_candles']} candles", err=True)
        raise typer.Exit(code=1)
    for profile in result["profiles"] + result["baselines"]:
        metrics = profile["metrics"]
        mean = metrics["mean_net_return_pct"]
        mean_label = "n/a" if mean is None else f"{mean:.4f}%"
        typer.echo(f"{profile['label']}: {metrics['trades']} trades | Mean net return: {mean_label}")
    for skipped in result["skipped_profiles"]:
        typer.echo(f"Skipped {skipped['id']}: {skipped['reason']}")
    typer.echo(result["limitations"])


@app.command()
def strategy_search(
    symbols: list[str] = typer.Argument(None, help="Symbols (default: 30 liquid NSE large caps)"),
    no_ml: bool = typer.Option(False, "--no-ml", help="Skip the gradient-boosting candidates"),
):
    """Frozen-selection strategy search with baselines and walk-forward; research only."""
    from trading_agent.research.strategy_search import main

    main([*(symbols or []), *(["--no-ml"] if no_ml else [])])


@app.command()
def cross_sectional(
    symbols: list[str] = typer.Argument(None, help="Symbols (default: symbols_nifty500_plus.txt)"),
    interval: str = typer.Option("5minute", help="Stored candle interval used to build daily bars"),
    max_lag: int = typer.Option(10, help="Longest lag in sessions (paper: 240)"),
    top_k: int = typer.Option(10, help="Stocks bought and shorted each session"),
    cost_bps: float = typer.Option(15.0, help="Round-trip cost per position, incl. slippage"),
    min_value_cr: float = typer.Option(10.0, help="Minimum 20-day mean traded value, INR crore"),
):
    """Cross-sectional ranking walk-forward against random picks; research only."""
    from trading_agent.research.cross_sectional import main

    main([*(symbols or []), "--interval", interval, "--max-lag", str(max_lag), "--top-k", str(top_k),
          "--cost-bps", str(cost_bps), "--min-value-cr", str(min_value_cr)])



@app.command("ranking-lab")
def ranking_lab(
    symbols: list[str] = typer.Argument(None, help="Symbols (default: symbols_nifty500_plus.txt)"),
    snapshots: str = typer.Option(None, help="Parquet cache for session snapshots (read if present)"),
):
    """Pre-registered cross-sectional variants with frozen validation selection; research only."""
    from trading_agent.research.ranking_lab import main

    main([*(symbols or []), *(["--snapshots", snapshots] if snapshots else [])])

@app.command("ranker-trial")
def ranker_trial(
    action: str = typer.Argument(..., help="start | pick | settle | status"),
    start_session: str = typer.Option(None, help="start: first session, YYYY-MM-DD (default: next one)"),
    session: str = typer.Option(None, help="settle: one session instead of every due one"),
    no_refresh: bool = typer.Option(False, help="Use stored candles; do not fetch from Kite"),
):
    """Prospective paper trial of the frozen cross-sectional ranker; research only."""
    from trading_agent.research.ranker_trial import main

    main([action, *(["--start-session", start_session] if start_session else []),
          *(["--session", session] if session else []), *(["--no-refresh"] if no_refresh else [])])

if __name__ == "__main__":
    app()
