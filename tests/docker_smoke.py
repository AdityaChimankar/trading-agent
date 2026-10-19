"""Standalone private-stack smoke check; invoked only by docker-compose.smoke.yml.

Never point this at a real book: it records and closes a synthetic paper position.
"""
from pathlib import Path
import subprocess
import sys

import httpx
import pandas as pd
from playwright.sync_api import sync_playwright, expect
from fastapi.testclient import TestClient

from trading_agent.core.config import get_settings
from trading_agent.storage.parquet_store import get_parquet_store


def main():
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
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
        page.goto("http://frontend:8080", wait_until="networkidle")
        page.get_by_text("API access token (if enabled on the server)").click()
        page.get_by_label("Bearer token").fill("smoke-test-only")
        page.get_by_role("button", name="Use for this page session").click()
        page.locator('input[name="symbol"]').fill("SMOKE")
        page.get_by_label("Entry price", exact=True).fill("100")
        page.get_by_label("Quantity", exact=True).fill("10")
        page.get_by_label("Stop loss", exact=True).fill("95")
        page.get_by_role("button", name="Record paper position", exact=True).click()
        expect(page.get_by_role("status")).to_have_text("Paper position recorded.")
        page.get_by_label("Exit price for SMOKE").fill("105")
        page.get_by_role("button", name="Close paper position", exact=True).click()
        expect(page.get_by_role("status")).to_have_text("Paper position closed.")
        page.reload(wait_until="networkidle")
        # Tokens are deliberately not stored across reloads.
        page.get_by_text("API access token (if enabled on the server)").click()
        page.get_by_label("Bearer token").fill("smoke-test-only")
        page.get_by_role("button", name="Use for this page session").click()
        expect(page.get_by_text("No open positions.", exact=True)).to_be_visible()
        assert not failures, failures
        browser.close()
    with httpx.Client(base_url="http://api:8000", headers={"Authorization": "Bearer smoke-test-only"}) as client:
        book = client.get("/api/v1/wallet/summary").json()
        assert book["cash"] == 1000050 and book["open_positions"] == 0, book
        assert len(client.get("/api/v1/wallet/trades").json()) == 1
    print("PASS: migrations, Parquet replay/compaction, nginx proxy, authentication, browser paper lifecycle and persistence")


if __name__ == "__main__":
    main()
