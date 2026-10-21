"""Offline browser check: run after `npm run build` in frontend.

Uses a temporary loopback server and mocked API responses, never the real book.
Requires Playwright's Chromium browser to be installed.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from os import environ
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise RuntimeError("Build the frontend before running this check")
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(dist))) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel=environ.get("PLAYWRIGHT_BROWSER_CHANNEL"))
                try:
                    for required in (False, True):
                        context = browser.new_context()
                        page = context.new_page()
                        failures = []
                        page.on("pageerror", lambda error: failures.append(str(error)))
                        page.route("**/api/**", lambda route: route.fulfill(
                            status=503, json={"detail": "Offline fixture: no market data requested"}))
                        page.route("**/api/v1/auth/status", lambda route: route.fulfill(json={"required": required}))
                        page.route("**/api/v1/research/outcome-models", lambda route: route.fulfill(json={
                            "active": None, "error": None, "required": "Both sides require historical and forward evidence",
                            "candidates": [{"candidate_id": "a"*32, "status": "historical_gate_failed", "symbols": ["TEST"],
                                            "test": {"BUY": {"trades": 0, "sessions": 0, "win_rate": None,
                                                             "win_lower_95": None, "mean_net_return_pct": None, "passed": False}}}],
                        }))
                        page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
                        expect(page.get_by_text("API authentication", exact=True)).to_have_count(int(required))
                        page.get_by_role("button", name="3. Validate strategy", exact=False).click()
                        expect(page.get_by_role("heading", name="BUY / SELL Outcome Models", exact=True)).to_be_visible()
                        page.get_by_text("aaaaaaaaaaaa · historical_gate_failed", exact=True).click()
                        expect(page.get_by_text("BUY: historical gate failed", exact=False)).to_contain_text("wins Unavailable")
                        if not required:
                            expect(page.locator('input[type="password"]')).to_have_count(0)
                        page.get_by_role("button", name="Paper operations", exact=True).click()
                        expect(page.get_by_text("API authentication", exact=True)).to_have_count(0)
                        expect(page.get_by_text("API access token (if enabled on the server)", exact=True)).to_have_count(int(required))
                        if not required:
                            expect(page.locator('input[type="password"]')).to_have_count(0)
                        assert not failures, failures
                        context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print("PASS: authentication controls hidden when disabled; available when required; no duplicate forms")


if __name__ == "__main__":
    main()
