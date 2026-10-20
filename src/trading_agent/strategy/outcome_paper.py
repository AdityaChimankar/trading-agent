"""Prospective shadow-paper evidence. No broker orders or real book mutations."""
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
from filelock import FileLock
from threadpoolctl import threadpool_limits

from trading_agent.core.config import get_settings
from trading_agent.research.trade_outcomes import trade_outcome
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.outcome_contract import (
    FEATURE_COLUMNS, POLICY, SIDES, atomic_json, decisions, eligible_features,
    frame_features, metrics, probabilities,
)
from trading_agent.strategy.train_outcome_models import candidate_path, read_candidate


def connection(path):
    conn = sqlite3.connect(path/"forward_paper.sqlite", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS observations (
        symbol TEXT NOT NULL, timestamp TEXT NOT NULL, recorded_at TEXT NOT NULL,
        entry_due TEXT NOT NULL, side TEXT NOT NULL, artifact_sha256 TEXT NOT NULL,
        snapshot TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT,
        PRIMARY KEY(symbol,timestamp))""")
    return conn


def latest(symbol, now):
    raw = get_parquet_store().read_latest(symbol, "minute", n=20000).copy()
    if raw.empty:
        return raw
    raw["timestamp"] = pd.to_datetime(raw.timestamp, utc=True, format="mixed")
    return frame_features(raw.loc[raw.timestamp <= now-timedelta(minutes=1)])


def settle(conn, bundle, report, now):
    for row in conn.execute("SELECT * FROM observations WHERE state='pending'").fetchall():
        df = latest(row["symbol"], now)
        if df.empty:
            continue
        found = np.flatnonzero(df.timestamp == pd.Timestamp(row["timestamp"]))
        if not len(found):
            # Never silently drop a missing losing outcome from the denominator.
            if now >= datetime.fromisoformat(row["entry_due"])+timedelta(minutes=POLICY.horizon):
                conn.execute("UPDATE observations SET state='invalid' WHERE symbol=? AND timestamp=?",
                             (row["symbol"], row["timestamp"]))
            continue
        i = int(found[0])
        entry_index = i+POLICY.entry_delay_bars
        if entry_index < len(df) and df.timestamp.iloc[entry_index] != pd.Timestamp(row["entry_due"]):
            conn.execute("UPDATE observations SET state='invalid' WHERE symbol=? AND timestamp=?",
                         (row["symbol"], row["timestamp"]))
            continue
        saved = json.loads(row["snapshot"])
        actual = [float(df.iloc[i][c]) for c in ("open", "high", "low", "close", "volume")]
        if actual != saved["ohlcv"] or row["artifact_sha256"] != report["artifact_sha256"]:
            conn.execute("UPDATE observations SET state='invalid' WHERE symbol=? AND timestamp=?",
                         (row["symbol"], row["timestamp"]))
            continue
        # ATR was frozen when the intent was recorded, not recomputed using corrections.
        df.loc[i, "atr"] = saved["atr"]
        outcome = trade_outcome(df, i, row["side"], POLICY)
        if outcome:
            conn.execute("UPDATE observations SET state='settled',outcome=? WHERE symbol=? AND timestamp=?",
                         (json.dumps(outcome, allow_nan=False), row["symbol"], row["timestamp"]))
        elif now >= datetime.fromisoformat(row["entry_due"])+timedelta(minutes=POLICY.horizon):
            conn.execute("UPDATE observations SET state='invalid' WHERE symbol=? AND timestamp=?",
                         (row["symbol"], row["timestamp"]))


def record_observation(conn, bundle, report, symbol, df, now):
    """Reject historical intents and already-open entry bars. Internal clock only in CLI."""
    if df.empty or symbol not in bundle["symbols"]:
        return False
    last = df.iloc[-1]
    stamp = last.timestamp.to_pydatetime()
    entry_due = stamp+timedelta(minutes=POLICY.entry_delay_bars)
    if (stamp <= datetime.fromisoformat(bundle["created_at"]) or
            stamp <= datetime.fromisoformat(bundle["test_end"]) or
            not stamp+timedelta(minutes=1) <= now < entry_due or not eligible_features(df).iloc[-1]):
        return False
    if conn.execute("SELECT 1 FROM observations WHERE symbol=? AND state IN ('pending','invalid')", (symbol,)).fetchone():
        return False
    previous = conn.execute("SELECT outcome FROM observations WHERE symbol=? AND state='settled' ORDER BY timestamp DESC LIMIT 1",
                            (symbol,)).fetchone()
    if previous and pd.Timestamp(json.loads(previous["outcome"])["exit_timestamp"]) > last.timestamp:
        return False
    with threadpool_limits(limits=1):
        p = {side: float(probabilities(bundle["sides"][side], df.tail(1))[0]) for side in SIDES}
    code = int(decisions(np.array([p["BUY"]]), np.array([p["SELL"]]), bundle["thresholds"])[0])
    side = ("HOLD", "BUY", "SELL")[code]
    snapshot = {"features": last[FEATURE_COLUMNS].to_dict(), "probabilities": p, "atr": float(last.atr),
                "ohlcv": [float(last[c]) for c in ("open", "high", "low", "close", "volume")]}
    conn.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,NULL)",
                 (symbol, stamp.isoformat(), now.isoformat(), entry_due.isoformat(), side,
                  report["artifact_sha256"], json.dumps(snapshot, allow_nan=False),
                  "pending" if code else "hold"))
    return True


def paper_report(conn, bundle, report):
    rows = conn.execute("SELECT * FROM observations").fetchall()
    trades, invalid, pending = [], 0, 0
    for row in rows:
        if (row["artifact_sha256"] != report["artifact_sha256"] or
                not datetime.fromisoformat(bundle["created_at"]) < datetime.fromisoformat(row["recorded_at"]) < datetime.fromisoformat(row["entry_due"]) or
                datetime.fromisoformat(row["timestamp"]) <= datetime.fromisoformat(bundle["test_end"])):
            invalid += 1
            continue
        if row["state"] == "settled":
            outcome = json.loads(row["outcome"])
            if (outcome["side"] != row["side"] or outcome["timestamp"] != row["timestamp"] or
                    datetime.fromisoformat(outcome["exit_timestamp"]) < datetime.fromisoformat(row["entry_due"])):
                invalid += 1
                continue
            trades.append(outcome)
        invalid += row["state"] == "invalid"
        pending += row["state"] == "pending"
    sides = {side: metrics([t for t in trades if t["side"] == side], forward=True) for side in SIDES}
    return {"candidate_id": bundle["candidate_id"], "observations": len(rows), "pending": pending,
            "invalid": invalid, "sides": sides, "settled": len(trades),
            "last_exit": {side: max((t["exit_timestamp"] for t in trades if t["side"] == side), default=None) for side in SIDES},
            "passed": not invalid and not pending and all(v["passed"] for v in sides.values()),
            "basis": "Prospectively recorded intents, delayed next-open OHLC fills, assumed costs. Not broker fills or portfolio performance."}


def observe(candidate_id):
    bundle, report = read_candidate(candidate_id)
    path = candidate_path(candidate_id)
    with FileLock(str(path/"paper.lock"), timeout=0):
        conn = connection(path)
        try:
            with conn:
                settle(conn, bundle, report, datetime.now(UTC))
                for symbol in bundle["symbols"]:
                    df = latest(symbol, datetime.now(UTC))
                    record_observation(conn, bundle, report, symbol, df, datetime.now(UTC))
            result = paper_report(conn, bundle, report)
            atomic_json(path/"forward_summary.json", result)
            return result
        finally:
            conn.close()


def promote(candidate_id):
    """No force flag. Both directions must pass historical AND forward gates."""
    bundle, report = read_candidate(candidate_id)
    historical = {side: metrics([t for t in bundle["test_trades"] if t["side"] == side]) for side in SIDES}
    if not all(v["passed"] for v in historical.values()):
        raise ValueError("Promotion blocked: both BUY and SELL must pass historical >60% and expectancy gates")
    path = candidate_path(candidate_id)
    with FileLock(str(path/"paper.lock"), timeout=0):
        conn = connection(path)
        try:
            forward = paper_report(conn, bundle, report)
        finally:
            conn.close()
        if not forward["passed"]:
            raise ValueError("Promotion blocked: forward-paper gates not passed, or pending/invalid outcomes remain")
        now = datetime.now(UTC)
        expires = min(now+timedelta(days=7), *(datetime.fromisoformat(v)+timedelta(days=7) for v in forward["last_exit"].values()))
        if expires <= now:
            raise ValueError("Promotion blocked: forward evidence is stale")
        manifest = {"candidate_id": candidate_id, "artifact_sha256": report["artifact_sha256"],
                    "contract_hash": bundle["contract_hash"], "policy": bundle["policy"],
                    "promoted_at": now.isoformat(), "expires_at": expires.isoformat(),
                    "historical": historical, "forward": forward}
        # Atomic pointer replacement; previous artifacts and legacy model are preserved.
        atomic_json(get_settings().ml.model_dir/"outcome-active.json", manifest)
        return manifest
