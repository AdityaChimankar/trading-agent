"""
Generates an end-of-day narrative summary from everything the pipeline
logged today: rule-based signals, LLM signals (if run), and news
sentiment. This is the lowest-risk LLM use in the whole project - it
only summarizes what already happened, it never influences a decision.

Run after market close: python digest.py
"""
import os
from datetime import datetime, date
import google.generativeai as genai
from dotenv import load_dotenv
from db import get_connection

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-flash-lite-latest")

PROMPT_TEMPLATE = """Write a concise end-of-day intraday trading summary for {trade_date}
based on this raw data. Plain English, 3-4 short paragraphs, no fluff.

Rule-based signals fired today:
{signals_summary}

LLM vs rule-based agreement:
{agreement_summary}

News sentiment overview:
{sentiment_summary}

Cover: which stocks were most active, whether signals leaned bullish/bearish/mixed
overall, any notable agreement or disagreement between the rule-based and LLM
agents, and one thing worth double-checking tomorrow. Do not give financial advice
or price predictions - this is a summary of what the system did, not a forecast."""


def gather_signals_summary(conn, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT symbol, action, COUNT(*) as cnt FROM signals "
        "WHERE date(timestamp) = ? GROUP BY symbol, action ORDER BY symbol",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "No signals recorded today."
    return "\n".join(f"- {r['symbol']}: {r['action']} x{r['cnt']}" for r in rows)


def gather_agreement_summary(conn, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT symbol, action, rule_based_action, COUNT(*) as cnt FROM llm_signals "
        "WHERE date(timestamp) = ? GROUP BY symbol, action, rule_based_action",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "LLM decision agent did not run today."
    lines = []
    for r in rows:
        agree = "agreed" if r["action"] == r["rule_based_action"] else f"differed (rules said {r['rule_based_action']})"
        lines.append(f"- {r['symbol']}: LLM said {r['action']} x{r['cnt']} - {agree}")
    return "\n".join(lines)


def gather_sentiment_summary(conn, trade_date: str) -> str:
    rows = conn.execute(
        "SELECT symbol, AVG(score) as avg_score, COUNT(*) as cnt FROM sentiment "
        "WHERE date(scored_at) = ? GROUP BY symbol ORDER BY avg_score DESC",
        (trade_date,),
    ).fetchall()
    if not rows:
        return "No news scored today."
    return "\n".join(f"- {r['symbol']}: avg sentiment {r['avg_score']:.2f} across {r['cnt']} headlines" for r in rows)


def generate_digest(trade_date: str = None) -> str:
    trade_date = trade_date or date.today().isoformat()
    conn = get_connection()

    prompt = PROMPT_TEMPLATE.format(
        trade_date=trade_date,
        signals_summary=gather_signals_summary(conn, trade_date),
        agreement_summary=gather_agreement_summary(conn, trade_date),
        sentiment_summary=gather_sentiment_summary(conn, trade_date),
    )

    response = model.generate_content(prompt)
    summary = response.text.strip()

    conn.execute(
        "INSERT OR REPLACE INTO digests (date, summary, generated_at) VALUES (?, ?, ?)",
        (trade_date, summary, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return summary


if __name__ == "__main__":
    summary = generate_digest()
    print("\n--- Daily Digest ---\n")
    print(summary)
