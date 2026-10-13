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
):
    """Run strategy lab grid search."""
    from trading_agent.research.strategy_lab import run_strategy_lab

    results = run_strategy_lab(symbol, interval)
    for r in results[:10]:
        typer.echo(
            f"Params: {r.params} | Net EV: {r.net_ev:.4f}% | WR: {r.win_rate:.1f}% | "
            f"DD: {r.max_drawdown:.2f}% | Sharpe: {r.sharpe:.2f}"
        )


if __name__ == "__main__":
    app()
