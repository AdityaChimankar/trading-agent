"""Offline browser integration test for the coordinated research workspace.

Run after building frontend: python tests/dashboard_workflow_smoke.py
All API requests are intercepted. No real training, news, book or orders.
"""
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def exercise(page, base_url, screenshots=None):
    posts = []
    waiting = {}
    failures = []
    now = datetime.now(UTC)
    options = {"capital": 100000, "interval": "5minute", "mode": "rule_only",
               "risk_pct": 1, "reward_risk": 3, "require_validated": True}
    row = {
        "symbol": "TCS", "interval": "5minute", "timestamp": now.isoformat(),
        "price": 100, "atr": 1, "rsi": 55, "adx": 25, "action": "BUY",
        "score": 75, "confidence": .7, "reasoning": "Offline signal",
        "stale": False, "age_minutes": 0, "patterns": [], "pattern_bias": "bullish",
        "agreements": [], "disagreements": [],
        "rule": {"action": "BUY", "confidence": .7},
        "ml": {"action": None, "confidence": None, "status": "unavailable"},
        "sentiment": None, "expires_at": (now + timedelta(hours=1)).isoformat(),
        "validation": {"status": "qualified_research", "win_probability": .65,
                       "expected_net_return_pct": .1, "reason": "Offline fixture only"},
        "plan": {"entry_price": 100, "stop_loss": 98.5, "take_profit": 104.5,
                 "quantity": 10, "position_value": 1000, "risk_amount": 15,
                 "reward_risk": 3, "blocked": False, "block_reason": None,
                 "room_reward_risk": 4},
    }
    scan = {"status": "idle", "total": 0, "processed": 0, "rows": [], "errors": [], "skipped": []}
    news = {"status": "cached", "articles": [{"headline": "Offline fixture: company publishes quarterly update",
            "url": "https://example.invalid/news", "source": "Offline fixture", "published_at": now.isoformat()}], "analysis": None}
    model_status = {"active": None, "error": None, "candidates": [], "required": "Promotion gates"}

    def route_api(route):
        nonlocal scan
        request = route.request
        # The unified shell polls the classic watchlist/wallet; the research
        # workspace itself must only use the versioned API.
        if request.url.split("?", 1)[0].endswith("/api/watchlist"):
            return route.fulfill(json={"symbols": []})
        if request.url.split("?", 1)[0].endswith("/api/wallet"):
            return route.fulfill(status=503, json={"detail": "Offline fixture"})
        assert "/api/v1/" in request.url, "The research workspace must use the versioned API"
        path = request.url.split("/api/v1/", 1)[-1].split("?", 1)[0]
        if request.method == "POST":
            body = request.post_data_json
            posts.append((path, body))
            if path in ("research/walk-forward", "research/outcome-validation"):
                waiting[path] = route
                return
            if path == "research/scan":
                bearish = {**row, "symbol": "INFY", "action": "SELL", "score": 68,
                           "rule": {"action": "SELL", "confidence": .65},
                           "plan": {**row["plan"], "stop_loss": 101.5, "take_profit": 95.5}}
                scan = {"status": "completed", "total": 2, "processed": 2, "evaluated": 2,
                        "stale": 0, "eligible": 2, "rows": [row, bearish], "errors": [], "skipped": [],
                        "options": body, "started_at": datetime.now(UTC).isoformat(),
                        "completed_at": datetime.now(UTC).isoformat(), "effective_capital": 100000}
                route.fulfill(json=scan)
                return
            if path == "positions/":
                route.fulfill(json={"id": 1})
                return
            if path.endswith("/news"):
                route.fulfill(json=news)
                return
            raise AssertionError(f"Unexpected mutating request: {path}")
        if path == "auth/status":
            response = {"required": False}
        elif path == "research/universe":
            response = {"symbols": ["TCS", "INFY"], "count": 2, "llm_provider": "mock"}
        elif path == "research/scan":
            response = scan
        elif path == "wallet/summary":
            response = {"cash": 98000, "invested": 2000, "total_value": 100000,
                        "total_pnl": 0, "open_positions": 1, "accounting_basis": "entry_cost"}
        elif path == "research/outcome-models":
            response = model_status
        elif path.endswith("/news"):
            response = news
        elif path.startswith("research/symbols/"):
            selected_symbol = path.rsplit("/", 1)[-1]
            response = {"analysis": {**row, "symbol": selected_symbol},
                        "chart": {"symbol": selected_symbol, "insufficient_data": True}, "news": news}
        elif path == "symbols/stored":
            response = [{"symbol": "TCS"}, {"symbol": "INFY"}]
        elif path.startswith("history/"):
            response = {"candles": []}
        elif path == "positions/":
            response = [] if "status=closed" in request.url else [{
                "id": 5, "symbol": "RELIANCE", "action": "BUY", "entry_price": 200,
                "quantity": 10, "stop_loss": 195, "take_profit": 210, "status": "open", "pnl": None,
            }]
        elif path == "ready":
            response = {"status": "ready"}
        elif path == "liveness/feed":
            response = {"connected": False, "last_tick": None}
        elif path == "pipeline/status":
            response = {"status": "idle", "symbols_processed": 0, "errors": []}
        else:
            raise AssertionError(f"Unexpected API query: {path}")
        route.fulfill(json=response)

    page.on("pageerror", lambda error: failures.append(str(error)))
    page.route("**/api/**", route_api)
    page.goto(f"{base_url}/#/research", wait_until="networkidle")
    expect(page.get_by_role("heading", name="Opportunity Finder", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Walk-Forward Optimizer", exact=True)).not_to_be_visible()
    expect(page.get_by_role("region", name="Paper book at a glance")).to_contain_text("98,000")
    expect(page.get_by_role("heading", name="Open paper positions", exact=True)).to_be_visible()
    expect(page.get_by_text("10 shares at", exact=False)).to_be_visible()
    expect(page.get_by_label("Target reward/risk", exact=True)).not_to_be_visible()
    expect(page.get_by_role("link", name="Offline fixture: company publishes quarterly update", exact=True)).to_be_visible()
    assert posts == [], "Navigation must not start jobs"
    symbol = page.get_by_label("Selected research symbol", exact=True)
    symbol.fill("TCS")
    page.get_by_label("Candle interval", exact=True).select_option("5minute")
    page.get_by_label("Decision mode", exact=True).select_option("rule_only")
    page.locator(".advanced-settings > summary").click()
    page.get_by_label("Target reward/risk", exact=True).fill("3")
    page.get_by_role("button", name="Scan entire watchlist", exact=True).click()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_be_visible()
    assert posts[-1] == ("research/scan", options)
    expect(page.get_by_role("button", name="Select INFY", exact=True)).to_be_visible()
    if screenshots:
        page.locator(".advanced-settings > summary").click()
        page.screenshot(path=str(screenshots / "overview-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.screenshot(path=str(screenshots / "overview-mobile.png"), full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator(".advanced-settings > summary").click()
    page.get_by_role("button", name="Select TCS", exact=True).click()
    expect(page.get_by_role("heading", name="TCS — Price, Indicators & Patterns", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Recent News + LLM Sentiment", exact=True)).to_be_visible()
    page.get_by_role("button", name="Validate TCS strategy", exact=True).click()
    expect(page.get_by_text("Selected: TCS / 5minute.", exact=False)).to_be_visible()
    page.get_by_role("button", name="Run walk-forward evaluation", exact=True).click()
    expect(page.get_by_text("Evaluating TCS / 5minute.", exact=False)).to_be_visible()
    assert posts[-1][1]["symbol"] == "TCS"
    assert posts[-1][1]["interval"] == "5minute"
    symbol.fill("INFY")
    expect(page.get_by_role("heading", name="Validation workspace: INFY", exact=True)).to_be_visible()
    waiting.pop("research/walk-forward").fulfill(json={
        "symbol": "TCS", "interval": "5minute", "status": "insufficient_history",
        "candles": 100, "required_candles": 4500, "folds": [],
    })
    expect(page.get_by_text("No walk-forward result for these exact inputs", exact=False)).to_be_visible()
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_have_count(0)
    symbol.fill("TCS")
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_be_visible()
    page.get_by_label("Training candles", exact=True).fill("5000")
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_have_count(0)
    page.get_by_label("Training candles", exact=True).fill("3750")
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_be_visible()
    page.get_by_role("button", name="Validate TCS outcomes", exact=True).click()
    expect(page.get_by_text("Validating TCS / 5minute / 3:1.", exact=False)).to_be_visible()
    assert posts[-1][1]["reward_risk"] == 3
    symbol.fill("INFY")
    waiting.pop("research/outcome-validation").fulfill(json={
        "symbol": "TCS", "interval": "5minute", "status": "unqualified", "expires_at": now.isoformat(),
        "data_end": now.isoformat(), "policy": {"reward_risk": 3},
        "calibration": {"trades": 20, "win_rate": .5, "mean_net_return_pct": -.1, "win_rate_interval": None},
        "holdout": {"trades": 10, "win_rate": .4, "mean_net_return_pct": -.2, "win_rate_interval": None},
        "sides": {}, "limitations": "Offline fixture: not investment evidence",
    })
    expect(page.get_by_text("No outcome validation for this symbol", exact=False)).to_be_visible()
    symbol.fill("TCS")
    expect(page.get_by_text("unqualified · Data through", exact=False)).to_be_visible()
    page.get_by_label("Target reward/risk", exact=True).fill("2")
    expect(page.get_by_text("unqualified · Data through", exact=False)).to_have_count(0)
    page.get_by_label("Target reward/risk", exact=True).fill("3")
    expect(page.get_by_text("unqualified · Data through", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Use promoted ML policy and review scan settings")).to_be_disabled()
    page.get_by_role("button", name="1. Find opportunities", exact=False).click()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_have_count(0)
    expect(page.get_by_text("Rule outcome evidence updated for TCS.", exact=False)).to_be_visible()
    page.get_by_role("button", name="Scan entire watchlist", exact=True).click()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_be_visible()
    # Another client changing the paper ledger must hide allocations immediately.
    scan["allocation_warning"] = "Paper ledger changed since this scan."
    page.get_by_role("button", name="Refresh views", exact=True).click()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_have_count(0)
    expect(page.get_by_text("Paper ledger changed since this scan.", exact=False)).to_be_visible()
    scan.pop("allocation_warning")
    page.get_by_role("button", name="Refresh views", exact=True).click()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_be_visible()
    page.get_by_role("button", name="Paper operations", exact=True).click()
    expect(page.get_by_label("Paper research symbol", exact=True)).to_have_value("TCS")
    expect(page.get_by_label("Paper candle interval", exact=True)).to_have_value("5minute")
    expect(page.get_by_label("Paper entry symbol", exact=True)).to_have_value("TCS")
    expect(page.get_by_role("heading", name="Opportunity Finder", exact=True)).not_to_be_visible()
    # This manual paper write is intercepted, and must invalidate scan sizing.
    page.get_by_label("Entry price", exact=True).fill("100")
    page.get_by_label("Quantity", exact=True).fill("1")
    page.get_by_label("Stop loss", exact=True).fill("98")
    page.get_by_role("button", name="Record paper position", exact=True).click()
    expect(page.get_by_text("Paper position recorded.", exact=False)).to_be_visible()
    assert posts[-1][0] == "positions/"
    assert posts[-1][1]["symbol"] == "TCS"
    page.get_by_label("Paper research symbol", exact=True).fill("INFY")
    expect(symbol).to_have_value("INFY")
    expect(page.get_by_label("Paper entry symbol", exact=True)).to_have_value("INFY")
    expect(page.get_by_label("Entry price", exact=True)).to_have_value("")
    page.get_by_label("Paper candle interval", exact=True).select_option("15minute")
    page.get_by_role("button", name="Inspect selected symbol and plan", exact=True).click()
    expect(page.get_by_label("Candle interval", exact=True)).to_have_value("15minute")
    expect(page.get_by_role("heading", name="INFY — Price, Indicators & Patterns", exact=True)).to_be_visible()
    expect(page.get_by_text("Paper book changed.", exact=False)).to_be_visible()
    symbol.fill("TCS")
    page.get_by_label("Candle interval", exact=True).select_option("5minute")
    page.get_by_role("button", name="3. Validate strategy", exact=False).click()
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_be_visible()
    expect(page.get_by_text("unqualified · Data through", exact=False)).to_be_visible()
    symbol.fill("INFY")
    page.get_by_role("button", name="Run walk-forward evaluation", exact=True).click()
    expect(page.get_by_text("Evaluating INFY / 5minute.", exact=False)).to_be_visible()
    waiting.pop("research/walk-forward").fulfill(status=503, json={"detail": "Fixture evaluator unavailable"})
    expect(page.get_by_text("Fixture evaluator unavailable", exact=True)).to_be_visible()
    symbol.fill("TCS")
    expect(page.get_by_text("Fixture evaluator unavailable", exact=True)).to_have_count(0)
    expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_be_visible()
    assert len(posts) == 6, posts
    if screenshots:
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(screenshots / "workflow-desktop.png"), full_page=True)
    # Mobile layout stays within the viewport, with usable stage navigation.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.get_by_role("button", name="3. Validate strategy", exact=False)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    if screenshots:
        page.screenshot(path=str(screenshots / "workflow-mobile.png"), full_page=True)
    # Policy handoff is explicit and changes settings only, not model state or scans.
    model_status.update(active={"candidate_id": "fixture-promoted", "expires_at": (now + timedelta(days=1)).isoformat()},
                        candidates=[{"candidate_id": "fixture-promoted", "status": "promoted", "symbols": ["TCS"]}])
    page.get_by_role("button", name="Refresh views", exact=True).click()
    policy = page.get_by_role("button", name="Use promoted ML policy and review scan settings", exact=True)
    expect(policy).to_be_enabled()
    policy.click()
    expect(page.get_by_label("Candle interval", exact=True)).to_have_value("minute")
    expect(page.get_by_label("Decision mode", exact=True)).to_have_value("ml_only")
    expect(page.get_by_label("Target reward/risk", exact=True)).to_have_value("2")
    expect(page.get_by_role("heading", name="Opportunity Finder", exact=True)).to_be_visible()
    assert len(posts) == 6, "Policy handoff must not scan or promote"
    # Expired snapshots remain inspectable but cannot carry an active allocation.
    row["expires_at"] = (now - timedelta(minutes=1)).isoformat()
    page.get_by_role("button", name="Scan entire watchlist", exact=True).click()
    expect(page.get_by_text("Blocked / expired", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Select TCS", exact=True)).to_have_count(0)
    page.get_by_role("button", name="2. Inspect selected stock", exact=False).click()
    expect(page.get_by_text("Blocked: Expired snapshot; re-scan", exact=True)).to_be_visible()
    assert len(posts) == 7
    assert not failures, failures


def main():
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("Build the frontend first")
    screenshots = dist.parents[1] / "artifacts" / "dashboard-repair" if "--screenshots" in sys.argv else None
    if screenshots:
        screenshots.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(dist))) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel=environ.get("PLAYWRIGHT_BROWSER_CHANNEL"))
                try:
                    exercise(browser.new_page(viewport={"width": 1440, "height": 1000}),
                             f"http://127.0.0.1:{server.server_port}", screenshots)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("PASS: shared workflow, request isolation, result retention, scan invalidation, paper context and mobile layout")


if __name__ == "__main__":
    main()
