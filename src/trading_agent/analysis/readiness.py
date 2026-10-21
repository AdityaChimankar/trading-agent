"""Bounded local operational evidence, not a certificate of live-trading readiness."""
from collections import Counter
from datetime import UTC, datetime
import copy
import math

from trading_agent.analysis.research_engine import universe, refresh_eligibility
from trading_agent.analysis.research_jobs import get_research_jobs
from trading_agent.analysis.outcome_validation import estimate
from trading_agent.core.config import get_settings
from trading_agent.core.freshness import ingest_heartbeat
from trading_agent.risk.ledger_guard import read_ledger_state
from trading_agent.storage.parquet_store import get_parquet_store
from trading_agent.strategy.outcome_model import candidate_status


def readiness(interval="minute", now=None):
    now = now or datetime.now(UTC)
    checks = []

    def check(code, severity, title, detail, action):
        checks.append({"code": code, "severity": severity, "title": title, "detail": detail, "action": action})

    settings = get_settings()
    try:
        configured = set(universe())
        store = get_parquet_store()
        stored = set(store.get_symbols())
        present = sum(interval in store.get_intervals(symbol) for symbol in configured & stored)
    except Exception:
        configured, present = set(), 0
        check("storage_unavailable", "blocker", "Stored-data coverage unavailable",
              "Check the configured symbol file, storage mount and permissions. No market download was attempted.", "data")
    if not configured:
        check("empty_universe", "blocker", "No configured universe",
              "Configure symbols.txt and ingest the required interval before scanning.", "data")
    elif present < len(configured):
        check("missing_interval", "warning", "Some symbols have no interval directory",
              f"{len(configured) - present} configured symbols lack {interval} storage. Directory presence is not verified candle history.", "data")
    jobs = get_research_jobs()
    # Avoid rerunning expensive per-symbol model validation merely to show health.
    with jobs.lock:
        scan = copy.deepcopy(jobs.state)
    rows = scan.get("rows", [])
    for row in rows:
        try:
            refresh_eligibility(row, now)
        except (ValueError, TypeError, KeyError):
            row["stale"] = True
            row["plan"] = {"blocked": True, "block_reason": "Invalid scan snapshot"}
    scanned_interval = scan.get("options", {}).get("interval")
    observed = {row.get("symbol") for row in rows}
    observed.update(item.get("symbol") for key in ("skipped", "errors") for item in scan.get(key, []))
    observed.discard(None)
    full = (bool(configured) and observed == configured and scan.get("status") == "completed"
            and scan.get("processed") == scan.get("total") == len(configured))
    if scan.get("status") != "completed" or scanned_interval != interval:
        check("scan_needed", "warning", "No completed scan for this interval",
              "Run an explicit full-universe scan with your shared settings. Partial, cancelled and other-interval runs are not current rankings.", "find")
    elif not full:
        check("partial_universe", "warning", "Latest scan does not cover the configured universe",
              "The attempted symbol set differs from today's configured list; inspect skips and re-scan.", "find")
    fresh = sum(not row.get("stale", True) for row in rows)
    if rows and fresh == 0:
        check("stale_scan", "warning", "All evaluated scan candles are expired",
              "Stored historical research is still available. Fresh actionable plans require completed current candles and a new scan.", "data")
    counts = Counter()
    for key in ("errors", "skipped"):
        for item in scan.get(key, []):
            counts[f"{key}: {item.get('reason', 'unknown')}"] += 1
    if counts:
        reasons = "; ".join(f"{reason} ({count})" for reason, count in counts.most_common(4))
        check("scan_exclusions", "warning", "Scan exclusions need review", reasons, "find")
    paper = {"entries_allowed": None, "block_reason": "Paper risk evidence unavailable",
             "realized_equity": None, "daily_realized_pnl": None, "realized_drawdown_pct": None,
             "accounting_basis": "realized_gross_entry_cost"}
    fingerprint_matches = False
    try:
        ledger = read_ledger_state(now)
        paper.update({key: ledger[key] for key in paper})
        fingerprint_matches = scan.get("ledger_fingerprint") == ledger["fingerprint"]
        if not ledger["entries_allowed"]:
            check("paper_entry_blocked", "blocker", "New paper entries blocked", ledger["block_reason"], "paper")
        if rows and not fingerprint_matches:
            check("ledger_changed", "warning", "Scan allocations need recomputing",
                  "The ledger, session date or risk limits differ from the scan. Re-scan before reviewing allocations.", "find")
    except Exception:
        check("ledger_unavailable", "blocker", "Paper ledger cannot be verified",
              "Check database availability and closed-trade timestamps/P&L. New allocations must remain blocked.", "paper")
    try:
        heartbeat = ingest_heartbeat() or {}
        age = heartbeat.get("heartbeat_age_sec")
        connected = heartbeat.get("connected") is True and age is not None and math.isfinite(float(age)) and 0 <= float(age) <= 120
        feed = {"connected": connected, "last_tick": heartbeat.get("last_tick"),
                "heartbeat_age_sec": float(age) if age is not None and math.isfinite(float(age)) else None}
    except Exception:
        feed = {"connected": False, "last_tick": None, "heartbeat_age_sec": None}
    if not feed["connected"]:
        check("feed_unconfirmed", "warning", "Live feed not confirmed",
              "Check ticker status during market sessions. A healthy heartbeat alone does not prove every symbol has fresh candles.", "data")
    model = {"status": "unavailable", "active_candidate_id": None, "latest_candidate_status": None}
    try:
        outcome = candidate_status()
        active = outcome.get("active")
        model.update(status="invalid" if outcome.get("error") else "promoted_record" if active else "exploratory",
                     active_candidate_id=active.get("candidate_id") if active else None,
                     latest_candidate_status=next(iter(outcome.get("candidates", [])), {}).get("status"))
    except Exception:
        pass
    if model["status"] != "promoted_record":
        check("ml_unqualified", "warning", "No verified promoted-ML record available",
              "Legacy ML and hybrid scores are exploratory. Review historical and prospective paper gates; rules need their own qualified outcome evidence.", "validate")
    else:
        check("ml_scoped", "info", "Promoted-ML record is present",
              "This overview reads promotion metadata only. Inference independently verifies artifact integrity, expiry and trained scope.", "validate")
    if settings.api.workers != 1:
        check("unsupported_workers", "blocker", "Unsupported multi-worker research state",
              "Use one API worker. Multi-user/job-queue coordination has not been implemented or certified.", "data")
    check("paper_boundary", "info", "Research and paper execution only",
          "No broker-order lifecycle, reconciliation or live-execution certification is implemented.", "paper")
    eligible = 0
    if fingerprint_matches and paper["entries_allowed"] and full and scanned_interval == interval:
        options = scan.get("options", {})
        for row in rows:
            plan = row.get("plan", {})
            if plan.get("blocked", True) or plan.get("quantity", 0) <= 0 or row.get("stale", True):
                continue
            if options.get("require_validated", True):
                evidence = estimate(row, options.get("mode"), options.get("reward_risk", 2), now)
                if evidence["status"] != "qualified_research" or (evidence.get("expected_net_return_pct") or 0) <= 0:
                    continue
            eligible += 1
    return {
        "generated_at": now.isoformat(), "execution_mode": "paper_only", "live_trading_ready": False,
        "status": "attention" if any(c["severity"] != "info" for c in checks) else "research_only",
        "interval": interval,
        "coverage": {"configured": len(configured), "stored_for_interval": present,
                     "missing_for_interval": max(0, len(configured) - present),
                     "scan_status": scan.get("status", "idle"), "scan_interval": scanned_interval,
                     "scan_processed": scan.get("processed", 0), "scan_total": scan.get("total", 0),
                     "evaluated": len(rows), "fresh": fresh, "stale": len(rows) - fresh,
                     "eligible": eligible,
                     "scan_completed_at": scan.get("completed_at"), "full_universe": full},
        "feed": feed, "model": model, "paper": paper, "checks": checks,
        "limitations": [
            "Storage coverage counts interval directories, not valid/fresh candles or point-in-time listings.",
            "Scan counts describe the latest snapshot and its interval, not streaming all-symbol surveillance; eligibility still requires fresh evidence.",
            "Paper risk uses realized gross equity only; unrealized losses, actual fees, taxes, slippage, sector and correlation exposure remain incomplete.",
            "Weekday/session checks are not an authoritative exchange holiday or special-session calendar.",
            "Single-worker local deployment; broker reconciliation, durable task orchestration, access roles and disaster-recovery certification remain outstanding.",
        ],
    }
