"""Standalone private-stack smoke check; invoked only by docker-compose.smoke.yml.

Never point this at a real book: it records and closes a synthetic paper position.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import httpx
import pandas as pd
import numpy as np
from playwright.sync_api import sync_playwright, expect
from fastapi.testclient import TestClient

from trading_agent.core.config import get_settings
from trading_agent.storage.parquet_store import get_parquet_store


def main():
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Exercise the actual Linux console entry point and Docker's SIGTERM path.
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(["trading-agent-scheduler"], stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                output.seek(0)
                log = output.read()
                if "Scheduler started; waiting" in log:
                    break
                assert process.poll() is None, log
                time.sleep(.1)
            else:
                raise AssertionError("Scheduler did not start")
            time.sleep(.2)
            process.terminate()
            assert process.wait(timeout=15) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    # Windows 1.5.2 Cython pickles use an unqualified loss module name.
    from trading_agent.strategy.research_features import load_legacy_bundle
    from sklearn._loss._loss import CyHalfMultinomialLoss
    legacy_fixture = settings.data_dir / "windows-loss.joblib"
    legacy_fixture.write_bytes(b"c_loss\nCyHalfMultinomialLoss\n.")
    assert load_legacy_bundle(legacy_fixture) is CyHalfMultinomialLoss
    for arguments in [("upgrade", "head"), ("check",)]:
        subprocess.run([sys.executable, "-m", "alembic", *arguments], check=True)
    store = get_parquet_store()
    frame = pd.DataFrame({"timestamp": ["2026-10-19T03:45:00Z", "2026-10-19 03:46:00.000000Z"],
                          "open": [100, 101], "high": [100, 101], "low": [100, 101],
                          "close": [100, 101], "volume": [10, 20], "oi": [None, 0]})
    store.write_candles(frame.iloc[:1], "SMOKE", "minute")
    store.write_candles(frame.iloc[1:], "SMOKE", "minute")
    store.write_candles(frame, "SMOKE", "minute")
    assert len(store.read_candles("SMOKE", "minute")) == 2
    from trading_agent.api.main import create_app
    with TestClient(create_app()) as local:
        history = local.get("/api/v1/history/SMOKE?interval=minute")
        assert history.status_code == 200, history.text
        assert [row["oi"] for row in history.json()["candles"]] == [None, 0]
        paper = local.post("/api/v1/positions/", json={"symbol": "BREAKEVEN", "action": "BUY",
                           "entry_price": 100, "quantity": 10, "stop_loss": 95}).json()
        assert local.post(f"/api/v1/positions/{paper['id']}/close?exit_price=100").status_code == 200
        assert local.get("/api/v1/wallet/metrics").status_code == 200
        prices = 100 + np.sin(np.arange(600) / 9) * 3 + np.arange(600) * 0.01
        store.write_candles(pd.DataFrame({
            "timestamp": pd.date_range("2026-10-19T03:45:00Z", periods=600, freq="min"),
            "open": prices - 0.2, "high": prices + 1, "low": prices - 1,
            "close": prices, "volume": 100000,
        }), "CHART", "minute")
        chart_result = local.get("/api/v1/research/symbols/CHART?interval=minute&mode=rule_only")
        assert chart_result.status_code == 200, chart_result.text
        chart_fixture = chart_result.json()
        walk_forward = local.post("/api/v1/research/walk-forward", json={
            "symbol": "CHART", "train_candles": 250, "test_candles": 100,
        })
        assert walk_forward.status_code == 200, walk_forward.text
        assert len(walk_forward.json()["folds"]) == 3
        outcomes = local.post("/api/v1/research/outcome-validation", json={"symbol": "CHART"})
        assert outcomes.status_code == 422 and "1,000" in outcomes.text
    store.compact("SMOKE", "minute")
    assert len(store.read_candles("SMOKE", "minute")) == 2
    with httpx.Client(base_url="http://frontend:8080", timeout=15) as client:
        assert client.get("/api/v1/ready").status_code == 200
        assert client.get("/api/v1/wallet/summary").status_code == 401
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        # Serve an actual isolated API response to exercise Plotly rendering.
        page.route("**/api/v1/research/symbols/CHART?*", lambda route: route.fulfill(json=chart_fixture))
        page.goto("http://frontend:8080", wait_until="networkidle")
        expect(page.get_by_role("heading", name="Watchlist Bias", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Scan entire watchlist", exact=True)).to_be_visible()
        page.get_by_text("API authentication", exact=True).click()
        page.get_by_label("Session API token", exact=True).fill("smoke-test-only")
        page.get_by_role("button", name="Apply session token", exact=True).click()
        page.get_by_label("Selected research symbol", exact=True).fill("CHART")
        page.get_by_role("button", name="Inspect CHART", exact=True).click()
        expect(page.locator(".js-plotly-plot")).to_be_visible(timeout=30000)
        expect(page.get_by_role("heading", name="Hybrid Decision Maker", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Stop Loss, Target & Position Sizing", exact=True)).to_be_visible()
        expect(page.get_by_label("Require qualified outcome evidence")).to_be_checked()
        page.get_by_role("button", name="Validate CHART strategy", exact=True).click()
        expect(page.get_by_role("heading", name="Walk-Forward Optimizer", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Trade Outcome Validation", exact=True)).to_be_visible()
        page.get_by_role("button", name="Run walk-forward evaluation", exact=True).click()
        expect(page.get_by_text("Walk-forward: insufficient_history", exact=False)).to_be_visible(timeout=30000)
        page.get_by_role("button", name="Validate CHART outcomes", exact=True).click()
        expect(page.get_by_text("At least 1,000 completed candles are required", exact=True)).to_be_visible(timeout=30000)
        page.get_by_role("tab", name="Paper operations", exact=True).click()
        page.get_by_text("API access token (if enabled on the server)").click()
        page.get_by_label("Bearer token").fill("smoke-test-only")
        page.get_by_role("button", name="Use for this page session").click()
        # Paper operations uses the same selection, with no second scanner.
        page.get_by_label("Paper research symbol", exact=True).fill("SMOKE")
        page.get_by_label("Entry price", exact=True).fill("100")
        page.get_by_label("Quantity", exact=True).fill("10")
        page.get_by_label("Stop loss", exact=True).fill("95")
        page.get_by_role("button", name="Record paper position", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("Paper position recorded.")
        page.get_by_label("Exit price for SMOKE").fill("105")
        page.get_by_role("button", name="Close paper position", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("Paper position closed.")
        page.reload(wait_until="networkidle")
        page.get_by_role("tab", name="Paper operations", exact=True).click()
        # Tokens are deliberately not stored across reloads.
        page.get_by_text("API access token (if enabled on the server)").click()
        page.get_by_label("Bearer token").fill("smoke-test-only")
        page.get_by_role("button", name="Use for this page session").click()
        expect(page.get_by_text("No open positions.", exact=True)).to_be_visible()
        assert not failures, failures
        browser.close()
    with httpx.Client(base_url="http://api:8000", headers={"Authorization": "Bearer smoke-test-only"}) as client:
        assert client.get("/api/v1/research/readiness").status_code == 200
        automation = client.get("/api/v1/research/automation").json()
        assert automation["running"] and not automation["enabled"]
        comparison = client.post("/api/v1/strategy-lab/compare", json={"symbol": "MISSING"})
        assert comparison.status_code == 200 and comparison.json()["status"] == "insufficient_history"
        book = client.get("/api/v1/wallet/summary").json()
        assert book["cash"] == 1000050 and book["open_positions"] == 0, book
        assert len(client.get("/api/v1/wallet/trades").json()) == 1
    print("PASS: migrations, Parquet replay/compaction, nginx proxy, authentication, research panels, browser paper lifecycle and persistence")


if __name__ == "__main__":
    main()
