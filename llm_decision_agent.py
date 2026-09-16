"""
An LLM-based decision layer that reasons over the same inputs as
decision_agent.py, but makes its own independent call instead of
following fixed if/elif rules.

IMPORTANT: this runs ALONGSIDE decision_agent.py, writing to a
separate llm_signals table - it does NOT replace the rule-based agent.
Reasons to keep them separate:
  - The rule-based agent is what your backtest.py validates. This one
    isn't backtestable the same way (an LLM won't give identical
    output on identical historical input the way deterministic rules
    do), so you have no statistical evidence yet on whether it helps.
  - Comparing the two over time tells you whether LLM reasoning adds
    real value here, or just adds cost and unpredictability.
Only consider using this as your ONLY decision-maker once you've
compared its calls against the rule-based agent (and its real
outcomes) for a meaningful stretch of time.
"""
import os
import json
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
from quant_indicators import latest_indicators
from pattern_detection import detect_patterns
from decision_agent import decide as rule_based_decide
from db import get_connection

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-flash-lite-latest")

PROMPT_TEMPLATE = """You are an intraday trading signal assistant analyzing {symbol}.

Quant indicators:
- RSI: {rsi} (below 30 = oversold, above 70 = overbought)
- ADX: {adx} (below 20 = weak/no trend, above 25 = strong trend)
- ATR: {atr} (volatility measure)

Detected chart patterns: {patterns}
Pattern bias: {pattern_bias}

Recent news headlines for this stock:
{headlines}

Based on ALL of this together, decide: BUY, SELL, or HOLD.
Respond ONLY with JSON, no markdown fences, no preamble:
{{"action": "BUY|SELL|HOLD", "confidence": <float 0.0-1.0>, "rationale": "<2-3 sentences explaining your reasoning>"}}
"""


def fetch_recent_headlines(symbol: str, limit: int = 5) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT headline FROM news WHERE symbol = ? ORDER BY published_at DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    conn.close()
    return [r["headline"] for r in rows]


def llm_decide(symbol: str) -> dict:
    indicators = latest_indicators(symbol)
    if "error" in indicators:
        return {"symbol": symbol, "action": "HOLD", "confidence": 0.0, "rationale": indicators["error"]}

    pattern_result = detect_patterns(symbol)
    headlines = fetch_recent_headlines(symbol)
    headlines_text = "\n".join(f"- {h}" for h in headlines) if headlines else "(no recent news)"

    prompt = PROMPT_TEMPLATE.format(
        symbol=symbol, rsi=indicators["rsi"], adx=indicators["adx"], atr=indicators["atr"],
        patterns=pattern_result["patterns"] or "none", pattern_bias=pattern_result["bias"],
        headlines=headlines_text,
    )

    try:
        response = model.generate_content(prompt)
        text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        result = json.loads(text)
    except Exception as e:
        return {"symbol": symbol, "action": "HOLD", "confidence": 0.0, "rationale": f"LLM call failed: {e}"}

    result["symbol"] = symbol
    return result


def run_llm_decision_cycle(symbols: list):
    """
    IMPORTANT: opens a fresh connection and commits for EACH symbol,
    not once at the end of the loop. The old version held one
    connection open across the entire loop - with real Gemini API
    calls per symbol (network latency x hundreds of symbols can add
    up to several minutes), that held a write lock far longer than
    any reasonable busy_timeout, causing 'database is locked' errors
    in OTHER processes (live_ticker.py's flush thread, scheduler.py)
    trying to write at the same time. Committing per-symbol means the
    lock is only held for the brief moment of each individual write,
    not for the duration of the slow LLM call before it.
    """
    timestamp = datetime.now().isoformat()

    for symbol in symbols:
        llm_result = llm_decide(symbol)  # slow (network call) - happens BEFORE opening any connection
        rule_result = rule_based_decide(symbol)  # for side-by-side comparison, not used as input

        conn = get_connection()  # opened only for the brief write below
        conn.execute(
            "INSERT INTO llm_signals (symbol, timestamp, action, confidence, rationale, rule_based_action) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, timestamp, llm_result["action"], llm_result.get("confidence"),
             llm_result.get("rationale"), rule_result["action"]),
        )
        conn.commit()
        conn.close()

        agree = "AGREE" if llm_result["action"] == rule_result["action"] else "DIFFER"
        print(f"{symbol}: LLM={llm_result['action']} | Rules={rule_result['action']} [{agree}]")


if __name__ == "__main__":
    from db import get_watchlist_symbols
    run_llm_decision_cycle(get_watchlist_symbols())
