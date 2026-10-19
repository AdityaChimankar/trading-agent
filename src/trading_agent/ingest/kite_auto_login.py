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

    async def login_with_playwright(self, headless: bool | None = None) -> str:
        """Automated browser login using Playwright."""
        from playwright.async_api import async_playwright

        if headless is None:
            headless = get_settings().kite.login_headless

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
            # Use stealth settings to avoid detection
            browser = await p.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ] if headless else [],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            page = await context.new_page()
            redirect_url: str | None = None

            def capture_redirect(request) -> None:
                nonlocal redirect_url
                if "request_token=" in request.url:
                    redirect_url = request.url

            page.on("request", capture_redirect)

            # Inject stealth script
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)

            try:
                logger.info("navigating_to_kite_login")
                await page.goto(login_url, wait_until="networkidle")

                # Wait for login form - try multiple possible selectors for userid
                userid_selectors = ['input#userid', 'input[name="user_id"]', 'input[type="text"]', '#userid']
                userid_found = False
                for selector in userid_selectors:
                    try:
                        await page.wait_for_selector(selector, timeout=10000)
                        logger.info("found_userid_field", selector=selector)
                        await page.fill(selector, self.credentials.user_id)
                        userid_found = True
                        break
                    except Exception:
                        continue
                
                if not userid_found:
                    raise AuthenticationError("Could not find userid input field")

                # Fill password - try multiple selectors
                password_selectors = ['input#password', 'input[name="password"]', 'input[type="password"]']
                password_found = False
                for selector in password_selectors:
                    try:
                        await page.fill(selector, self.credentials.password)
                        password_found = True
                        break
                    except Exception:
                        continue
                
                if not password_found:
                    raise AuthenticationError("Could not find password input field")

                # Click submit button
                submit_selectors = ['button[type="submit"]', 'button:has-text("Login")', 'input[type="submit"]']
                for selector in submit_selectors:
                    try:
                        await page.click(selector)
                        break
                    except Exception:
                        continue

                # Wait for TOTP page - try multiple possible selectors
                totp_selectors = [
                    'input#totp',
                    'input[name="totp"]',
                    'input[autocomplete="one-time-code"]',
                    'input[type="number"]',
                    'input[placeholder*="TOTP" i]',
                    'input[placeholder*="2FA" i]',
                    'input[placeholder*="PIN" i]',
                ]
                try:
                    totp_field = page.locator(", ".join(totp_selectors)).first
                    await totp_field.wait_for(state="visible", timeout=30000)
                    logger.info("found_totp_field")

                    totp_code = self.generate_totp()
                    logger.info("generated_totp")
                    await totp_field.fill(totp_code)
                except Exception:
                    inputs = await page.locator("input").evaluate_all(
                        """elements => elements.map(element => ({
                            id: element.id,
                            name: element.name,
                            type: element.type,
                            placeholder: element.placeholder,
                            autocomplete: element.autocomplete
                        }))"""
                    )
                    logger.warning(
                        "totp_field_not_found",
                        inputs=inputs,
                    )
                    raise AuthenticationError("Could not find TOTP input field")

                # Click submit button for TOTP
                for selector in submit_selectors:
                    try:
                        button = page.locator(selector).first
                        if await button.is_visible():
                            await button.click(timeout=3000)
                            break
                    except Exception:
                        continue

                # Wait for redirect
                for _ in range(300):
                    if redirect_url:
                        break
                    await page.wait_for_timeout(100)

                if not redirect_url:
                    logger.warning(
                        "kite_redirect_not_received",
                    )
                    raise AuthenticationError("Kite login did not return a request token")

                # Extract request_token
                current_url = redirect_url
                match = re.search(r"request_token=([^&]+)", current_url)
                if not match:
                    raise AuthenticationError(
                        "request_token not found in redirect URL"
                    )

                request_token = match.group(1)
                logger.info("got_request_token")

            finally:
                await browser.close()

        # Generate access token
        return self.auth.generate_session(request_token)

    def login_manual(self, request_token: str) -> str:
        """Manual login with request token."""
        return self.auth.generate_session(request_token)

    async def ensure_valid_token(self) -> str:
        """Ensure we have a valid access token."""
        # Try loading from file
        token = self.auth.load_access_token()
        if token and self.auth.validate_token():
            logger.info("loaded_valid_token_from_file")
            return token

        # Try auto-login with Playwright
        try:
            logger.info("attempting_auto_login")
            return await self.login_with_playwright()
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
    import asyncio

    auto_login = AutoLogin()
    return asyncio.run(auto_login.ensure_valid_token())


if __name__ == "__main__":
    import asyncio

    async def main():
        auto_login = AutoLogin()
        try:
            # Use non-headless mode by default for reliability
            token = await auto_login.login_with_playwright(headless=False)
            print("Success! Access token saved.")
        except Exception as e:
            print(f"Auto-login failed: {e}")
            print("Falling back to manual login...")
            print("1. Open this URL, log in, and paste the request_token from the redirect:\n")
            kite = KiteConnect(api_key=auto_login.credentials.api_key)
            print(kite.login_url())
            token = input("\nrequest_token: ").strip()
            auto_login.login_manual(token)

    asyncio.run(main())
