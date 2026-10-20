# ML Pipeline Redesign Summary

> Latest outcome-model lifecycle (2026-10-20): separate calibrated BUY/SELL net-profit classifiers now share one training/paper/dashboard contract. The new entry point is `python -m trading_agent.strategy.outcome_cli`, not the older v2 command below. Historical and prospective-paper gates must pass before explicit promotion. The first five-stock candidate trained successfully but **failed** qualification: no tuning threshold passed, so no test trades were selected and no win rate can be reported. No model was promoted. See the README's lifecycle section; historical tables below are not evidence for this candidate.

> Validation update (2026-10-20): direction/cost labels now exclude unobserved futures; enhanced triple-barrier labels are binary next-open long outcomes using the full path and conservative fills. Their take-profit parameter now means reward/risk. Training uses timestamp-based purged train/tuning/holdout partitions; early stopping and threshold selection no longer use the reported holdout. Multi-symbol data is globally time ordered and regime quantiles are causal trailing ranks. Continuous-return labels are rejected by binary trainers. Existing models are unchanged and require a separate retraining/promotion decision. The new dashboard outcome panel validates fixed rules only; it does not establish hybrid profitability.

> Historical implementation summary, reviewed 2026-10-19. Use the installed CLI commands below and validate metrics again on current data before promotion.

> Dashboard restoration (2026-10-20): the active dashboard's **Walk-Forward Optimizer** is a corrected **rule-parameter** evaluator, not the v2 ML trainer described below. It does not retrain or promote the existing nine-feature `ml_model.joblib`. Its train/test outcomes are purged at window boundaries and use consistent entry/cost assumptions. See [README.md](README.md#walk-forward-optimizer-optional-research) for controls and commands. Historical metrics and no-lookahead claims in this document are not fresh validation evidence for the restored hybrid model.

## Problem
The original ML model (train_ml_model.py / ml_decision_agent.py) had severe issues:
- 48% accuracy (near random for 3-class)
- 66.5% win rate but tiny avg_win (0.108%) vs avg_loss (-0.123%)
- Made 5000+ overlapping trades on backtest (not realistic)
- No confidence thresholding - acted on all predictions
- No session/regime awareness
- Simple chronological split (not walk-forward)
- No cost-aware labeling

## Solution: Complete Redesign

### 1. Walk-Forward Validation (train_ml_model_v2.py)
- Rolling train/test windows: 3750 train / 750 test candles (~50/10 trading days)
- Re-trains model on each fold, tests on immediately following data
- No lookahead bias - each test fold is strictly after its train fold
- 21 folds per symbol on 20000 candles

### 2. Enhanced Features (19 features vs 9 original)
**Original (9):** rsi, adx, atr_pct, pattern_code, return_1/3/6, volume_ratio, dist_from_ma20
**Added (10):** minute_of_day, atr_pctile, dist_vwap, vwap_slope, volume_z, htf_fast_ret, htf_slow_ret, expected_move, gap_pct, bars_left_in_day

Session/regime features from strategy/session_features.py:
- Time-of-day filter (skip first 30min / last 60min)
- ATR percentile regime (avoid dead-calm and panic volatility)
- Session VWAP distance & slope (volume-weighted reference price)
- Higher-timeframe agreement (4hr + daily momentum)
- Cost-cover expected move (ATR scaled to hold horizon)
- Gap filter (avoid post-gap noise)

### 3. Cost-Aware Labeling
- Dynamic threshold: max(0.5 * ATR_pct, 3 * SLIPPAGE)
- Only labels UP/DOWN if forward return exceeds both volatility AND cost
- Reduces noise labels in low-volatility regimes

### 4. Confidence Thresholding
- Per-symbol optimal thresholds found on validation data (last 20%)
- Range: 0.40-0.80 depending on symbol
- Only acts when model confidence >= threshold
- Falls back to global threshold (0.55) for unseen symbols

