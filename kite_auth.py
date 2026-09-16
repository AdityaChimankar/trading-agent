"""
One-time-per-day login flow for Kite Connect.
Kite access tokens expire daily at ~7:30 AM IST, so this needs to be
re-run each trading day (can be scripted with the request_token you
copy from the redirect URL after logging in).

Setup:
1. Create a developer account at developers.kite.trade
2. Subscribe to the paid Kite Connect plan (₹500/mo - required for
   live + historical market data, not just order placement)
3. Create an app, set a redirect URL (can be http://localhost for dev)
4. Put your API key + secret in a .env file (see .env.example)
"""
import os
from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")


def get_login_url() -> str:
    kite = KiteConnect(api_key=API_KEY)
    return kite.login_url()


def generate_access_token(request_token: str) -> str:
    """
    request_token comes from the redirect URL after you log in
    via the URL from get_login_url(). Example redirect:
    http://localhost/?request_token=XXXX&action=login&status=success
    """
    kite = KiteConnect(api_key=API_KEY)
    session = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session["access_token"]

    # Persist for the rest of today's scripts to reuse
    with open(".access_token", "w") as f:
        f.write(access_token)

    print("Access token saved to .access_token")
    return access_token


def get_kite_client() -> KiteConnect:
    """Load a ready-to-use, authenticated Kite client."""
    kite = KiteConnect(api_key=API_KEY)
    with open(".access_token") as f:
        access_token = f.read().strip()
    kite.set_access_token(access_token)
    return kite


if __name__ == "__main__":
    print("1. Open this URL, log in, and paste the request_token from the redirect:\n")
    print(get_login_url())
    token = input("\nrequest_token: ").strip()
    generate_access_token(token)
