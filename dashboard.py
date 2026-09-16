"""
Local visualization dashboard. Run with: streamlit run dashboard.py
Opens at http://localhost:8501 - no cloud hosting needed.
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from db import get_connection
from quant_indicators import load_candles, compute_rsi, compute_atr, compute_adx
from rankings import rank_watchlist, top_bullish, top_bearish

st.set_page_config(page_title="Intraday Trading Agent", layout="wide")
st.title("Intraday Trading Agent Dashboard")

conn = get_connection()

# --- Sidebar: Top 10 Bullish / Bearish panel ---
# Cached for 60s so switching the toggle or opening a symbol doesn't
# re-score the whole watchlist on every rerun - only refreshes when
# the cache expires or new candle/pattern data changes meaningfully.
@st.cache_data(ttl=60)
def _cached_rankings():
    return rank_watchlist()

with st.sidebar:
    st.header("Watchlist Bias")
    view = st.radio("Show", ["Bullish", "Bearish"], horizontal=True)

    scored = _cached_rankings()
    ranked_list = top_bullish(scored) if view == "Bullish" else top_bearish(scored)
    score_key = "bullish_score" if view == "Bullish" else "bearish_score"

    if not ranked_list:
        st.write(f"No {view.lower()} candidates right now.")
    else:
        for entry in ranked_list:
            label = f"{entry['symbol']}  ·  RSI {entry['rsi']}  ·  score {entry[score_key]}"
            if st.button(label, key=f"{view}_{entry['symbol']}", use_container_width=True):
                st.session_state["selected_symbol"] = entry["symbol"]

    st.divider()
    st.header("Position Sizing")
    capital = st.number_input("Available capital (Rs.)", min_value=1000.0, value=100000.0, step=1000.0)
    st.caption("Risk 1% of capital per trade, stop-loss set at 1.5x ATR, "
               "capped at 20% of capital per position. Adjust in position_sizing.py.")

# --- Today's digest, if generated ---
from datetime import date
digest_row = conn.execute("SELECT summary FROM digests WHERE date = ?", (date.today().isoformat(),)).fetchone()
if digest_row:
    with st.expander("📋 Today's Digest", expanded=True):
        st.write(digest_row["summary"])

symbols = [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist").fetchall()]
default_symbol = st.session_state.get("selected_symbol")
default_index = symbols.index(default_symbol) if default_symbol in symbols else 0
symbol = st.selectbox("Symbol", symbols, index=default_index) if symbols else None

if symbol:
    df = load_candles(symbol, limit=200)

    if len(df) < 5:
        st.warning("Not enough candle data yet - run fetch_historical.py first.")
    else:
        df["rsi"] = compute_rsi(df)
        df["atr"] = compute_atr(df)
        df["adx"] = compute_adx(df)
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma50"] = df["close"].rolling(50).mean()

        # --- Advanced multi-panel chart: price+MAs+signals / volume / RSI / ADX,
        # all sharing one synced x-axis (zoom or pan any panel, they move together) ---
        from plotly.subplots import make_subplots
        fig = make_subplots(
            rows=4, cols=1, shared_xaxes=True,
            row_heights=[0.5, 0.15, 0.175, 0.175], vertical_spacing=0.03,
            subplot_titles=(f"{symbol} - Price", "Volume", "RSI", "ADX"),
        )

        fig.add_trace(go.Candlestick(
            x=df["timestamp"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=symbol,
        ), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ma20"], name="MA20",
                                  line=dict(color="orange", width=1)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["ma50"], name="MA50",
                                  line=dict(color="purple", width=1)), row=1, col=1)

        # Overlay actual BUY/SELL signals this symbol has fired, so you can see
        # exactly where the rule-based agent triggered relative to price action
        signal_markers = pd.read_sql_query(
            "SELECT timestamp, action FROM signals WHERE symbol = ? AND action != 'HOLD' "
            "AND timestamp >= ? ORDER BY timestamp",
            conn, params=(symbol, df["timestamp"].min()),
        )
        if not signal_markers.empty:
            buys = signal_markers[signal_markers["action"] == "BUY"]
            sells = signal_markers[signal_markers["action"] == "SELL"]
            price_lookup = df.set_index("timestamp")["close"]
            if not buys.empty:
                buy_prices = buys["timestamp"].map(price_lookup)
                fig.add_trace(go.Scatter(
                    x=buys["timestamp"], y=buy_prices, mode="markers", name="BUY signal",
                    marker=dict(symbol="triangle-up", size=12, color="green"),
                ), row=1, col=1)
            if not sells.empty:
                sell_prices = sells["timestamp"].map(price_lookup)
                fig.add_trace(go.Scatter(
                    x=sells["timestamp"], y=sell_prices, mode="markers", name="SELL signal",
                    marker=dict(symbol="triangle-down", size=12, color="red"),
                ), row=1, col=1)

        # --- Pattern markers - flag candles where doji/hammer/engulfing fired ---
        from pattern_detection import compute_pattern_columns
        pattern_df = compute_pattern_columns(df)
        for col, marker_symbol, color in [
            ("doji", "circle", "gray"), ("hammer", "star", "blue"),
            ("bullish_engulfing", "diamond", "green"), ("bearish_engulfing", "diamond", "red"),
        ]:
            hits = pattern_df[pattern_df[col] == True]
            if not hits.empty:
                fig.add_trace(go.Scatter(
                    x=hits["timestamp"], y=hits["high"] * 1.002, mode="markers", name=col,
                    marker=dict(symbol=marker_symbol, size=8, color=color),
                ), row=1, col=1)

        fig.add_trace(go.Bar(x=df["timestamp"], y=df["volume"], name="Volume",
                              marker_color="lightblue"), row=2, col=1)

        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["rsi"], name="RSI",
                                  line=dict(color="teal")), row=3, col=1)
        fig.add_hline(y=70, line_dash="dot", line_color="red", row=3, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="green", row=3, col=1)

        fig.add_trace(go.Scatter(x=df["timestamp"], y=df["adx"], name="ADX",
                                  line=dict(color="brown")), row=4, col=1)
        fig.add_hline(y=20, line_dash="dot", line_color="gray", row=4, col=1)

        fig.update_layout(height=850, xaxis_rangeslider_visible=False, showlegend=True,
                           legend=dict(orientation="h", yanchor="bottom", y=1.02))
        st.plotly_chart(fig, use_container_width=True)

        # --- Indicator summary ---
        col1, col2, col3 = st.columns(3)
        col1.metric("RSI (latest)", f"{df['rsi'].iloc[-1]:.1f}")
        col2.metric("ADX (latest)", f"{df['adx'].iloc[-1]:.1f}")
        col3.metric("ATR (latest)", f"{df['atr'].iloc[-1]:.2f}")

        # --- Position sizing, portfolio-adjusted for correlation + total risk budget ---
        from portfolio_risk import calculate_portfolio_adjusted_position, get_open_positions, add_open_position, close_position
        latest_signal = conn.execute(
            "SELECT action FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1", (symbol,)
        ).fetchone()
        st.subheader("Position Sizing (ATR-based, portfolio-adjusted)")
        if not latest_signal or latest_signal["action"] == "HOLD":
            st.write("No active BUY/SELL signal for this symbol - nothing to size.")
        else:
            adjusted = calculate_portfolio_adjusted_position(symbol, latest_signal["action"], capital)
            plan = adjusted.base_plan
            if plan is None:
                st.write("Not enough data to calculate a position yet.")
            else:
                pcol1, pcol2, pcol3, pcol4 = st.columns(4)
                pcol1.metric("Stop-loss", plan.stop_loss)
                pcol2.metric("Take-profit", plan.take_profit)
                pcol3.metric("Approved size", f"{adjusted.approved_size} shares",
                             delta=f"{adjusted.approved_size - plan.position_size} vs per-trade-only" if adjusted.approved_size != plan.position_size else None)
                pcol4.metric("Approved value", f"Rs.{adjusted.approved_value:,.0f}")

                if adjusted.blocked:
                    st.warning(f"Blocked: {adjusted.block_reason}")
                elif adjusted.approved_size < plan.position_size:
                    st.info(f"Reduced from {plan.position_size} to {adjusted.approved_size} shares - "
                            f"total risk budget {adjusted.total_risk_used_pct}% used, "
                            + (f"correlated with open {', '.join(adjusted.correlated_with)} "
                               f"(cluster at {adjusted.cluster_exposure_pct}%)" if adjusted.correlated_with else ""))
                else:
                    st.caption(f"Risking Rs.{plan.risk_amount:,.0f} · portfolio risk budget "
                               f"{adjusted.total_risk_used_pct}% used before this trade")

                if not adjusted.blocked and adjusted.approved_size > 0:
                    if st.button(f"Record as open position ({adjusted.approved_size} shares)", key=f"open_{symbol}"):
                        add_open_position(plan)
                        st.rerun()
                st.caption("This is a calculated plan, not an order - nothing is placed automatically.")

        # --- Open positions ledger (paper tracking, for the portfolio-risk checks above) ---
        st.subheader("Open Positions (paper-tracked)")
        open_positions = get_open_positions()
        if not open_positions:
            st.write("No open positions recorded.")
        else:
            for pos in open_positions:
                pcols = st.columns([3, 1])
                pcols[0].write(f"**{pos['symbol']}** {pos['action']} · {pos['position_size']} shares · "
                                f"Rs.{pos['position_value']:,.0f} · risk Rs.{pos['risk_amount']:,.0f} · opened {pos['opened_at'][:16]}")
                if pcols[1].button("Close", key=f"close_{pos['id']}"):
                    close_position(pos["id"])
                    st.rerun()

        # --- Pattern detection ---
        from pattern_detection import detect_patterns
        pattern_result = detect_patterns(symbol)
        st.subheader("Detected Chart Patterns")
        if pattern_result["patterns"]:
            st.info(f"**Bias: {pattern_result['bias'].upper()}** — {', '.join(pattern_result['patterns'])}")
        else:
            st.write("No patterns detected in the current window.")

        # --- Sentiment feed ---
        st.subheader("Recent News + Sentiment")
        news_df = pd.read_sql_query(
            """SELECT n.headline, n.published_at, s.score, s.rationale
               FROM news n LEFT JOIN sentiment s ON n.id = s.news_id
               WHERE n.symbol = ? ORDER BY n.published_at DESC LIMIT 10""",
            conn, params=(symbol,),
        )
        st.dataframe(news_df, use_container_width=True)

        # --- Signal log ---
        st.subheader("Signal History (Rule-Based)")
        signals_df = pd.read_sql_query(
            """SELECT timestamp, action, rsi, adx, sentiment_score, rationale
               FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT 20""",
            conn, params=(symbol,),
        )
        st.dataframe(signals_df, use_container_width=True)

        # --- LLM vs rule-based comparison ---
        st.subheader("LLM Agent vs Rule-Based Agent")
        llm_df = pd.read_sql_query(
            """SELECT timestamp, action as llm_action, rule_based_action, confidence, rationale
               FROM llm_signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT 20""",
            conn, params=(symbol,),
        )
        if not llm_df.empty:
            agree_pct = (llm_df["llm_action"] == llm_df["rule_based_action"]).mean() * 100
            st.metric("Agreement rate (last 20)", f"{agree_pct:.0f}%")
            st.dataframe(llm_df, use_container_width=True)
        else:
            st.write("LLM decision agent hasn't run yet for this symbol.")

        # --- ML vs rule-based comparison ---
        st.subheader("ML Model vs Rule-Based Agent")
        ml_df = pd.read_sql_query(
            """SELECT timestamp, action as ml_action, rule_based_action, confidence
               FROM ml_signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT 20""",
            conn, params=(symbol,),
        )
        if not ml_df.empty:
            ml_agree_pct = (ml_df["ml_action"] == ml_df["rule_based_action"]).mean() * 100
            st.metric("Agreement rate (last 20)", f"{ml_agree_pct:.0f}%")
            st.dataframe(ml_df, use_container_width=True)
        else:
            st.write("ML decision agent hasn't run yet for this symbol (train a model first with train_ml_model.py).")

conn.close()
