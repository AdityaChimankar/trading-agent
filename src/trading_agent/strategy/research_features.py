"""Nine-feature contract recovered from commit 5dcea32, not the new trainer.

The existing ml_model.joblib was trained with rolling ATR, EWM RSI/ADX and
confirmed swing/candle pattern codes. Do not feed it the newer feature schema.
"""
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from trading_agent.research.walk_forward_optimizer import precompute_pattern_bias_codes

FEATURES = ["rsi", "adx", "atr_pct", "pattern_code", "return_1", "return_3",
            "return_6", "volume_ratio", "dist_from_ma20"]


def load_legacy_bundle(path):
    """Trusted local pickle only; reject sklearn version drift.

    The Windows 1.5.2 Cython wheel pickled loss classes as `_loss`. Linux
    exposes the same classes under sklearn._loss._loss. Resolve only this
    known alias, only with the original sklearn release; never rewrite files.
    """
    import sys
    import warnings
    import joblib
    import sklearn
    from sklearn.exceptions import InconsistentVersionWarning
    with warnings.catch_warnings():
        warnings.simplefilter("error", InconsistentVersionWarning)
        try:
            return joblib.load(path)
        except ModuleNotFoundError as error:
            if error.name != "_loss" or sklearn.__version__ != "1.5.2":
                raise
            import sklearn._loss._loss as legacy_loss
            sys.modules.setdefault("_loss", legacy_loss)
            return joblib.load(path)


def prepare_research_frame(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy().reset_index(drop=True)
    delta = df.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    tr = pd.concat([df.high - df.low, (df.high - df.close.shift()).abs(),
                    (df.low - df.close.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    up, down = df.high.diff(), -df.low.diff()
    plus = 100 * (((up > down) & (up > 0)) * up).ewm(alpha=1 / 14, adjust=False).mean() / df.atr.replace(0, 1e-9)
    minus = 100 * (((down > up) & (down > 0)) * down).ewm(alpha=1 / 14, adjust=False).mean() / df.atr.replace(0, 1e-9)
    df["adx"] = (100 * (plus - minus).abs() / (plus + minus).replace(0, 1e-9)).ewm(alpha=1 / 14, adjust=False).mean()
    body, span = (df.close - df.open).abs(), df.high - df.low
    lower = df[["open", "close"]].min(axis=1) - df.low
    upper = df.high - df[["open", "close"]].max(axis=1)
    df["doji"] = (span > 0) & (body / span < 0.1)
    df["hammer"] = (body > 0) & (lower > 2 * body) & (upper < body)
    df["bullish_engulfing"] = (df.close.shift() < df.open.shift()) & (df.close > df.open) & (df.open <= df.close.shift()) & (df.close >= df.open.shift())
    df["bearish_engulfing"] = (df.close.shift() > df.open.shift()) & (df.close < df.open) & (df.open >= df.close.shift()) & (df.close <= df.open.shift())
    swings = {"highs": [(int(i), df.high.iloc[i]) for i in argrelextrema(df.high.to_numpy(), np.greater_equal, order=5)[0]],
              "lows": [(int(i), df.low.iloc[i]) for i in argrelextrema(df.low.to_numpy(), np.less_equal, order=5)[0]]}
    df["pattern_code"] = precompute_pattern_bias_codes({
        "length": len(df), "hammers": df.hammer.to_numpy(),
        "bull_engulf": df.bullish_engulfing.to_numpy(),
        "bear_engulf": df.bearish_engulfing.to_numpy(), "swings": swings,
    })
    df["atr_pct"] = df.atr / df.close
    for lag in (1, 3, 6):
        df[f"return_{lag}"] = df.close.pct_change(lag, fill_method=None)
    df["volume_ratio"] = df.volume / df.volume.rolling(20).mean().replace(0, np.nan)
    df["ma20"], df["ma50"] = df.close.rolling(20).mean(), df.close.rolling(50).mean()
    df["dist_from_ma20"] = (df.close - df.ma20) / df.ma20
    # Intraday trend context for the moving-average profiles. EMAs with adjust=False
    # and a session-cumulative VWAP use only the current and earlier candles.
    df["ema9"] = df.close.ewm(span=9, adjust=False).mean()
    df["ema21"] = df.close.ewm(span=21, adjust=False).mean()
    session = pd.to_datetime(df.timestamp, utc=True).dt.tz_convert("Asia/Kolkata").dt.date
    traded = (df.high + df.low + df.close) / 3 * df.volume
    df["vwap"] = traded.groupby(session).cumsum() / df.volume.groupby(session).cumsum().replace(0, np.nan)
    return df


class ResearchModel:
    """Load only a locally installed, trusted artifact. Never fill missing features."""

    def __init__(self, path):
        from sklearn.exceptions import InconsistentVersionWarning
        self.model = None
        self.status = "missing"
        self.threshold = 0.55
        self.thresholds = {}
        self.interval = "minute"
        self.warning = ""
        if not path.exists():
            return
        try:
            bundle = load_legacy_bundle(path)
            if list(bundle["feature_columns"]) != FEATURES:
                self.status = "incompatible_features"
                return
            self.model = bundle["model"]
            if set(map(int, self.model.classes_)) != {0, 1, 2}:
                self.model = None
                self.status = "incompatible_classes"
                return
            self.threshold = float(bundle.get("confidence_threshold", 0.55))
            self.thresholds = bundle.get("per_symbol_thresholds", {})
            if not 0 <= self.threshold <= 1 or any(not 0 <= float(v) <= 1 for v in self.thresholds.values()):
                raise ValueError("Invalid model threshold")
            self.interval = bundle.get("interval", "minute")
            self.status = "loaded"
            self.warning = "Legacy artifact: training provenance and out-of-sample performance must be revalidated."
            if "interval" not in bundle:
                self.warning += " Minute interval inferred from the original loader; artifact has no interval metadata."
        except InconsistentVersionWarning:
            self.model = None
            self.status = "incompatible_version"
            self.warning = "Model scikit-learn version differs from runtime; install the project's pinned dependencies."
        except Exception:
            self.model = None
            self.status = "load_failed"

    def predict(self, df, symbol, interval):
        result = {"action": None, "confidence": None, "status": self.status}
        if self.model is None:
            return result
        if interval != self.interval:
            return {**result, "status": "interval_mismatch"}
        features = df[FEATURES].tail(1)
        if not np.isfinite(features.to_numpy(dtype=float)).all():
            return {**result, "status": "invalid_features"}
        from threadpoolctl import threadpool_limits
        with threadpool_limits(limits=1):
            probabilities = np.asarray(self.model.predict_proba(features)[0], dtype=float)
        if len(probabilities) != 3 or not np.isfinite(probabilities).all() or (probabilities < 0).any() or not np.isclose(probabilities.sum(), 1):
            return {**result, "status": "invalid_prediction"}
        index = int(probabilities.argmax())
        action = {0: "HOLD", 1: "BUY", 2: "SELL"}[int(self.model.classes_[index])]
        confidence = float(probabilities[index])
        threshold = float(self.thresholds.get(symbol, self.threshold))
        return {"action": action if confidence >= threshold else "HOLD", "raw_action": action,
                "confidence": confidence, "threshold": threshold, "status": "available"}
