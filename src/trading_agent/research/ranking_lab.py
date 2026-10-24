"""Ranking lab: pre-registered variants of the cross-sectional approach, selected honestly.

Single-stock timing showed no edge after costs (``strategy_search``), while the
cross-sectional ranker showed a weak, unstable one. This lab tries that idea's
neighbours on the Nifty 500 minute history:

- rule ranks with a sign fixed in advance (nothing is fitted): overnight-gap
  reversal and momentum, previous-day and 5-day reversal, opening-drive
  (09:15-09:45) momentum and reversal, and drive scaled by the opening volume shock;
- model ranks, walk-forward (train on the preceding ``train_days`` sessions, refit
  every ``retrain_days``): random forest, logistic regression and gradient
  boosting, on the paper's lag features and on lags plus gap, drive and volume shock;
- entries at the 09:15 open or the 09:45 bar, exits at 10:15, 12:00 or 15:15;
- books of the top and bottom 5, 10 or 20.

Each variant is a long/short book, net of ``cost_bps`` per position. A random
book from the same names has an expected gross return of exactly zero, so its
expected net is ``-cost_bps``; beating it means a positive gross return.

Every variant is scored on the same test days, those after the longest warm-up.
They are split by date into validation (first ``validation_share``) and holdout.
Selection sees validation only: at least ``min_days`` days, a positive mean, and
a daily t-stat above the Bonferroni bar for the number of variants. The
shortlist, the validation-best and the reference (the paper trial's frozen
config) are written to a hashed run file before any holdout statistic is
computed, and holdout statistics are reported for that frozen set only. A
rolling choose-best (trailing ``wf_train`` test days, applied to the next
``wf_test``) measures the selection procedure itself.

Win rates are reported per day (book positive) and per position. Variants are
ranked by mean net return: a high win rate at negative expectancy loses money.
Research only; nothing here places orders or changes the live rule.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from trading_agent.core.config import get_settings
from trading_agent.core.logging_config import get_logger
from trading_agent.research.cross_sectional import CRORE, IST, default_universe
from trading_agent.research.strategy_search import _write_atomic, bonferroni_t, git_revision, versions

logger = get_logger(__name__)

OPEN = 9 * 60 + 15
DRIVE_END = 9 * 60 + 45
EXITS = {"1015": 10 * 60 + 15, "1200": 12 * 60, "1515": 15 * 60 + 15}
LAGS = tuple(range(1, 11))
OPEN_FEATURES = ("gap", "drive", "vshock")


@dataclass(frozen=True)
class LabConfig:
    cost_bps: float = 15.0          # round trip per position, incl. slippage (as the trial)
    min_avg_value_cr: float = 10.0  # 20-day mean traded value, known before the open
    train_days: int = 60
    retrain_days: int = 20
    validation_share: float = 0.6
    min_days: int = 40
    family_wise_alpha: float = 0.05
    wf_train: int = 60
    wf_test: int = 20
    seed: int = 9


@dataclass(frozen=True)
class Variant:
    name: str
    method: str          # "rule" or a model: rf | logit | hgb
    entry: str           # "0915" or "0945"
    exit: str            # key of EXITS
    k: int
    score: str = ""      # rule: column name; sign is part of the column
    features: str = ""   # model: "lags" or "lags+open"

    @property
    def key(self) -> str:
        return f"{self.name}|{self.entry}-{self.exit}|k{self.k}"


RULES = (
    ("gap_reversal", "neg_gap", "0915", ("1015", "1200", "1515")),
    ("gap_momentum", "gap", "0915", ("1015", "1200", "1515")),
    ("prev_day_reversal", "neg_IntraR1", "0915", ("1515",)),
    ("week_reversal", "neg_CloseR5", "0915", ("1515",)),
    ("drive_momentum", "drive", "0945", ("1200", "1515")),
    ("drive_reversal", "neg_drive", "0945", ("1200", "1515")),
    ("drive_volume_momentum", "drive_x_vshock", "0945", ("1200", "1515")),
)
MODELS = (
    ("rf_lags", "rf", "lags", "0915"),
    ("logit_lags", "logit", "lags", "0915"),
    ("hgb_lags", "hgb", "lags", "0915"),
    ("rf_open", "rf", "lags+open", "0945"),
    ("logit_open", "logit", "lags+open", "0945"),
    ("hgb_open", "hgb", "lags+open", "0945"),
)
BOOK_SIZES = (5, 10, 20)
REFERENCE = "rf_lags|0915-1515|k10"  # the paper trial's frozen configuration


def variants() -> list[Variant]:
    out = [Variant(name, "rule", entry, exit_, k, score=score)
           for name, score, entry, exits in RULES for exit_ in exits for k in BOOK_SIZES]
    out += [Variant(name, method, entry, "1515", k, features=features)
            for name, method, features, entry in MODELS for k in BOOK_SIZES]
    return out


# --- data ------------------------------------------------------------------------------------

def snapshots(intraday: pd.DataFrame) -> pd.DataFrame:
    """Per IST session: 09:15 open, 09:44 close, 09:45 entry open, exit closes, opening volume, value.

    A price is used only if its bar exists within five minutes of the time it stands for.
    """
    columns = ["open", "p0945", "e0945", *[f"x{k}" for k in EXITS], "vol_open", "value"]
    if intraday is None or intraday.empty:
        return pd.DataFrame(columns=columns)
    stamp = pd.to_datetime(intraday["timestamp"], utc=True).dt.tz_convert(IST)
    frame = intraday.assign(session=stamp.dt.date, minute=stamp.dt.hour * 60 + stamp.dt.minute).sort_values("timestamp")

    def first_open(lo, hi):
        part = frame[(frame["minute"] >= lo) & (frame["minute"] < hi)]
        return part.groupby("session")["open"].first()

    def last_close(before):
        part = frame[(frame["minute"] < before) & (frame["minute"] >= before - 5)]
        return part.groupby("session")["close"].last()

    out = pd.DataFrame({
        "open": first_open(OPEN, OPEN + 1), "p0945": last_close(DRIVE_END),
        "e0945": first_open(DRIVE_END, DRIVE_END + 5),
        **{f"x{k}": last_close(m) for k, m in EXITS.items()},
        "vol_open": frame[frame["minute"] < DRIVE_END].groupby("session")["volume"].sum(),
        "value": (frame["close"] * frame["volume"]).groupby(frame["session"]).sum(),
    })
    return out[columns].dropna(subset=["open"])


def load_snapshots(symbols: list[str], store=None, interval: str = "minute") -> dict[str, pd.DataFrame]:
    """Wide (session x symbol) frames, one per snapshot column."""
    from trading_agent.storage.parquet_store import get_parquet_store

    store = store or get_parquet_store()
    parts = {}
    for symbol in symbols:
        try:
            snap = snapshots(store.read_candles(symbol, interval))
        except Exception as error:  # one unreadable symbol must not stop the lab
            logger.warning("ranking_lab_symbol_skipped", symbol=symbol, error=str(error))
            continue
        if len(snap):
            parts[symbol] = snap
    if not parts:
        return {}
    long = pd.concat(parts, names=["symbol", "session"])
    return {c: long[c].unstack("symbol").sort_index() for c in long.columns}


def build_table(snap: dict[str, pd.DataFrame], config: LabConfig) -> pd.DataFrame:
    """Long (session, symbol) table: features, signed rule scores, target returns, liquidity."""
    o, x = snap["open"], snap["x1515"].reindex_like(snap["open"])
    cols = {}
    intraday = x / o - 1
    for k in LAGS:
        cols[f"IntraR{k}"] = intraday.shift(k)
        cols[f"CloseR{k}"] = x.pct_change(k, fill_method=None).shift(1)
        cols[f"OverNR{k}"] = o / x.shift(k) - 1
    cols["gap"] = o / x.shift(1) - 1
    cols["drive"] = snap["p0945"].reindex_like(o) / o - 1
    vol = snap["vol_open"].reindex_like(o)
    cols["vshock"] = vol / vol.rolling(20, min_periods=10).mean().shift(1)
    value = snap["value"].reindex_like(o)
    cols["liquid"] = (value.rolling(20, min_periods=10).mean().shift(1) >= config.min_avg_value_cr * CRORE)
    entries = {"0915": o, "0945": snap["e0945"].reindex_like(o)}
    for entry, price in entries.items():
        for exit_ in EXITS:
            cols[f"ret_{entry}_{exit_}"] = snap[f"x{exit_}"].reindex_like(o) / price - 1
    table = pd.concat({name: frame.stack(future_stack=True) for name, frame in cols.items()}, axis=1)
    table.index.names = ["session", "symbol"]
    table = table[table["liquid"].fillna(False).astype(bool)].drop(columns="liquid")
    table["neg_gap"], table["neg_drive"] = -table["gap"], -table["drive"]
    table["neg_IntraR1"], table["neg_CloseR5"] = -table["IntraR1"], -table["CloseR5"]
    table["drive_x_vshock"] = table["drive"] * table["vshock"].clip(0, 5)
    return table


def feature_names(kind: str) -> list[str]:
    lags = [f"{p}{k}" for k in LAGS for p in ("IntraR", "CloseR", "OverNR")]
    return lags + (list(OPEN_FEATURES) if kind == "lags+open" else [])


# --- scoring ---------------------------------------------------------------------------------

def _model(method: str, seed: int):
    if method == "rf":
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(n_estimators=300, max_depth=10, min_samples_leaf=5, random_state=seed, n_jobs=-1)
    if method == "logit":
        from sklearn.linear_model import LogisticRegression

        return LogisticRegression(C=1.0, max_iter=1000)
    if method == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                              min_samples_leaf=100, l2_regularization=1.0, random_state=seed)
    raise ValueError(method)


def scored_sessions(table: pd.DataFrame, config: LabConfig) -> list:
    """Sessions after the longest warm-up: lag features complete and ``train_days`` sessions before."""
    complete = table.dropna(subset=feature_names("lags"))
    sessions = sorted(complete.index.get_level_values("session").unique())
    return sessions[config.train_days:]


def model_folds(history: list, tested: list, config: LabConfig) -> list[tuple[list, list]]:
    """(training sessions, scored block) pairs: the last ``train_days`` sessions strictly before each block."""
    folds = []
    for start in range(0, len(tested), config.retrain_days):
        block = tested[start:start + config.retrain_days]
        train = [d for d in history if d < block[0]][-config.train_days:]
        if len(train) == config.train_days:
            folds.append((train, block))
    return folds


def model_scores(table: pd.DataFrame, method: str, features: str, target: str, config: LabConfig,
                 tested: list) -> pd.Series:
    """Walk-forward probability of beating the session median of ``target``; trained on earlier sessions only."""
    names = feature_names(features)
    data = table.dropna(subset=names + [target])
    if method != "rf":  # per-session ranks: comparable across days for the linear and boosted models
        data = data.copy()
        data[names] = data.groupby(level="session")[names].rank(pct=True) - 0.5
    median = data.groupby(level="session")[target].transform("median")
    label = (data[target] > median).astype(int)
    session = data.index.get_level_values("session")
    scores = []
    for train_days, block in model_folds(sorted(session.unique()), tested, config):
        train = session.isin(train_days)
        if label[train].nunique() < 2:
            continue
        model = _model(method, config.seed).fit(data.loc[train, names].to_numpy(), label[train].to_numpy())
        rows = session.isin(block)
        if rows.any():
            scores.append(pd.Series(model.predict_proba(data.loc[rows, names].to_numpy())[:, 1],
                                    index=data.index[rows]))
    return pd.concat(scores) if scores else pd.Series(dtype=float)


def book(frame: pd.DataFrame, k: int, cost: float) -> pd.DataFrame:
    """Daily long/short book on columns ``score`` and ``ret``: net, gross and position wins."""
    rows = []
    for session, day in frame.groupby(level="session"):
        if len(day) < 2 * k:
            continue
        order = np.argsort(day["score"].to_numpy(), kind="stable")
        ret = day["ret"].to_numpy()
        longs, shorts = ret[order[-k:]], -ret[order[:k]]
        gross = 0.5 * longs.mean() + 0.5 * shorts.mean()
        rows.append((session, gross - cost, gross, float(np.mean(np.r_[longs, shorts] > cost))))
    return pd.DataFrame(rows, columns=["session", "net", "gross", "position_win"]).set_index("session")


def daily_series(table: pd.DataFrame, config: LabConfig, only: set[str] | None = None) -> dict[str, pd.DataFrame]:
    """variant key -> daily book on the common test days."""
    tested = scored_sessions(table, config)
    on_test = table[table.index.get_level_values("session").isin(tested)]
    cost = config.cost_bps / 1e4
    out = {}
    fitted: dict[tuple, pd.Series] = {}
    for v in variants():
        if only is not None and v.key not in only:
            continue
        target = f"ret_{v.entry}_{v.exit}"
        if v.method == "rule":
            frame = on_test[[v.score, target]].rename(columns={v.score: "score", target: "ret"}).dropna()
        else:
            spec = (v.method, v.features, target)
            if spec not in fitted:
                fitted[spec] = model_scores(table, v.method, v.features, target, config, tested)
                logger.info("ranking_lab_model_scored", model=v.name, rows=len(fitted[spec]))
            frame = pd.DataFrame({"score": fitted[spec], "ret": table[target]}).dropna()
        out[v.key] = book(frame, v.k, cost)
    return out


# --- statistics and selection ------------------------------------------------------------------

def stats(daily: pd.DataFrame) -> dict:
    if daily is None or not len(daily):
        return {"days": 0}
    net = daily["net"].to_numpy()
    std = float(net.std(ddof=1)) if len(net) > 1 else 0.0
    return {
        "days": int(len(net)), "mean_net_bps": round(float(net.mean()) * 1e4, 3),
        "mean_gross_bps": round(float(daily["gross"].mean()) * 1e4, 3),
        "t_stat": round(float(net.mean() / (std / math.sqrt(len(net)))), 3) if std > 0 else None,
        "day_win_rate_pct": round(float((net > 0).mean()) * 100, 2),
        "position_win_rate_pct": round(float(daily["position_win"].mean()) * 100, 2),
        "annualised_sharpe": round(float(net.mean() / std * math.sqrt(252)), 3) if std > 0 else None,
        "total_net_pct_points": round(float(net.sum()) * 100, 3),
    }


def split(tested: list, share: float) -> tuple[set, set]:
    cut = int(len(tested) * share)
    return set(tested[:cut]), set(tested[cut:])


def select(series: dict[str, pd.DataFrame], validation: set, config: LabConfig) -> dict:
    """Validation-only statistics, gates and the shortlist."""
    t_bar = bonferroni_t(len(series), config.family_wise_alpha)
    rows = {}
    for key, daily in series.items():
        s = stats(daily[daily.index.isin(validation)])
        checks = {"enough_days": s["days"] >= config.min_days, "positive_after_costs": s.get("mean_net_bps", -1) > 0,
                  "t_above_bonferroni": (s.get("t_stat") or 0) >= t_bar}
        rows[key] = {"validation": s, "checks": checks}
    eligible = [k for k, r in rows.items() if r["checks"]["enough_days"]]
    best = max(eligible, key=lambda k: rows[k]["validation"]["mean_net_bps"]) if eligible else None
    shortlist = sorted((k for k, r in rows.items() if all(r["checks"].values())),
                       key=lambda k: -rows[k]["validation"]["mean_net_bps"])
    return {"rows": rows, "shortlist": shortlist, "validation_best": best, "bonferroni_t": round(t_bar, 3),
            "variants_tried": len(series)}


def rolling_choice(series: dict[str, pd.DataFrame], tested: list, config: LabConfig) -> dict:
    """Pick the best variant on each trailing window of test days; chain the next window's result."""
    nets = pd.DataFrame({k: d["net"] for k, d in series.items()}).reindex(tested)
    chained, folds = [], []
    for start in range(config.wf_train, len(tested), config.wf_test):
        train, test = tested[start - config.wf_train:start], tested[start:start + config.wf_test]
        if len(test) < config.wf_test // 2:
            break
        trailing = nets.loc[train].dropna(axis=1, thresh=config.wf_train // 2).mean()
        if trailing.empty:
            continue
        choice = trailing.idxmax()
        result = nets.loc[test, choice].dropna()
        chained.append(result)
        folds.append({"test_start": str(test[0]), "chosen": choice, "train_mean_bps": round(trailing.max() * 1e4, 3),
                      "test_mean_bps": round(float(result.mean()) * 1e4, 3) if len(result) else None})
    if not chained:
        return {"folds": [], "out_of_sample": {"days": 0}}
    net = pd.concat(chained)
    frame = pd.DataFrame({"net": net, "gross": net + config.cost_bps / 1e4, "position_win": np.nan})
    out = stats(frame)
    out.pop("position_win_rate_pct", None)
    return {"folds": folds, "out_of_sample": out}


# --- run ----------------------------------------------------------------------------------------

def runs_dir() -> Path:
    return get_settings().data_dir / "research" / "ranking_lab" / "runs"


def run(symbols: list[str] | None = None, config: LabConfig = LabConfig(), store=None, snap=None) -> dict:
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    directory = runs_dir() / run_id
    symbols = symbols or default_universe()
    snap = snap if snap is not None else load_snapshots(symbols, store)
    table = build_table(snap, config)
    tested = scored_sessions(table, config)
    if len(tested) < 2 * config.min_days:
        raise ValueError(f"{len(tested)} test sessions; need at least {2 * config.min_days}")
    validation, holdout = split(tested, config.validation_share)
    series = daily_series(table, config)
    selection = select(series, validation, config)
    meta = {"run_id": run_id, "started_at": datetime.now(UTC).isoformat(), "git_revision": git_revision(),
            "versions": versions(), "config": asdict(config), "symbols_requested": len(symbols),
            "symbols_with_data": int(snap["open"].shape[1]), "test_days": len(tested),
            "validation": [str(min(validation)), str(max(validation))],
            "holdout": [str(min(holdout)), str(max(holdout))], "reference": REFERENCE}
    frozen_keys = list(dict.fromkeys([*selection["shortlist"], *filter(None, [selection["validation_best"]]),
                                      REFERENCE]))
    frozen = {**meta, "phase": "selection_frozen", "shortlist": selection["shortlist"],
              "validation_best": selection["validation_best"], "holdout_will_report": frozen_keys,
              "bonferroni_t": selection["bonferroni_t"], "variants_tried": selection["variants_tried"],
              "validation_table": {k: r["validation"] for k, r in selection["rows"].items()}}
    frozen_sha = _write_atomic(directory / "selection.json", frozen, exclusive=True)
    holdout_stats = {k: stats(series[k][series[k].index.isin(holdout)]) for k in frozen_keys if k in series}
    ranked = sorted(selection["rows"], key=lambda k: -selection["rows"][k]["validation"].get("mean_net_bps", -1e9))
    report = {
        **meta, "phase": "complete", "selection_sha256": frozen_sha, "scope": "research_only",
        "bonferroni_t": selection["bonferroni_t"], "variants_tried": selection["variants_tried"],
        "shortlist": selection["shortlist"], "validation_best": selection["validation_best"],
        "validation_top": [{"variant": k, **selection["rows"][k]} for k in ranked[:15]],
        "highest_validation_win_rate": sorted(
            ({"variant": k, **selection["rows"][k]["validation"]} for k in ranked
             if selection["rows"][k]["validation"]["days"] >= config.min_days),
            key=lambda r: -r["day_win_rate_pct"])[:5],
        "holdout": holdout_stats,
        "rolling_choice": rolling_choice(series, tested, config),
        "notes": ("Holdout statistics are reported only for the frozen set. Every historical window before "
                  "this run had been inspected for the reference ranker, so only the paper trial is clean "
                  "evidence for it. A variant must also pass a prospective trial before any use."),
    }
    _write_atomic(directory / "report.json", report, exclusive=True)
    _write_atomic(runs_dir().parent / "latest.json", {"run_id": run_id, "report": str(directory / "report.json")})
    return report


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ranking lab: pre-registered cross-sectional variants (research only)")
    parser.add_argument("symbols", nargs="*", help="Default: symbols_nifty500_plus.txt")
    parser.add_argument("--snapshots", type=Path, help="Cache file for session snapshots (parquet)")
    args = parser.parse_args(argv)
    symbols = [s.upper() for s in args.symbols] or default_universe()
    snap = None
    if args.snapshots and args.snapshots.exists():
        long = pd.read_parquet(args.snapshots)
        snap = {c: long[c].unstack("symbol").sort_index() for c in long.columns}
    elif args.snapshots:
        snap = load_snapshots(symbols)
        pd.concat({c: f.stack(future_stack=True) for c, f in snap.items()}, axis=1).rename_axis(
            ["session", "symbol"]).to_parquet(args.snapshots)
    report = run(symbols, snap=snap)
    print(json.dumps({k: v for k, v in report.items() if k not in ("versions",)}, indent=1, default=str))


if __name__ == "__main__":
    main()
