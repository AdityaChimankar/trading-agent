#!/usr/bin/env python
"""
Manual Kite Connect Login Script.

This script guides you through the manual login process to generate a valid access token.
Run this script whenever your access token expires (typically daily at ~7:30 AM IST).

Usage:
    python manual_login.py
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def main():
    api_key = os.getenv('KITE_API_KEY')
    api_secret = os.getenv('KITE_API_SECRET')
    
    if not api_key or not api_secret:
        print("ERROR: KITE_API_KEY and KITE_API_SECRET must be set in .env file")
        print("Check your .env file has:")
        print("  KITE_API_KEY=your_key")
        print("  KITE_API_SECRET=your_secret")
        sys.exit(1)
    
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()
    
    print("=" * 70)
    print("MANUAL KITE CONNECT LOGIN")
    print("=" * 70)
    print(f"\n1. Open this URL in your browser:")
    print(f"\n   {login_url}\n")
    print("2. Log in with your Kite credentials:")
    print(f"   - User ID: {os.getenv('KITE_USER_ID', 'YOUR_USER_ID')}")
    print(f"   - Password: ********")
    print(f"   - TOTP from your authenticator app")
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
        sys.exit(1)
    
    # Generate access token
    try:
        kite = KiteConnect(api_key=api_key)
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
        
        # Save inside the shared data directory so local and Docker services
        # use the same token location.
        token_path = Path(os.getenv("DATA_DIR", "data")) / ".access_token"
        token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(token_path, 'w') as f:
            f.write(access_token)
        
        print(f"\nSuccess! Access token saved to {token_path}")
        print(f"  Token: {access_token[:20]}...")
        
        # Test the token
        kite.set_access_token(access_token)
        profile = kite.profile()
        print(f"\n✓ Logged in as: {profile['user_name']} ({profile['user_id']})")
        print(f"  Broker: {profile.get('broker', 'N/A')}")
        print(f"  Email: {profile.get('email', 'N/A')}")
        
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nTroubleshooting:")
        print("  - Make sure you copied the request_token correctly")
        print("  - The request_token is only valid for a few minutes")
        print("  - Try generating a new request_token by visiting the login URL again")
        sys.exit(1)

if __name__ == "__main__":
    main()