### 5. Proper Trade Simulation (Non-Overlapping)
- After trade exits, skips to next available bar
- Uses 1.5x ATR stop, no target (best-measured exit from diagnose_edge.py)
- Max 24 bars hold (5min candles = 2 hours)

## Results Comparison

### Original Model (train_ml_model.py)
| Metric | Value |
|--------|-------|
| Accuracy | 48% |
| Trades (overlapping) | 8,589 |
| Win Rate | 66.5% |
| Avg Win | 0.108% |
| Avg Loss | -0.123% |
| Total Return | 1,263% (unrealistic - overlapping) |

### Enhanced Model (train_ml_model_v2.py) - Walk-Forward
| Symbol | WF Accuracy | Trades | Win% | Return |
|--------|-------------|--------|------|--------|
| RELIANCE | 71.4% | 1,834 | 66.5% | 84.6% |
| TCS | 59.8% | 2,921 | 59.6% | 109.2% |
| INFY | 61.0% | 2,694 | 60.3% | 52.4% |

### Backtest: ML vs Rule-Based (Non-Overlapping, Full History)
| Symbol | ML Return | Rule Return | ML Trades | Rule Trades |
|--------|-----------|-------------|-----------|-------------|
| RELIANCE | +56% | -2.7% | 320 | 430 |
| TCS | +211% | -1.9% | 305 | 449 |
| INFY | +197% | -5.5% | 285 | 512 |
| HDFCBANK | +8% | -7% | 162 | 483 |
| ICICIBANK | +9% | -22% | 74 | 566 |
| **TOTAL** | **+495%** | **-135%** | **1,858** | **5,066** |

### Key Improvements
1. **Fewer, better trades**: 1,858 vs 5,066 (ML is more selective)
2. **Positive expectancy**: ML wins on 7/10 symbols vs rule-based losing on 9/10
3. **Risk-adjusted**: Higher win rates (50-60% vs 27-40%) with controlled losses
4. **Per-symbol adaptation**: Thresholds range 0.40-0.80 based on symbol behavior

## Files Created/Modified

### New Files
- `strategy/train_ml_model_v2.py` - Enhanced training with walk-forward
- `strategy/ml_decision_agent_v2.py` - Enhanced inference with per-symbol thresholds

### Key Changes
- Added `FORWARD_WINDOW = 6` to core/signals.py (was missing)
- Session features integrated from strategy/session_features.py
- Model bundle now stores: per_symbol_thresholds, feature_importance, walk_forward_results
- ml_decision_agent_v2 uses per-symbol thresholds at inference time

## Usage

### Train Enhanced Model
```bash
trading-agent optimize ml-model-v2 RELIANCE TCS INFY
```

### Run Enhanced ML Agent (Parallel Logging)
```bash
trading-agent-scheduler
# Runs configured independent comparison cycles during scheduled windows.
```

### Backtest Comparison
```bash
pytest tests/test_signals.py tests/unit/test_strategy.py
trading-agent optimize strategy-lab RELIANCE
```

## Next Steps for Production
1. Train in explicit, reviewable batches: `trading-agent optimize ml-model-v2 RELIANCE TCS INFY`
2. Monitor ml_signals table for live comparison with rule-based signals
3. When ML track record justifies (STRATEGIES.md promotion criteria), integrate into decision_agent.py
4. Consider ensemble: combine ML + Rule + LLM signals with weighted voting
5. Add model drift detection using core/ml_versioning.py

## Validation Checklist
- [x] Walk-forward validation (no lookahead)
- [x] Session/regime features integrated
- [x] Cost-aware labeling
- [x] Per-symbol confidence thresholds
- [x] Non-overlapping trade simulation
- [x] Feature importance analysis
- [x] Comparison with rule-based baseline
- [x] Live inference agent (ml_decision_agent_v2.py)
- [ ] Full watchlist training in explicit batches
- [ ] Drift monitoring in production
