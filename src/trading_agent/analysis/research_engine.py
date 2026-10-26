"""Full-universe research: aligned rule/ML signals, news evidence and sizing.

All computations use one completed-candle frame per symbol. No broker orders,
training, per-symbol backtests, or external AI calls run inside this engine.
"""
import math
from datetime import UTC, datetime, timedelta
from datetime import time
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from trading_agent.core.config import get_settings
from trading_agent.core.position_sizing import round_to_tick
from trading_agent.core.signals import evaluate_signals
from trading_agent.paths import LIVE_SYMBOLS_TXT, SYMBOLS_TXT
from trading_agent.strategy.research_features import FEATURES, ResearchModel, prepare_research_frame
from trading_agent.analysis.research_news import read_news, usable_sentiment
from trading_agent.storage.parquet_store import get_parquet_store

INTERVAL_SECONDS = {"minute": 60, "5minute": 300, "15minute": 900, "day": 86400}


def refresh_eligibility(row, now):
    """Recheck expiry on every scan read; never trust a saved `stale=False`."""
    if "timestamp" not in row:  # Compatibility for non-market test/legacy rows.
        return
    stamp = datetime.fromisoformat(row["timestamp"])
    seconds = INTERVAL_SECONDS[row["interval"]]
    expiry = stamp + timedelta(seconds=seconds*2+15)
    if row.get("ml", {}).get("model_kind") == "outcome":
        expiry = stamp + timedelta(seconds=seconds*2)
    row["expires_at"] = expiry.isoformat()
    row["stale"] = now >= expiry or stamp > now
    row["age_minutes"] = round(max(0, (now-stamp).total_seconds()/60), 1)
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    row["session_open"] = local.weekday() < 5 and time(9, 15) <= local.time() < time(15, 20)
    reason = "Expired candle snapshot" if row["stale"] else (
        "Outside configured entry session" if not row["session_open"] else None)
    if reason and row.get("plan"):
        row["plan"].update(blocked=True, block_reason=reason, quantity=0, position_value=0, risk_amount=0)


