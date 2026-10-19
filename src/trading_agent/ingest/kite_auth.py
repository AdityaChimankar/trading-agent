"""Kite Connect authentication and session management."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

from trading_agent.core.config import get_settings
from trading_agent.core.exceptions import AuthenticationError
from trading_agent.core.logging_config import get_logger

logger = get_logger(__name__)

load_dotenv()


@dataclass(frozen=True)
class KiteCredentials:
    """Kite Connect credentials."""

    api_key: str
    api_secret: str
    access_token: str = ""
    user_id: str = ""
    password: str = ""
    totp_secret: str = ""
    redirect_url: str = "http://localhost"


class KiteAuth:
    """Kite Connect authentication manager."""

    def __init__(self, credentials: KiteCredentials | None = None):
        settings = get_settings()
        self.credentials = credentials or KiteCredentials(
            api_key=settings.kite.api_key,
            api_secret=settings.kite.api_secret,
            access_token=settings.kite.access_token,
            user_id=settings.kite.user_id,
            password=settings.kite.password,
            totp_secret=settings.kite.totp_secret,
            redirect_url=settings.kite.redirect_url,
        )
        self._kite: KiteConnect | None = None
        self._access_token_path = settings.data_dir / ".access_token"
        if self._access_token_path.exists():
            token = self._access_token_path.read_text().strip()
            if token:
                self.credentials = KiteCredentials(
                    **{**self.credentials.__dict__, "access_token": token}
                )

    @property
    def kite(self) -> KiteConnect:
        """Get authenticated Kite client."""
        if self._kite is None:
            self._kite = KiteConnect(api_key=self.credentials.api_key)
            if self.credentials.access_token:
                self._kite.set_access_token(self.credentials.access_token)
        return self._kite

    def login_url(self) -> str:
        """Get login URL for manual authentication."""
        return self.kite.login_url()

    def generate_session(self, request_token: str) -> str:
        """Generate access token from request token."""
        try:
            session = self.kite.generate_session(
                request_token, api_secret=self.credentials.api_secret
            )
            access_token = session["access_token"]
            self._save_access_token(access_token)
            self._kite.set_access_token(access_token)
            logger.info("kite_session_generated")
            return access_token
        except Exception as e:
            logger.error("kite_session_failed", error=str(e))
            raise AuthenticationError(f"Failed to generate session: {e}") from e

    def _save_access_token(self, token: str) -> None:
        """Atomically publish a private token file; readers never see a partial token."""
        self._access_token_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".access_token-", dir=self._access_token_path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._access_token_path)
        finally:
            temporary.unlink(missing_ok=True)
        self.credentials = KiteCredentials(**{**self.credentials.__dict__, "access_token": token})

    def load_access_token(self) -> str | None:
        """Load access token from file."""
        if self._access_token_path.exists():
            with open(self._access_token_path) as f:
                token = f.read().strip()
            if token:
                self.kite.set_access_token(token)
                self.credentials = KiteCredentials(
                    **{**self.credentials.__dict__, "access_token": token}
                )
                return token
        return None

    def validate_token(self) -> bool:
        """Validate current access token."""
        try:
            self.kite.profile()
            return True
        except Exception:
            return False

    def ensure_valid_token(self) -> str:
        """Ensure we have a valid token, re-authenticate if needed."""
        if self.validate_token():
            return self.credentials.access_token

        logger.warning("kite_token_invalid_reauth")
        # Could implement auto-login here with TOTP
        raise AuthenticationError("Token invalid and auto-login not implemented")

    def logout(self) -> None:
        """Logout and clear token."""
        with contextlib.suppress(Exception):
            self.kite.invalidate_access_token()
        self._access_token_path.unlink(missing_ok=True)
        self._kite = None
        logger.info("kite_logged_out")


# Global instance
_auth: KiteAuth | None = None


def get_kite_auth(credentials: KiteCredentials | None = None) -> KiteAuth:
    """Get or create the global Kite auth instance."""
    global _auth
    if _auth is None:
        _auth = KiteAuth(credentials)
    return _auth


if __name__ == "__main__":
    """Manual login entry point."""
    import os
    from kiteconnect import KiteConnect
    from dotenv import load_dotenv

    load_dotenv()

    api_key = os.getenv('KITE_API_KEY')
    api_secret = os.getenv('KITE_API_SECRET')

    if not api_key or not api_secret:
        print("ERROR: KITE_API_KEY and KITE_API_SECRET must be set in .env file")
        exit(1)

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print("=" * 70)
    print("MANUAL KITE CONNECT LOGIN")
    print("=" * 70)
    print("\n1. Open this URL in your browser:")
    print(f"\n   {login_url}\n")
    print("2. Log in with your Kite credentials:")
    print(f"   - User ID: {os.getenv('KITE_USER_ID', 'YOUR_USER_ID')}")
    print("   - Password: ********")
    print("   - TOTP from your authenticator app")
    print()
    print("3. After successful login, you'll be redirected to:")
    redirect_url = os.getenv('KITE_REDIRECT_URL', 'http://localhost')
    print(f"   {redirect_url}/?request_token=XXXXXXXXXXXXXXXXXXXX")
    print()
    print("4. Copy the 'request_token' from the URL (the XXXXXXXXXXXXXXXX part)")
    print("5. Paste it below when prompted\n")

    request_token = input("Enter request_token: ").strip()

    if not request_token:
        print("\nERROR: No request_token provided")
        exit(1)

    # Generate access token
    try:
        kite = KiteConnect(api_key=api_key)
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]

        # Save to file
        with open('.access_token', 'w') as f:
            f.write(access_token)

        print("\nSuccess! Access token saved to .access_token")
        print("Access token saved.")

        # Test the token
        kite.set_access_token(access_token)
        profile = kite.profile()
        print(f"\nLogged in as: {profile['user_name']} ({profile['user_id']})")
        print(f"Broker: {profile.get('broker', 'N/A')}")
        print(f"Email: {profile.get('email', 'N/A')}")

    except Exception as e:
        print(f"\nError: {e}")
        print("\nTroubleshooting:")
        print("  - Make sure you copied the request_token correctly")
        print("  - The request_token is only valid for a few minutes")
        print("  - Try generating a new request_token by visiting the login URL again")
        exit(1)
