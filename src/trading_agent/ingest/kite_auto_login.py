"""Automated Kite Connect login using TOTP and Playwright."""

from __future__ import annotations

import re
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

from trading_agent.core.config import get_settings
from trading_agent.core.exceptions import AuthenticationError
from trading_agent.core.logging_config import get_logger
from trading_agent.ingest.kite_auth import KiteAuth, KiteCredentials

logger = get_logger(__name__)

load_dotenv()


class AutoLogin:
    """Automated Kite login with TOTP."""

    def __init__(self, credentials: KiteCredentials | None = None):
        self.credentials = credentials or KiteCredentials(
            api_key=get_settings().kite.api_key,
            api_secret=get_settings().kite.api_secret,
            user_id=get_settings().kite.user_id,
            password=get_settings().kite.password,
            totp_secret=get_settings().kite.totp_secret,
            redirect_url=get_settings().kite.redirect_url,
        )
        self.auth = KiteAuth(self.credentials)
        self._access_token_path = Path(".access_token")

    def generate_totp(self) -> str:
        """Generate current TOTP code."""
        import pyotp

        if not self.credentials.totp_secret:
            raise AuthenticationError("TOTP secret not configured")
        return pyotp.TOTP(self.credentials.totp_secret).now()

    async def login_with_playwright(self) -> str:
        """Automated browser login using Playwright."""
        from playwright.async_api import async_playwright

        if not all(
            [
                self.credentials.api_key,
                self.credentials.api_secret,
                self.credentials.totp_secret,
                self.credentials.user_id,
                self.credentials.password,
            ]
        ):
            raise AuthenticationError("Missing required credentials for auto-login")

        kite = KiteConnect(api_key=self.credentials.api_key)
        login_url = kite.login_url()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                logger.info("navigating_to_kite_login")
                await page.goto(login_url)

                # Wait for login form
                await page.wait_for_selector('input[type="text"][name="user_id"]', timeout=30000)

                # Fill credentials
                await page.fill('input[type="text"][name="user_id"]', self.credentials.user_id)
                await page.fill(
                    'input[type="password"][name="password"]', self.credentials.password
                )
                await page.click('button[type="submit"]:has-text("Login")')

                # Wait for TOTP page
                await page.wait_for_selector('input[name="totp"]', timeout=30000)

                # Enter TOTP
                totp_code = self.generate_totp()
                logger.info("generated_totp", code=totp_code[:2] + "****")
                await page.fill('input[name="totp"]', totp_code)
                await page.click(
                    'button[type="submit"]:has-text("Continue"), button[type="submit"]:has-text("Submit")'
                )

                # Wait for redirect
                await page.wait_for_url(
                    re.compile(rf"{re.escape(self.credentials.redirect_url)}.*request_token="),
                    timeout=30000,
                )

                # Extract request_token
                current_url = page.url
                match = re.search(r"request_token=([^&]+)", current_url)
                if not match:
                    raise AuthenticationError(
                        f"request_token not found in redirect URL: {current_url}"
                    )

                request_token = match.group(1)
                logger.info("got_request_token", token=request_token[:10] + "...")

            finally:
                await browser.close()

        # Generate access token
        return self.auth.generate_session(request_token)

    def login_manual(self, request_token: str) -> str:
        """Manual login with request token."""
        return self.auth.generate_session(request_token)

    def ensure_valid_token(self) -> str:
        """Ensure we have a valid access token."""
        # Try loading from file
        token = self.auth.load_access_token()
        if token and self.auth.validate_token():
            logger.info("loaded_valid_token_from_file")
            return token

        # Try auto-login with Playwright
        try:
            logger.info("attempting_auto_login")
            return self.login_with_playwright()
        except Exception as e:
            logger.warning("auto_login_failed", error=str(e))

        # Fallback to manual
        raise AuthenticationError(
            "Auto-login failed. Please run manual login:\n"
            f"1. Visit: {self.auth.login_url()}\n"
            "2. Log in and copy request_token from redirect URL\n"
            "3. Call login_manual(request_token)"
        )


def ensure_valid_token() -> str:
    """Convenience function to get valid token."""
    auto_login = AutoLogin()
    return auto_login.ensure_valid_token()


if __name__ == "__main__":
    import asyncio

    async def main():
        auto_login = AutoLogin()
        try:
            token = await auto_login.login_with_playwright()
            print(f"Success! Access token: {token[:20]}...")
        except Exception as e:
            print(f"Auto-login failed: {e}")
            print("Falling back to manual login...")
            print("1. Open this URL, log in, and paste the request_token from the redirect:\n")
            kite = KiteConnect(api_key=auto_login.credentials.api_key)
            print(kite.login_url())
            token = input("\nrequest_token: ").strip()
            auto_login.login_manual(token)

    asyncio.run(main())
