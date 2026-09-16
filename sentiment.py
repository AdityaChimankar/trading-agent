"""
Scores unscored news headlines using Gemini Flash-Lite. Only scores
headlines already matched to a watchlist symbol, to keep API calls
(and cost) down - unmatched general market news is skipped here.

Setup: pip install google-generativeai, then set GEMINI_API_KEY in .env
"""
import os
import json
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
from db import get_connection

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-flash-lite-latest")

PROMPT_TEMPLATE = """You are scoring financial news sentiment for {symbol}.
Headline: "{headline}"

Respond ONLY with JSON, no markdown fences, no preamble:
{{"score": <float from -1.0 (very negative) to 1.0 (very positive)>, "rationale": "<one short sentence>"}}
"""


def fetch_unscored_news(conn, limit: int = 20):
    return conn.execute(
        """SELECT id, symbol, headline FROM news
           WHERE symbol IS NOT NULL
           AND id NOT IN (SELECT news_id FROM sentiment)
           LIMIT ?""",
        (limit,),
    ).fetchall()


def score_headline(symbol: str, headline: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(symbol=symbol, headline=headline)
    response = model.generate_content(prompt)
    text = response.text.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(text)


def run_sentiment_pass():
    conn = get_connection()
    rows = fetch_unscored_news(conn)
    scored_at = datetime.now().isoformat()

    for row in rows:
        try:
            result = score_headline(row["symbol"], row["headline"])
            conn.execute(
                "INSERT INTO sentiment (news_id, symbol, score, rationale, scored_at) VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["symbol"], result["score"], result["rationale"], scored_at),
            )
        except Exception as e:
            print(f"Failed to score news_id={row['id']}: {e}")

    conn.commit()
    conn.close()
    print(f"Scored {len(rows)} headlines")


def latest_sentiment(symbol: str) -> float | None:
    """Average sentiment score from the most recent scored headlines for a symbol."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT score FROM sentiment WHERE symbol = ? ORDER BY scored_at DESC LIMIT 5",
        (symbol,),
    ).fetchall()
    conn.close()
    if not rows:
        return None
    return sum(r["score"] for r in rows) / len(rows)


if __name__ == "__main__":
    run_sentiment_pass()
