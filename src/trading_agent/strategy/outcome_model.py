"""Dashboard adapter for explicitly promoted outcome candidates."""
import json
from datetime import UTC, datetime, timedelta

import numpy as np
from threadpoolctl import threadpool_limits

from trading_agent.core.config import get_settings
from trading_agent.strategy.outcome_contract import (
    SIDES, decisions, eligible_features, frame_features, probabilities, metrics, contract_hash,
)
from trading_agent.strategy.train_outcome_models import read_candidate, candidate_root


def active_manifest():
    path = get_settings().ml.model_dir/"outcome-active.json"
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (manifest["contract_hash"] != contract_hash() or
            datetime.fromisoformat(manifest["expires_at"]) <= datetime.now(UTC)):
        raise ValueError("Promoted outcome model contract/evidence expired")
    if not manifest["forward"]["passed"] or not all(manifest["historical"][s]["passed"] and manifest["forward"]["sides"][s]["passed"] for s in SIDES):
        raise ValueError("Promotion evidence missing")
    return manifest


class OutcomeResearchModel:
    def __init__(self):
        self.status, self.warning, self.bundle = "outcome_unavailable", "", None
        try:
            self.manifest = active_manifest()
            if self.manifest is None:
                return
            bundle, report = read_candidate(self.manifest["candidate_id"])
            if report["artifact_sha256"] != self.manifest["artifact_sha256"]:
                raise ValueError("Promoted artifact was changed")
            if not all(metrics([t for t in bundle["test_trades"] if t["side"] == side])["passed"] for side in SIDES):
                raise ValueError("Historical outcome evidence does not pass")
            self.bundle = bundle
            self.status = "outcome_loaded"
            self.warning = "Promoted BUY/SELL outcome models. Evidence applies to ML-only mode, trained symbols, minute bars and the frozen trade policy; not guaranteed winners."
        except Exception:
            self.warning = "Outcome model unavailable or invalid; no silent fallback to legacy predictions."

    def predict(self, df, symbol, interval):
        unavailable = {"action": None, "confidence": None, "status": self.status}
        if self.bundle is None:
            return unavailable
        if datetime.fromisoformat(self.manifest["expires_at"]) <= datetime.now(UTC):
            return {**unavailable, "status": "outcome_evidence_expired"}
        if interval != "minute" or symbol not in self.bundle["symbols"]:
            return {**unavailable, "status": "outside_outcome_training_scope"}
        data = frame_features(df)
        if not eligible_features(data).iloc[-1]:
            return {**unavailable, "status": "invalid_outcome_features_or_session"}
        if data.timestamp.iloc[-1] <= datetime.fromisoformat(self.bundle["test_end"]):
            return {**unavailable, "status": "candle_overlaps_training_evaluation"}
        if datetime.now(UTC) >= data.timestamp.iloc[-1].to_pydatetime() + timedelta(minutes=2):
            return {**unavailable, "status": "outcome_entry_window_expired"}
        with threadpool_limits(limits=1):
            p = {side: float(probabilities(self.bundle["sides"][side], data.tail(1))[0]) for side in SIDES}
        code = int(decisions(np.array([p["BUY"]]), np.array([p["SELL"]]), self.bundle["thresholds"])[0])
        action = ("HOLD", "BUY", "SELL")[code]
        expected = None
        if code:
            side_model = self.bundle["sides"][action]
            expected = 100*(p[action]*side_model["mean_win"]+(1-p[action])*side_model["mean_loss"])
        return {"action": action, "confidence": p[action] if code else 0., "status": "available",
                "model_kind": "outcome", "candidate_id": self.bundle["candidate_id"],
                "probabilities": p, "policy": self.bundle["policy"],
                "expected_net_return_pct": expected,
                "thresholds": self.bundle["thresholds"], "expires_at": self.manifest["expires_at"]}


def candidate_status():
    root = candidate_root()
    reports = []
    if root.exists():
        for path in sorted(root.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                reports.append({k: report.get(k) for k in ("candidate_id", "created_at", "status", "symbols", "test", "tuning", "windows", "error")})
            except (OSError, ValueError):
                continue
    try:
        active = active_manifest()
        active = None if active is None else {k: active[k] for k in ("candidate_id", "promoted_at", "expires_at")}
        error = None
    except (ValueError, KeyError, TypeError, OSError):
        active, error = None, "Active outcome evidence is invalid or expired"
    return {"active": active, "error": error, "candidates": reports,
            "required": "Each side: >60% wins, both 95% lower win bounds >60%, positive lower net expectancy; historical 100 trades/10 sessions, forward paper 100 trades/20 sessions."}
