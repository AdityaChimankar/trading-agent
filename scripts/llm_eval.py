"""
LLM Value Measurement - Backtest LLM decisions vs rule-only on historical data.

This script evaluates whether the LLM decision agent adds value over the
rule-based agent by:
1. Running both agents on identical historical inputs
2. Measuring agreement rate, EV difference, cost per decision
3. Determining if LLM path should be killed or kept

Usage:
    python -m scripts.llm_eval --symbols RELIANCE TCS INFY
    python -m scripts.llm_eval --all
"""
import sys
import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

from core.quant_indicators import load_candles, compute_macd
from core.pattern_detection import detect_patterns
from core.signals import prepare_symbol_series, precompute_pattern_bias_codes
from strategy.decision_agent import evaluate_signals as rule_evaluate_signals
from strategy.llm_decision_agent import llm_decide, fetch_recent_headlines, PROMPT_TEMPLATE
from storage.db import get_watchlist_symbols, get_connection

# Thresholds for LLM kill switch
MIN_AGREEMENT_RATE = 0.65      # LLM must agree with rules at least 65% of the time
MAX_EV_DEGRADATION = -0.005    # LLM EV must not be worse than rules by more than 0.5%
MIN_TRADES_FOR_EVAL = 20       # Minimum trades for statistical significance


