"""
Automated Kite Connect login using TOTP (Time-based One-Time Password).
This eliminates the daily manual login step by:
1. Generating TOTP from the shared secret
2. Using Playwright to automate the browser login flow
3. Extracting the request_token from the redirect URL
4. Generating and saving the access_token

Prerequisites:
- Playwright: pip install playwright && playwright install chromium
- pyotp: pip install pyotp
- Kite credentials in .env including KITE_TOTP_SECRET

Setup:
1. Enable 2FA on Kite and get the TOTP secret (shown when setting up 2FA)
2. Add KITE_TOTP_SECRET=your_totp_secret to .env
3. Set KITE_REDIRECT_URL in .env (e.g., http://localhost)
4. Run: python -m ingest.kite_auto_login
"""
import os
import re
import time
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from paths import ACCESS_TOKEN_PATH

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")
TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")
REDIRECT_URL = os.getenv("KITE_REDIRECT_URL", "http://localhost")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")


def generate_totp() -> str:
    """Generate current TOTP code from secret."""
    import pyotp
    if not TOTP_SECRET:
        raise ValueError("KITE_TOTP_SECRET not set in .env")
    totp = pyotp.TOTP(TOTP_SECRET)
    return totp.now()


def _get_sync_playwright():
    """Import Playwright lazily so editor/runtime checks do not fail before install."""
    try:
        from importlib import import_module
        return import_module("playwright.sync_api").sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc


def login_with_totp() -> str:
    """
    Automated browser login to Kite using Playwright.
    Returns the access_token.
    """
    sync_playwright = _get_sync_playwright()
    
    if not all([API_KEY, API_SECRET, TOTP_SECRET, KITE_USER_ID, KITE_PASSWORD]):
        raise ValueError(
            "Missing required env vars: KITE_API_KEY, KITE_API_SECRET, "
            "KITE_TOTP_SECRET, KITE_USER_ID, KITE_PASSWORD"
        )
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        try:
            print("Navigating to Kite login...")
            page.goto(login_url)
            
            # Wait for login form
            page.wait_for_selector('input[type="text"][name="user_id"]', timeout=30000)
            
            # Fill user ID
            page.fill('input[type="text"][name="user_id"]', KITE_USER_ID)
            
            # Fill password
            page.fill('input[type="password"][name="password"]', KITE_PASSWORD)
            
            # Click login button
            page.click('button[type="submit"]:has-text("Login")')
            
            # Wait for TOTP page
            page.wait_for_selector('input[name="totp"]', timeout=30000)
            
            # Generate and enter TOTP
            totp_code = generate_totp()
            print(f"Generated TOTP: {totp_code}")
            page.fill('input[name="totp"]', totp_code)
            
            # Click continue/submit
            page.click('button[type="submit"]:has-text("Continue"), button[type="submit"]:has-text("Submit")')
            
            # Wait for redirect to capture request_token
            page.wait_for_url(re.compile(rf"{re.escape(REDIRECT_URL)}.*request_token="), timeout=30000)
            
            # Extract request_token from URL
            current_url = page.url
            match = re.search(r"request_token=([^&]+)", current_url)
            if not match:
                raise ValueError(f"request_token not found in redirect URL: {current_url}")
            
            request_token = match.group(1)
            print(f"Got request_token: {request_token[:10]}...")
            
        finally:
            browser.close()
    
    # Generate access token from request_token
    return generate_access_token(request_token)


def generate_access_token(request_token: str) -> str:
    """
    request_token comes from the redirect URL after login.
    Example redirect: http://localhost/?request_token=XXXX&action=login&status=success
    """
    kite = KiteConnect(api_key=API_KEY)
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session["access_token"]

    # Persist for the rest of today's scripts to reuse
    with open(ACCESS_TOKEN_PATH, "w") as f:
        f.write(access_token)

    print(f"Access token saved to {ACCESS_TOKEN_PATH.name}")
    return access_token


def get_kite_client() -> KiteConnect:
    """Load a ready-to-use, authenticated Kite client."""
    kite = KiteConnect(api_key=API_KEY)
    with open(ACCESS_TOKEN_PATH) as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


def check_token_valid() -> bool:
    """Check if current access token is still valid."""
    try:
        kite = get_kite_client()
        kite.profile()  # This will fail if token is invalid/expired
        return True
    except Exception:
        return False


def ensure_valid_token() -> str:
    """
    Ensure we have a valid access token.
    If current token is invalid/expired, run automated login.
    Returns the valid access_token.
    """
    if check_token_valid():
        with open(ACCESS_TOKEN_PATH) as f:
            return f.read().strip()
    
    print("Token expired or invalid. Running automated login...")
    return login_with_totp()


if __name__ == "__main__":
    try:
        token = ensure_valid_token()
        print(f"Success! Access token: {token[:20]}...")
    except Exception as e:
        print(f"Auto-login failed: {e}")
        print("Falling back to manual login...")
        print("1. Open this URL, log in, and paste the request_token from the redirect:\n")
        kite = KiteConnect(api_key=API_KEY)
        print(kite.login_url())
        token = input("\nrequest_token: ").strip()
        generate_access_token(token)