def universe():
    """The whole configured file, deduplicated; never an arbitrary first 15/500."""
    if SYMBOLS_TXT.exists():
        symbols = [line.strip().upper() for line in SYMBOLS_TXT.read_text(encoding="utf-8-sig").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    else:
        symbols = get_parquet_store().get_symbols()
    return sorted(set(symbols))


def live_symbols():
    """The live set: 1-minute bars from the ticker, scanned as each bar closes.

    ``data/live_symbols.txt`` overrides the shipped ``live_symbols.txt``; empty when neither exists.
    """
    from trading_agent.core.config import get_settings

    path = get_settings().data_dir / "live_symbols.txt"
    path = path if path.exists() else LIVE_SYMBOLS_TXT
    if not path.exists():
        return []
    return sorted({line.strip().upper() for line in path.read_text(encoding="utf-8-sig").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")})


def combine(rule, rule_confidence, ml, mode="rule_ml_combined"):
    """Disagreement never falls back silently to the original rule direction."""
    if mode == "rule_only":
        return rule, rule_confidence, "Rule-only mode selected"
    if ml["status"] != "available":
        return "HOLD", 0.0, f"ML unavailable: {ml['status']}"
    action, confidence = ml["action"], ml["confidence"]
    if mode == "ml_only":
        return action, confidence, "ML-only mode selected"
    if rule == action:
        return rule, (rule_confidence + confidence) / 2 if rule != "HOLD" else 0.0, "Rules and ML agree"
    if rule == "HOLD":
        return action, confidence * 0.6, "ML directional; rules abstain (reduced score)"
    if action == "HOLD":
        return rule, rule_confidence * 0.6, "Rules directional; ML abstains (reduced score)"
    return "HOLD", 0.0, "Rules and ML conflict; no directional opportunity"


def completed_frame(store, symbol, interval, now):
    frame = store.read_latest(symbol, interval, n=20000)
    if frame.empty:
        return frame
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, format="mixed")
    cutoff = now - timedelta(seconds=INTERVAL_SECONDS[interval])
    return frame.loc[frame.timestamp <= cutoff].sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def analyze_frame(symbol, frame, interval, model, mode, now, news=None):
    if len(frame) < 60:
        raise ValueError("insufficient_history")
    df = prepare_research_frame(frame)
    last = df.iloc[-1]
    if not np.isfinite(last[FEATURES].to_numpy(dtype=float)).all() or last.atr <= 0 or last.close <= 0:
        raise ValueError("invalid_indicators")
    candle_time = last.timestamp.to_pydatetime()
    age = max(0.0, (now - candle_time).total_seconds() / 60)
    stale = age > (INTERVAL_SECONDS[interval] * 2 + 15) / 60
    action_code = int(evaluate_signals(np.array([last.rsi]), np.array([last.adx]), np.array([last.pattern_code]))[0])
    rule = ["HOLD", "BUY", "SELL"][action_code]
    rule_confidence = min(0.95, 0.5 + min(float(last.adx), 50) / 200 + 0.1 * (last.pattern_code != 0)) if rule != "HOLD" else 0.0
    ml = model.predict(df, symbol, interval)
    if ml.get("status") == "available":
        from trading_agent.strategy.model_monitoring import log_live_features

        log_live_features(symbol, interval, last)
    action, confidence, reasoning = combine(rule, rule_confidence, ml, mode)
    sentiment = usable_sentiment(news or {}, candle_time)
    score = confidence * 100
    agreements, disagreements = [], []
    if rule == ml.get("action") and rule != "HOLD":
        agreements.append(f"Rule and ML agree on {rule}")
    elif rule != "HOLD" and ml.get("action") in {"BUY", "SELL"}:
        disagreements.append(f"Rules: {rule}; ML: {ml['action']}")
    if sentiment and action != "HOLD":
        adjustment = float(sentiment["score"]) * float(sentiment["confidence"]) * 5 * (1 if action == "BUY" else -1)
        score += adjustment
        (agreements if adjustment >= 0 else disagreements).append(f"LLM news sentiment {sentiment['score']:+.2f}")
    patterns = [name for name in ("doji", "hammer", "bullish_engulfing", "bearish_engulfing") if bool(last[name])]
    room = None
    if action in {"BUY", "SELL"}:
        values = df.high.to_numpy() if action == "BUY" else -df.low.to_numpy()
        indices, _ = find_peaks(values[-200:], prominence=float(last.atr))
        levels = values[-200:][indices]
        reference = float(last.close) * (1 if action == "BUY" else -1)
        opposing = levels[levels > reference]
        if len(opposing):
            room = float(opposing.min() - reference)
    turnover = float((df.close * df.volume).tail(20).mean())
    quality = []
    if last.volume <= 0:
        quality.append("Latest candle has no traded volume")
    if not np.isfinite(turnover) or turnover < 100000:
        quality.append("Average 20-bar traded value below INR 100,000 per bar (liquidity proxy)")
    recent = df.tail(20)
    local_dates = recent.timestamp.dt.tz_convert("Asia/Kolkata").dt.date
    same_day = local_dates == local_dates.shift()
    if ((recent.timestamp.diff().dt.total_seconds() > INTERVAL_SECONDS[interval]*1.5) & same_day).any():
        quality.append("Missing recent same-session candles")
    row = {
        "symbol": symbol, "interval": interval, "timestamp": candle_time.isoformat(),
        "price": float(last.close), "atr": float(last.atr), "rsi": float(last.rsi), "adx": float(last.adx),
        "volume": int(last.volume), "patterns": patterns, "pattern_bias": ["neutral", "bullish", "bearish"][int(last.pattern_code)],
        "rule": {"action": rule, "confidence": rule_confidence}, "ml": ml,
        "action": action, "confidence": confidence, "score": round(max(0, score), 3) if action != "HOLD" else 0.0,
        "reasoning": reasoning, "agreements": agreements, "disagreements": disagreements,
        "sentiment": sentiment, "stale": stale, "age_minutes": round(age, 1), "room_to_target": room,
        "quality_reasons": quality, "average_bar_turnover": turnover,
    }
    refresh_eligibility(row, now)
    return row, df


def size_opportunities(rows, capital, positions, limits, risk_pct=1.0, reward_risk=2.0, require_validated=False,
                       daily_risk_headroom=None, tick_size=None):
    """Rank once, then reserve across the whole bullish/bearish basket.

    Entry-cost notional, no short-sale leverage, no correlation claim. The
    paper API revalidates against the current ledger before accepting entries.
    """
    def rank_key(row):
        evidence = row.get("validation", {})
        expected = evidence.get("expected_net_return_pct")
        qualified = evidence.get("status") == "qualified_research" and expected is not None and expected > 0
        return (row["stale"], not qualified, -(expected if qualified else row["score"]), row["symbol"])
    ranked = sorted(rows, key=rank_key)
    held = {p["symbol"] for p in positions}
    used_value = sum(p["position_value"] for p in positions)
    used_risk = sum(p["risk_amount"] for p in positions)
    slots = max(0, limits.max_positions - len(positions))
    # Worst case today: realized P&L and every open stop already count (ledger_guard).
    daily_left = math.inf if daily_risk_headroom is None else daily_risk_headroom
    for row in ranked:
        entry, distance = row["price"], row["atr"] * 1.5
        sign = 1 if row["action"] == "BUY" else -1
        direction = row["action"] in {"BUY", "SELL"}
        stop = entry - sign * distance if direction else None
        if direction and math.isfinite(distance) and distance > 0:
            # Stop on the exchange tick, rounded away from the entry; risk follows the rounded stop.
            tick = (tick_size(row["symbol"]) if tick_size else None) or 0.05
            stop = round_to_tick(stop, tick, away_from=entry)
            distance = abs(entry - stop)
            target = round_to_tick(entry + sign * distance * reward_risk, tick)
        else:
            target = entry + sign * distance * reward_risk if direction else None
        reason = None
        if not direction:
            reason = "No directional hybrid signal"
        elif row["stale"]:
            reason = "Stale candles: historical research only"
        elif row.get("session_open") is False:
            reason = "Outside configured entry session"
        elif row.get("quality_reasons"):
            reason = "; ".join(row["quality_reasons"])
        elif require_validated and (row.get("validation", {}).get("status") != "qualified_research"
                                   or (row["validation"].get("expected_net_return_pct") or 0) <= 0):
            reason = "No independently qualified positive-expectancy evidence"
        elif row["symbol"] in held:
            reason = "Position already open"
        elif slots == 0:
            reason = "Portfolio position-count limit"
        elif stop <= 0 or target <= 0:
            reason = "Invalid price bracket"
        elif distance < entry * limits.min_stop_distance_pct:
            reason = f"Stop within {limits.min_stop_distance_pct:.2%} of entry: too tight to survive the spread"
        risk_budget = max(0, min(capital * min(risk_pct / 100, limits.max_trade_risk_pct),
                                 capital * limits.max_risk_pct - used_risk, daily_left))
        notional_budget = max(0, min(capital * limits.max_single_position_pct, capital * limits.max_exposure_pct - used_value, capital - used_value))
        quantity = max(0, min(int(risk_budget / distance), int(notional_budget / entry))) if direction and distance > 0 else 0
        if not reason and (quantity == 0 or quantity * entry < 5000):
            reason = ("Worst-case daily loss limit: no risk budget left today" if daily_left < distance
                      else "Insufficient capital or remaining portfolio risk/exposure")
        approved = quantity if not reason else 0
        if approved:
            used_value += approved * entry
            used_risk += approved * distance
            daily_left -= approved * distance
            slots -= 1
        row["plan"] = {"entry_price": entry, "stop_loss": stop, "take_profit": target,
                       "quantity": approved, "position_value": approved * entry, "risk_amount": approved * distance,
                       "reward_risk": reward_risk, "blocked": bool(reason), "block_reason": reason,
                       "room_reward_risk": row["room_to_target"] / distance if row["room_to_target"] is not None else None}
    return ranked


def model_for_research():
    from trading_agent.strategy.model_monitoring import DriftWithheldModel, fallback_state

    model_dir = Path(get_settings().ml.model_dir)
    if fallback_state().get("active"):
        # Feature drift: use the previous artifact if kept, otherwise withdraw ML.
        previous = model_dir / "ml_model.previous.joblib"
        return ResearchModel(previous) if previous.exists() else DriftWithheldModel()
    if (model_dir / "outcome-active.json").exists():
        from trading_agent.strategy.outcome_model import OutcomeResearchModel
        return OutcomeResearchModel()
    return ResearchModel(Path(get_settings().ml.model_dir) / "ml_model.joblib")


def chart_payload(symbol, df, limit=200):
    view = df.tail(limit)
    return {
        "symbol": symbol, "insufficient_data": False,
        "candles": {k: view[k].tolist() for k in ("timestamp", "open", "high", "low", "close", "volume")},
        "indicators": {k: view[k].tolist() for k in ("rsi", "adx", "atr", "ma20", "ma50", "ema9", "ema21", "vwap")},
        "patterns": {k: view[k].tolist() for k in ("doji", "hammer", "bullish_engulfing", "bearish_engulfing")},
        "signal_markers": [
            {"timestamp": timestamp, "action": ["HOLD", "BUY", "SELL"][int(action)]}
            for timestamp, action in zip(view.timestamp, evaluate_signals(
                view.rsi.to_numpy(), view.adx.to_numpy(), view.pattern_code.to_numpy()), strict=True)
            if action != 0
        ],
        "latest": {k: view.iloc[-1][k] for k in ("close", "rsi", "adx", "atr")},
    }


def analyze_symbol(symbol, interval="minute", mode="rule_ml_combined", model=None, now=None):
    now = now or datetime.now(UTC)
    frame = completed_frame(get_parquet_store(), symbol, interval, now)
    return analyze_frame(symbol, frame, interval, model or model_for_research(), mode, now, read_news(symbol))
