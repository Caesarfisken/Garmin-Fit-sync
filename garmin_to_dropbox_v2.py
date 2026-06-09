#!/usr/bin/env python3
"""
garmin_to_dropbox.py
--------------------
Automatically downloads new running FIT files from Garmin Connect
and uploads them to a Dropbox folder.

Requirements:
    pip install garminconnect dropbox

First-time setup:
    1. Run dropbox_auth_setup.py once to authorise Dropbox
    2. Set GARMIN_EMAIL and GARMIN_PASSWORD as environment variables
    3. Run this script manually to verify, then schedule via Task Scheduler

Scheduling (Windows Task Scheduler — daily at 8am):
    See SETUP_README.md for full instructions
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

try:
    import garminconnect
except ImportError:
    raise SystemExit("Missing dependency: pip install garminconnect")

try:
    import dropbox
    from dropbox.exceptions import ApiError
    from dropbox.files import WriteMode
except ImportError:
    raise SystemExit("Missing dependency: pip install dropbox")

# ── Configuration ─────────────────────────────────────────────────────────────
GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL",    "your_garmin_email@example.com")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "your_garmin_password")

# Dropbox folder where FIT files will be saved
DROPBOX_FOLDER  = "/Garmin/FIT_Files"

# How many days back to look on first run
INITIAL_LOOKBACK_DAYS = 30

# Local state file tracking uploaded activity IDs
STATE_FILE       = Path.home() / ".garmin_sync_state.json"

# Dropbox refresh token file (created by dropbox_auth_setup.py)
DROPBOX_TOKEN_FILE = Path.home() / ".dropbox_refresh_token.json"

# Activity types to sync
ACTIVITY_TYPES = ["running"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ── State management ──────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"synced_ids": [], "last_sync": None}


def save_state(state: dict):
    state["last_sync"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved to %s", STATE_FILE)


# ── Garmin ────────────────────────────────────────────────────────────────────
def connect_garmin() -> garminconnect.Garmin:
    log.info("Connecting to Garmin Connect as %s ...", GARMIN_EMAIL)
    client = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    log.info("Garmin login successful.")
    return client


def get_recent_activities(client, since_date: str) -> list:
    log.info("Fetching activities since %s ...", since_date)
    activities = client.get_activities_by_date(
        since_date,
        datetime.now().strftime("%Y-%m-%d")
    )
    if ACTIVITY_TYPES:
        activities = [
            a for a in activities
            if a.get("activityType", {}).get("typeKey", "").lower() in ACTIVITY_TYPES
        ]
        log.info("Filtered to %d running activities.", len(activities))
    else:
        log.info("Found %d activities total.", len(activities))
    return activities


def download_fit(client, activity_id: int) -> bytes:
    log.info("Downloading FIT for activity %s ...", activity_id)
    return client.download_activity(
        activity_id,
        dl_fmt=client.ActivityDownloadFormat.ORIGINAL
    )


# ── Dropbox ───────────────────────────────────────────────────────────────────
def connect_dropbox() -> dropbox.Dropbox:
    """Connect using saved refresh token — never expires."""
    if not DROPBOX_TOKEN_FILE.exists():
        raise SystemExit(
            f"Dropbox token file not found: {DROPBOX_TOKEN_FILE}\n"
            "Please run dropbox_auth_setup.py first."
        )

    with open(DROPBOX_TOKEN_FILE) as f:
        token_data = json.load(f)

    log.info("Connecting to Dropbox ...")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=token_data["refresh_token"],
        app_key=token_data["app_key"],
        app_secret=token_data["app_secret"]
    )
    account = dbx.users_get_current_account()
    log.info("Dropbox connected as %s", account.name.display_name)
    return dbx


def ensure_dropbox_folder(dbx: dropbox.Dropbox, folder_path: str):
    try:
        dbx.files_get_metadata(folder_path)
    except ApiError:
        log.info("Creating Dropbox folder: %s", folder_path)
        dbx.files_create_folder_v2(folder_path)


def upload_to_dropbox(dbx: dropbox.Dropbox, fit_data: bytes, filename: str) -> bool:
    dropbox_path = f"{DROPBOX_FOLDER}/{filename}"
    try:
        dbx.files_get_metadata(dropbox_path)
        log.info("Already exists in Dropbox, skipping: %s", filename)
        return False
    except ApiError:
        pass

    log.info("Uploading %s (%d bytes) ...", filename, len(fit_data))
    dbx.files_upload(fit_data, dropbox_path, mode=WriteMode.add)
    log.info("Uploaded: %s", dropbox_path)
    return True


# ── Filename builder ──────────────────────────────────────────────────────────
def build_filename(activity: dict) -> str:
    activity_id   = activity.get("activityId", "unknown")
    activity_type = activity.get("activityType", {}).get("typeKey", "activity").lower()
    start_time_raw = activity.get("startTimeLocal", "")
    try:
        dt = datetime.strptime(start_time_raw, "%Y-%m-%d %H:%M:%S")
        timestamp = dt.strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        timestamp = "unknown_time"
    return f"{timestamp}_{activity_id}_{activity_type}.fit"


# ── Main sync ─────────────────────────────────────────────────────────────────
def sync():
    state = load_state()
    synced_ids = set(state.get("synced_ids", []))

    last_sync = state.get("last_sync")
    if last_sync:
        since_date = (datetime.fromisoformat(last_sync) - timedelta(days=1)).strftime("%Y-%m-%d")
        log.info("Incremental sync from %s", since_date)
    else:
        since_date = (datetime.now() - timedelta(days=INITIAL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        log.info("First run — full lookback from %s", since_date)

    garmin = connect_garmin()
    dbx    = connect_dropbox()
    ensure_dropbox_folder(dbx, DROPBOX_FOLDER)

    activities = get_recent_activities(garmin, since_date)

    if not activities:
        log.info("No new activities found.")
        save_state(state)
        return

    new_count = 0
    for activity in activities:
        activity_id = activity.get("activityId")
        if activity_id in synced_ids:
            log.info("Already synced: %s", activity_id)
            continue
        try:
            fit_data = download_fit(garmin, activity_id)
            filename = build_filename(activity)
            uploaded = upload_to_dropbox(dbx, fit_data, filename)
            if uploaded:
                new_count += 1
            synced_ids.add(activity_id)
        except Exception as e:
            log.error("Failed to sync activity %s: %s", activity_id, e)

    state["synced_ids"] = list(synced_ids)
    save_state(state)
    log.info("Sync complete. %d new file(s) uploaded.", new_count)


if __name__ == "__main__":
    sync()
