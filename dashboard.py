"""
Local visualization dashboard. Run with: streamlit run dashboard.py
Opens at http://localhost:8501 - no cloud hosting needed.
"""
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from db import get_connection, init_db
from quant_indicators import load_candles, compute_rsi, compute_atr, compute_adx
from rankings import rank_watchlist, rank_watchlist_with_sizing, annotate_contradictions, get_contradictions
from position_monitor import get_all_position_statuses
from portfolio_risk import close_position as _close_position
from opportunity_finder import find_opportunities

# Ensures the schema (including any new columns from a migration) is
# up to date every time the dashboard starts - init_db() is idempotent
# (CREATE TABLE IF NOT EXISTS + safe ALTER TABLE that ignores "already
# exists" errors), so this is harmless to call on every launch.
init_db()

st.set_page_config(page_title="Intraday Trading Agent", layout="wide")
st.title("Intraday Trading Agent Dashboard")

conn = get_connection()

# --- Cached data fetches, both defined up top so the sidebar (which
# needs both the technical rankings AND live position P&L to cross-check
# them against each other) and the main-body Position Monitor panel can
# share the same cached results instead of fetching twice. ---

@st.cache_data(ttl=60)
def _cached_rankings():
    return rank_watchlist()

# Signal-reversal checking calls decision_agent.decide() per position,
# which is real computation (not free at scale) - cache briefly so
# switching tabs/toggles doesn't repeatedly recompute it.
@st.cache_data(ttl=30)
def _cached_position_statuses():
    return get_all_position_statuses(fetch_current_signals=True)

position_statuses = _cached_position_statuses()

# --- Sidebar: capital input first (sizing below needs it), then
# bias-vs-P&L contradiction warnings, then the ranked watchlist -
# now combined with an ACTUAL portfolio-adjusted position size and
# open-position status per symbol, not just a raw score. ---
with st.sidebar:
    st.header("Watchlist Bias")

    scored = _cached_rankings()
    scored = annotate_contradictions(scored, position_statuses)
    scored_by_symbol = {s["symbol"]: s for s in scored}

    contradictions = get_contradictions(scored)
    if contradictions:
        with st.expander(f"⚠️ {len(contradictions)} bias contradiction(s)", expanded=True):
            st.caption("Open positions where this panel's technical bias "
                       "disagrees with live P&L - the position is winning "
                       "in the opposite direction of the flag below.")
            for c in contradictions:
                st.warning(f"**{c['symbol']}**: {c['contradiction']}")

    capital = st.number_input("Available capital (Rs.)", min_value=1000.0, value=100000.0, step=1000.0,
                               help="Used to size every suggested BUY/SELL on this page.")

    @st.cache_data(ttl=60)
    def _cached_rankings_with_sizing(capital_value):
        return rank_watchlist_with_sizing(capital_value, n=10)

    sized = _cached_rankings_with_sizing(capital)

    view = st.radio("Show", ["Bullish", "Bearish"], horizontal=True)
    ranked_list = sized["bullish"] if view == "Bullish" else sized["bearish"]
    score_key = "bullish_score" if view == "Bullish" else "bearish_score"

    if not ranked_list:
        st.write(f"No {view.lower()} candidates right now.")
    else:
        for entry in ranked_list:
            pin = "📌 " if entry.get("has_open_position") else ""
            warn = "⚠️ " if scored_by_symbol.get(entry["symbol"], {}).get("contradiction") else ""
            label = f"{warn}{pin}{entry['symbol']} · RSI {entry['rsi']} · trend {entry.get('trend', '?')} · score {entry[score_key]}"
            if st.button(label, key=f"{view}_{entry['symbol']}", use_container_width=True):
                st.session_state["selected_symbol"] = entry["symbol"]

            contradiction = scored_by_symbol.get(entry["symbol"], {}).get("contradiction")
            if contradiction:
                st.caption(f"⚠️ {contradiction}")

            if entry["blocked"]:
                st.caption(f"🚫 {entry['action']} blocked - {entry['block_reason']}")
            elif entry["suggested_size"] is not None and entry["suggested_size"] > 0:
                st.caption(f"Suggested {entry['action']}: {entry['suggested_size']} sh @ {entry['entry_price']} "
                           f"(Rs.{entry['suggested_value']:,.0f}) · stop {entry['stop_loss']} · target {entry['take_profit']}")
            else:
                st.caption("Not enough data to size this trade yet.")

    st.divider()
    st.caption("Risk 1% of capital per trade, stop-loss set at 1.5x ATR, "
               "capped at 20% of capital per position/cluster, 6% total risk budget. "
               "Adjust in position_sizing.py / portfolio_risk.py.")

# --- Top Opportunity: the SINGLE highest-conviction bullish and bearish
# pick right now, front and center. "Conviction" means agreement across
# your independent signals (technical score, ML model, LLM agent,
# sentiment) - NOT a promise of profit, and NOT a bigger position for a
# "better" setup (position sizing deliberately risks a fixed % of
# capital per trade regardless of conviction - that consistency is the
# point of risk-based sizing). A higher-conviction setup is simply more
# likely to actually reach its (fixed) profit target than a
# contradicted one. See opportunity_finder.py for the full reasoning. ---
st.header("🎯 Top Opportunity")
st.caption("Ranked by agreement across your signals - technical score, ML model, LLM agent, and "
           "sentiment - plus real room to the next resistance/support level. Not a profit guarantee.")