def get_historical_llm_decisions(symbol: str, limit: int = 500) -> list:
    """Get stored LLM decisions from the database for backtesting."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT action, confidence, rationale, rule_based_action, timestamp
               FROM llm_signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?""",
            (symbol, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def evaluate_llm_vs_rules(symbols: list = None, min_trades: int = MIN_TRADES_FOR_EVAL) -> dict:
    """Compare LLM decisions against rule-based decisions on historical data."""
    if symbols is None:
        symbols = get_watchlist_symbols()[:30]  # Limit for speed
    
    all_results = []
    
    for symbol in symbols:
        # Get LLM decisions from DB
        llm_decisions = get_historical_llm_decisions(symbol, limit=200)
        
        if len(llm_decisions) < min_trades:
            print(f"  {symbol}: SKIP - only {len(llm_decisions)} LLM decisions (need {min_trades})")
            continue
        
        # Also get rule decisions for same timestamps
        # For a fair comparison, we need to re-run rule logic on historical candles
        # at the same timestamps
        prepared = prepare_symbol_series(symbol, candle_limit=5000)
        if prepared is None:
            print(f"  {symbol}: SKIP - insufficient historical data")
            continue
        
        # Build rule decisions for each LLM decision timestamp
        rule_matches = 0
        total_compared = 0
        llm_trades = 0
        rule_trades = 0
        
        for llm_dec in llm_decisions:
            llm_action = llm_dec["action"]
            rule_action = llm_dec["rule_based_action"]
            
            if llm_action in ("BUY", "SELL"):
                llm_trades += 1
            if rule_action in ("BUY", "SELL"):
                rule_trades += 1
            
            if llm_action == rule_action and llm_action != "HOLD":
                rule_matches += 1
            total_compared += 1
        
        agreement_rate = rule_matches / total_compared if total_compared > 0 else 0
        
        # Simulate P&L for both (simplified - using fixed exit logic)
        from core.signals import simulate_trades, DEFAULT_PARAMS
        
        # Get rule-based trades
        rule_trades_list = simulate_trades(prepared, params=DEFAULT_PARAMS)
        
        # For LLM, we'd need to reconstruct the signals from stored decisions
        # This is complex - for now we compare agreement rates
        
        result = {
            "symbol": symbol,
            "llm_decisions": len(llm_decisions),
            "agreement_rate": agreement_rate,
            "llm_trades": llm_trades,
            "rule_trades": rule_trades,
            "kill_recommendation": "KILL" if agreement_rate < MIN_AGREEMENT_RATE else "KEEP",
        }
        
        all_results.append(result)
        print(f"  {symbol}: agreement={agreement_rate:.1%}, llm_trades={llm_trades}, rule_trades={rule_trades} -> {result['kill_recommendation']}")
    
    # Overall summary
    if all_results:
        avg_agreement = np.mean([r["agreement_rate"] for r in all_results])
        kill_count = sum(1 for r in all_results if r["kill_recommendation"] == "KILL")
        keep_count = sum(1 for r in all_results if r["kill_recommendation"] == "KEEP")
        
        print(f"\n=== LLM Evaluation Summary ===")
        print(f"Symbols evaluated: {len(all_results)}")
        print(f"Average agreement rate: {avg_agreement:.1%}")
        print(f"Kill recommendation: {kill_count} kill, {keep_count} keep")
        print(f"Threshold: agreement >= {MIN_AGREEMENT_RATE:.0%}")
        
        return {
            "symbols": all_results,
            "avg_agreement_rate": avg_agreement,
            "kill_count": kill_count,
            "keep_count": keep_count,
            "threshold": MIN_AGREEMENT_RATE,
            "recommendation": "KILL_LLM" if avg_agreement < MIN_AGREEMENT_RATE else "KEEP_LLM",
        }
    
    return {"error": "No symbols with sufficient data"}


def estimate_llm_cost_per_decision() -> dict:
    """Estimate the cost of running LLM decisions."""
    from core.llm_client import get_model
    
    model = get_model()
    
    # Rough cost estimates (OpenRouter pricing varies)
    cost_estimates = {
        "openrouter/free": {"input_per_1k": 0.0, "output_per_1k": 0.0},
        "openrouter/auto": {"input_per_1k": 0.0001, "output_per_1k": 0.0003},
        "anthropic/claude-3-haiku": {"input_per_1k": 0.00025, "output_per_1k": 0.00125},
        "anthropic/claude-3-sonnet": {"input_per_1k": 0.003, "output_per_1k": 0.015},
        "openai/gpt-3.5-turbo": {"input_per_1k": 0.0005, "output_per_1k": 0.0015},
        "openai/gpt-4": {"input_per_1k": 0.03, "output_per_1k": 0.06},
    }
    
    model_key = model.lower().replace(":", "/")
    pricing = cost_estimates.get(model_key, {"input_per_1k": 0.001, "output_per_1k": 0.003})
    
    # Estimate tokens per call
    prompt_tokens = 800  # ~800 tokens for our prompt template
    output_tokens = 50   # ~50 tokens for JSON response
    
    cost_per_call = (prompt_tokens / 1000) * pricing["input_per_1k"] + (output_tokens / 1000) * pricing["output_per_1k"]
    
    # Daily cost (500 symbols * 4 calls/hour * 6.5 hours = 13,000 calls)
    daily_calls = 500 * 4 * 6.5  # 13,000
    daily_cost = daily_calls * cost_per_call
    monthly_cost = daily_cost * 22  # ~22 trading days
    
    return {
        "model": model,
        "cost_per_call_usd": cost_per_call,
        "daily_calls": daily_calls,
        "daily_cost_usd": daily_cost,
        "monthly_cost_usd": monthly_cost,
    }


def main():
    parser = argparse.ArgumentParser(description="LLM value measurement and kill switch")
    parser.add_argument("--symbols", type=str, help="Comma-separated list of symbols")
    parser.add_argument("--all", action="store_true", help="Evaluate all symbols with sufficient data")
    parser.add_argument("--cost", action="store_true", help="Show cost estimate only")
    parser.add_argument("--min-trades", type=int, default=MIN_TRADES_FOR_EVAL, help="Minimum trades for evaluation")
    
    args = parser.parse_args()
    
    if args.cost:
        cost_info = estimate_llm_cost_per_decision()
        print(f"LLM Cost Estimate:")
        print(f"  Model: {cost_info['model']}")
        print(f"  Cost per call: ${cost_info['cost_per_call_usd']:.6f}")
        print(f"  Daily calls (est): {cost_info['daily_calls']:,}")
        print(f"  Daily cost: ${cost_info['daily_cost_usd']:.2f}")
        print(f"  Monthly cost: ${cost_info['monthly_cost_usd']:.2f}")
        return
    
    symbols = args.symbols.split(",") if args.symbols else None
    if args.all:
        symbols = None  # Will use default list
    
    result = evaluate_llm_vs_rules(symbols, min_trades=args.min_trades)
    print(f"\nFinal recommendation: {result.get('recommendation', 'UNKNOWN')}")
    
    # Also show cost
    cost_info = estimate_llm_cost_per_decision()
    print(f"\nLLM Cost: ${cost_info['monthly_cost_usd']:.2f}/month (est.)")


if __name__ == "__main__":
    main()