#!/usr/bin/env python3
"""
garmin_to_dropbox_and_drive_v3.py
----------------------------------
Downloads new running FIT files from Garmin Connect and uploads to:
  - Dropbox:      /Garmin/FIT_Files/
  - Google Drive: folder ID 11m5Qr1sbsy5RcKJjXHGaRcc9v1C0HvNZ

Requirements:
    pip install garminconnect dropbox google-api-python-client google-auth
"""

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

try:
    import garminconnect
except ImportError:
    raise SystemExit("Missing: pip install garminconnect")

try:
    import dropbox
    from dropbox.exceptions import ApiError
    from dropbox.files import WriteMode
except ImportError:
    raise SystemExit("Missing: pip install dropbox")

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload
    from google.oauth2 import service_account
except ImportError:
    raise SystemExit("Missing: pip install google-api-python-client google-auth")

# ── Configuration ─────────────────────────────────────────────────────────────
GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

DROPBOX_FOLDER      = "/Garmin/FIT_Files"
GDRIVE_FOLDER_ID    = "11m5Qr1sbsy5RcKJjXHGaRcc9v1C0HvNZ"

INITIAL_LOOKBACK_DAYS = 30
STATE_FILE             = Path.home() / ".garmin_sync_state.json"
DROPBOX_TOKEN_FILE     = Path.home() / ".dropbox_refresh_token.json"
GDRIVE_CREDS_FILE      = Path.home() / ".google_credentials.json"

ACTIVITY_TYPES = ["running"]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"synced_ids": [], "last_sync": None}

def save_state(state):
    state["last_sync"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved.")


# ── Garmin ────────────────────────────────────────────────────────────────────
def connect_garmin():
    log.info("Connecting to Garmin Connect as %s ...", GARMIN_EMAIL)
    client = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    log.info("Garmin login successful.")
    return client

def get_recent_activities(client, since_date):
    log.info("Fetching activities since %s ...", since_date)
    activities = client.get_activities_by_date(
        since_date, datetime.now().strftime("%Y-%m-%d")
    )
    if ACTIVITY_TYPES:
        activities = [
            a for a in activities
            if a.get("activityType", {}).get("typeKey", "").lower() in ACTIVITY_TYPES
        ]
    log.info("Found %d running activities.", len(activities))
    return activities

def download_fit(client, activity_id):
    log.info("Downloading FIT for activity %s ...", activity_id)
    return client.download_activity(
        activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
    )


# ── Dropbox ───────────────────────────────────────────────────────────────────
def connect_dropbox():
    if not DROPBOX_TOKEN_FILE.exists():
        raise SystemExit(f"Dropbox token not found: {DROPBOX_TOKEN_FILE}")
    with open(DROPBOX_TOKEN_FILE) as f:
        t = json.load(f)
    log.info("Connecting to Dropbox ...")
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=t["refresh_token"],
        app_key=t["app_key"],
        app_secret=t["app_secret"]
    )
    account = dbx.users_get_current_account()
    log.info("Dropbox connected as %s", account.name.display_name)
    return dbx

def ensure_dropbox_folder(dbx, folder_path):
    try:
        dbx.files_get_metadata(folder_path)
    except ApiError:
        dbx.files_create_folder_v2(folder_path)
        log.info("Created Dropbox folder: %s", folder_path)

def upload_to_dropbox(dbx, fit_data, filename):
    path = f"{DROPBOX_FOLDER}/{filename}"
    try:
        dbx.files_get_metadata(path)
        log.info("Dropbox: already exists, skipping: %s", filename)
        return False
    except ApiError:
        pass
    dbx.files_upload(fit_data, path, mode=WriteMode.add)
    log.info("Dropbox: uploaded %s", filename)
    return True


# ── Google Drive ──────────────────────────────────────────────────────────────
def connect_gdrive():
    if not GDRIVE_CREDS_FILE.exists():
        raise SystemExit(f"Google credentials not found: {GDRIVE_CREDS_FILE}")
    log.info("Connecting to Google Drive ...")
    creds = service_account.Credentials.from_service_account_file(
        str(GDRIVE_CREDS_FILE),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    log.info("Google Drive connected.")
    return service

def file_exists_in_drive(service, filename):
    results = service.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id, name)"
    ).execute()
    return len(results.get("files", [])) > 0

def upload_to_gdrive(service, fit_data, filename):
    if file_exists_in_drive(service, filename):
        log.info("Google Drive: already exists, skipping: %s", filename)
        return False
    media = MediaInMemoryUpload(fit_data, mimetype="application/octet-stream")
    file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
    service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    log.info("Google Drive: uploaded %s", filename)
    return True


# ── Filename ──────────────────────────────────────────────────────────────────
def build_filename(activity):
    activity_id   = activity.get("activityId", "unknown")
    activity_type = activity.get("activityType", {}).get("typeKey", "activity").lower()
    start_time    = activity.get("startTimeLocal", "")
    try:
        dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        timestamp = dt.strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        timestamp = "unknown_time"
    return f"{timestamp}_{activity_id}_{activity_type}.fit"


# ── Main ──────────────────────────────────────────────────────────────────────
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

    garmin  = connect_garmin()
    dbx     = connect_dropbox()
    gdrive  = connect_gdrive()

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

            db_uploaded = upload_to_dropbox(dbx, fit_data, filename)
            gd_uploaded = upload_to_gdrive(gdrive, fit_data, filename)

            if db_uploaded or gd_uploaded:
                new_count += 1

            synced_ids.add(activity_id)

        except Exception as e:
            log.error("Failed to sync activity %s: %s", activity_id, e)

    state["synced_ids"] = list(synced_ids)
    save_state(state)
    log.info("Sync complete. %d new file(s) uploaded.", new_count)


if __name__ == "__main__":
    sync()