@st.cache_data(ttl=60)
def _cached_opportunities(capital_value):
    return find_opportunities(capital_value)

opportunities = _cached_opportunities(capital)

opp_col1, opp_col2 = st.columns(2)
for col, direction, arrow in [(opp_col1, "bullish", "🟢"), (opp_col2, "bearish", "🔴")]:
    with col:
        picks = opportunities[direction]
        if not picks:
            st.info(f"No {direction} candidates right now.")
            continue

        top = picks[0]
        with st.container(border=True):
            st.markdown(f"### {arrow} {top.symbol} — {top.action}")
            st.metric("Conviction score", top.conviction_score,
                      delta=f"{top.conviction_score - top.technical_score:+.1f} from signal agreement"
                      if top.conviction_score != top.technical_score else None)

            if top.agreements:
                for a in top.agreements:
                    st.caption(f"✅ {a}")
            if top.disagreements:
                for d in top.disagreements:
                    st.caption(f"❌ {d}")
            if not top.agreements and not top.disagreements:
                st.caption("Technical score only - no ML/LLM/sentiment signal available yet for this symbol.")

            if top.blocked:
                st.warning(f"🚫 Blocked: {top.block_reason}")
            elif top.suggested_size:
                st.write(f"**{top.suggested_size} shares** @ {top.entry_price} "
                         f"(Rs.{top.suggested_value:,.0f})")
                st.caption(f"Stop {top.stop_loss} · Target {top.take_profit}"
                           + (f" · Room to next level: Rs.{top.room_to_target:.2f} "
                              f"(realistic R:R {top.realistic_reward_risk})" if top.room_to_target else
                              " · No further resistance/support level detected yet"))
            else:
                st.caption("Not enough data to size this trade yet.")

            if st.button(f"Select {top.symbol}", key=f"top_opp_{direction}"):
                st.session_state["selected_symbol"] = top.symbol
                st.rerun()

        if len(picks) > 1:
            with st.expander(f"Next {min(4, len(picks)-1)} {direction} candidates"):
                for o in picks[1:5]:
                    st.write(f"**{o.symbol}**: conviction {o.conviction_score} "
                             f"(technical {o.technical_score})"
                             + (" 🚫 blocked" if o.blocked else ""))

st.divider()

# --- Today's digest, if generated ---
from datetime import date
digest_row = conn.execute("SELECT summary FROM digests WHERE date = ?", (date.today().isoformat(),)).fetchone()
if digest_row:
    with st.expander("📋 Today's Digest", expanded=True):
        st.write(digest_row["summary"])

# --- Position Monitor: dedicated panel for every open position, with
# live P&L (placed value vs current value) and a HOLD/SELL indicator
# per symbol - separate from the per-symbol view below since this
# should stay visible regardless of which symbol you're looking at.
# Reuses position_statuses fetched above (same cache) rather than
# re-querying - it's the same data the sidebar contradiction check
# just used. ---
st.header("📊 Position Monitor")

if not position_statuses:
    st.write("No open positions. Size a trade below and click **Record as open position** to track it here.")
else:
    total_placed_value = sum(s.position["position_value"] for s in position_statuses)
    total_current_value = sum(s.current_value for s in position_statuses)
    total_pnl = sum(s.pnl for s in position_statuses)

    mcol1, mcol2, mcol3 = st.columns(3)
    mcol1.metric("Placed value", f"Rs.{total_placed_value:,.0f}")
    mcol2.metric("Current value", f"Rs.{total_current_value:,.0f}")
    mcol3.metric("Total P&L", f"Rs.{total_pnl:,.0f}", delta=f"{(total_pnl/total_placed_value*100) if total_placed_value else 0:.2f}%")

    for status in position_statuses:
        p = status.position
        pnl_color = "🟢" if status.pnl >= 0 else "🔴"
        indicator = "🟡 HOLD" if status.recommendation == "HOLD" else "🔴 SELL"

        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 1])
            c1.write(f"**{p['symbol']}** ({p['action']})")
            c2.write(f"Placed: Rs.{p['position_value']:,.0f} @ {p['entry_price']}")
            c3.write(f"Now: Rs.{status.current_value:,.0f} @ {status.current_price}")
            c4.write(f"{pnl_color} Rs.{status.pnl:,.0f} ({status.pnl_pct}%)")
            if c5.button("Close", key=f"monitor_close_{p['id']}"):
                _close_position(p["id"])
                st.cache_data.clear()
                st.rerun()
            st.caption(f"{indicator} — {status.reason}")

            # Surface the same bias-vs-P&L contradiction here too, right
            # next to the position it's actually about, not just in the
            # sidebar's flat list.
            bias_entry = scored_by_symbol.get(p["symbol"])
            if bias_entry and bias_entry.get("contradiction"):
                st.warning(f"⚠️ {bias_entry['contradiction']}")

st.divider()

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
        from portfolio_risk import calculate_portfolio_adjusted_position, add_open_position

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
                        st.cache_data.clear()
                        st.rerun()
                st.caption("This is a calculated plan, not an order - nothing is placed automatically.")

        # (Open positions are now shown in the Position Monitor panel at the top of the page)

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