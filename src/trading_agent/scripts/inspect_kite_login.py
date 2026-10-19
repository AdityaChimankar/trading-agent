"""Optional login-form diagnostic, never an automatically collected test.

Run from the repository root with --help. Uses configured credentials only;
does not submit them unless explicitly requested, and never completes TOTP.
"""
import argparse
import json
import sys


def describe_inputs(page) -> list[dict]:
    """Describe selectors without exposing input values or page contents."""
    return [
        {attribute: element.get_attribute(attribute) for attribute in ("type", "name", "id")}
        for element in page.query_selector_all("input")
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", help="Show the diagnostic browser window")
    parser.add_argument("--submit-credentials", action="store_true",
                        help="Submit configured user ID/password to inspect the next form (no TOTP)")
    args = parser.parse_args(argv)

    from trading_agent.core.config import get_settings

    settings = get_settings().kite
    if not settings.api_key:
        parser.error("Set KITE_API_KEY in the environment or .env first")
    if args.submit_credentials and (not settings.user_id or not settings.password):
        parser.error("Credential submission requires KITE_USER_ID and KITE_PASSWORD")

    from kiteconnect import KiteConnect
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            try:
                page = browser.new_page()
                page.set_default_timeout(15000)
                page.goto(KiteConnect(api_key=settings.api_key).login_url(), wait_until="domcontentloaded")
                page.wait_for_selector("input")
                print("Login-form input selectors:")
                print(json.dumps(describe_inputs(page), indent=2))
                if args.submit_credentials:
                    page.fill("input#userid", settings.user_id)
                    page.fill("input#password", settings.password)
                    page.click('button[type="submit"]')
                    page.wait_for_timeout(3000)
                    print("Input selectors after submission (login success is not verified):")
                    print(json.dumps(describe_inputs(page), indent=2))
            finally:
                browser.close()
    except Exception as exc:
        # Browser exception messages can contain request URLs/tokens.
        print(f"Login-form diagnostic failed ({type(exc).__name__}). Check configuration and browser installation.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
