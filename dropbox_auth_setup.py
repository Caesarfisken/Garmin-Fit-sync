#!/usr/bin/env python3
"""
dropbox_auth_setup.py
---------------------
Run this ONCE to authorise Dropbox and save a permanent refresh token.
After this you never need to log in to Dropbox again.

Usage:
    python dropbox_auth_setup.py
"""

import json
from pathlib import Path
import dropbox
from dropbox import DropboxOAuth2FlowNoRedirect

# ── Paste your app credentials here ──────────────────────────────────────────
APP_KEY    = "778qu4t208083zt"
APP_SECRET = "nq3bk7r8xlrp0tv"
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_FILE = Path.home() / ".dropbox_refresh_token.json"

def main():
    auth_flow = DropboxOAuth2FlowNoRedirect(
        APP_KEY,
        APP_SECRET,
        token_access_type="offline"   # <-- this is what gives a refresh token
    )

    authorize_url = auth_flow.start()

    print("\n" + "="*60)
    print("DROPBOX AUTHORISATION")
    print("="*60)
    print("\n1. Open this URL in your browser:\n")
    print(f"   {authorize_url}\n")
    print("2. Click 'Allow' when prompted")
    print("3. Copy the authorisation code shown")
    print("="*60 + "\n")

    auth_code = input("Paste the authorisation code here: ").strip()

    oauth_result = auth_flow.finish(auth_code)

    # Save the refresh token to disk
    token_data = {
        "refresh_token": oauth_result.refresh_token,
        "app_key":       APP_KEY,
        "app_secret":    APP_SECRET
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\n✓ Refresh token saved to {TOKEN_FILE}")
    print("  You can now run garmin_to_dropbox.py normally.\n")

    # Quick verification
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=oauth_result.refresh_token,
        app_key=APP_KEY,
        app_secret=APP_SECRET
    )
    account = dbx.users_get_current_account()
    print(f"✓ Connected to Dropbox as: {account.name.display_name}")
    print("  Setup complete!\n")

if __name__ == "__main__":
    main()
