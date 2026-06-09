#!/usr/bin/env python3
"""
garmin_sync_v5.py
-----------------
Downloads new running FIT files from Garmin Connect and:
  1. Uploads raw FIT to Dropbox:      /Garmin/FIT_Files/
  2. Uploads raw FIT to Google Drive: personal account (visible in My Drive)
  3. Parses FIT and saves JSON summary to Google Drive for Claude to read

Uses personal Google OAuth token — files appear directly in your Drive.

Requirements:
    pip install garminconnect dropbox google-api-python-client google-auth google-auth-oauthlib
"""

import os
import json
import struct
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
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
except ImportError:
    raise SystemExit("Missing: pip install google-api-python-client google-auth")

# ── Configuration ─────────────────────────────────────────────────────────────
GARMIN_EMAIL    = os.environ.get("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD", "")

DROPBOX_FOLDER   = "/Garmin/FIT_Files"
GDRIVE_FOLDER_ID = "11m5Qr1sbsy5RcKJjXHGaRcc9v1C0HvNZ"

INITIAL_LOOKBACK_DAYS = 30
STATE_FILE             = Path.home() / ".garmin_sync_state.json"
DROPBOX_TOKEN_FILE     = Path.home() / ".dropbox_refresh_token.json"
GDRIVE_TOKEN_FILE      = Path.home() / ".google_oauth_token.json"

ACTIVITY_TYPES = ["running"]

# Karvonen zones for Julius
RHR = 37; MHR = 167; HRR = 130

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ── Heart rate zones ──────────────────────────────────────────────────────────
def get_zone(hr):
    if not hr or hr <= 0: return "-"
    pct = (hr - RHR) / HRR * 100
    if pct < 60: return "Z1"
    if pct < 70: return "Z2"
    if pct < 80: return "Z3"
    if pct < 90: return "Z4"
    return "Z5"

def get_zone_pct(hr):
    if not hr or hr <= 0: return 0
    return round((hr - RHR) / HRR * 100, 1)


# ── FIT parser ────────────────────────────────────────────────────────────────
def parse_fit(raw):
    header_size = raw[0]; pos = header_size
    local_msg_defs = {}; laps = []; records = []
    while pos < len(raw) - 2:
        if pos >= len(raw): break
        record_header = raw[pos]; pos += 1
        if (record_header & 0x80) != 0: continue
        is_def = (record_header & 0x40) != 0
        has_dev = (record_header & 0x20) != 0
        local_num = record_header & 0x0F
        if is_def:
            pos += 1; arch = raw[pos]; pos += 1
            endian = '>' if arch == 1 else '<'
            global_num = struct.unpack(endian + 'H', raw[pos:pos+2])[0]; pos += 2
            num_fields = raw[pos]; pos += 1
            fields = []
            for _ in range(num_fields):
                fn = raw[pos]; pos += 1; fs = raw[pos]; pos += 1; bt = raw[pos]; pos += 1
                fields.append((fn, fs, bt))
            dev_fields = []
            if has_dev:
                num_dev = raw[pos]; pos += 1
                for _ in range(num_dev):
                    fn = raw[pos]; pos += 1; fs = raw[pos]; pos += 1; di = raw[pos]; pos += 1
                    dev_fields.append((fn, fs, di))
            local_msg_defs[local_num] = (global_num, endian, fields, dev_fields)
        else:
            if local_num not in local_msg_defs: break
            global_num, endian, fields, dev_fields = local_msg_defs[local_num]
            msg_data = {}
            for field_num, field_size, base_type in fields:
                raw_bytes = raw[pos:pos+field_size]; pos += field_size
                bt = base_type & 0x9F
                try:
                    if bt == 0x00: val = raw_bytes[0]
                    elif bt == 0x01: val = struct.unpack('b', raw_bytes)[0]
                    elif bt == 0x02: val = raw_bytes[0]
                    elif bt == 0x83: val = struct.unpack(endian+'h', raw_bytes)[0]
                    elif bt == 0x84: val = struct.unpack(endian+'H', raw_bytes)[0]
                    elif bt == 0x85: val = struct.unpack(endian+'i', raw_bytes)[0]
                    elif bt == 0x86: val = struct.unpack(endian+'I', raw_bytes)[0]
                    elif bt == 0x07: val = raw_bytes.decode('utf-8', errors='replace').rstrip('\x00')
                    elif bt == 0x88: val = struct.unpack(endian+'f', raw_bytes)[0]
                    elif bt == 0x89: val = struct.unpack(endian+'d', raw_bytes)[0]
                    elif bt == 0x0A: val = raw_bytes[0]
                    elif bt == 0x8B: val = struct.unpack(endian+'H', raw_bytes)[0]
                    elif bt == 0x8C: val = struct.unpack(endian+'I', raw_bytes)[0]
                    else: val = int.from_bytes(raw_bytes, 'little')
                except: val = None
                msg_data[field_num] = val
            for _, fs, _ in dev_fields: pos += fs
            if global_num == 19: laps.append(msg_data)
            elif global_num == 20: records.append(msg_data)
    return laps, records


def build_json_summary(fit_data, activity):
    laps, records = parse_fit(fit_data)

    lap_start_dist = []
    cum = 0
    for lap in laps:
        lap_start_dist.append(cum)
        cum += lap.get(9, 0) / 100

    def get_lap_idx(rdist):
        for i in range(len(lap_start_dist) - 1, -1, -1):
            if rdist >= lap_start_dist[i]:
                return i
        return 0

    lap_records = [[] for _ in range(len(laps))]
    for r in records:
        lap_records[get_lap_idx(r.get(5, 0) / 100)].append(r)

    laps_out = []
    for li in range(len(laps)):
        lap = laps[li]
        recs = lap_records[li]
        dist = lap.get(9, 0) / 100
        elapsed = lap.get(7, 0) / 1000
        if dist < 50: continue

        hrs  = [r[3] for r in recs if r.get(3,255) not in [255,0] and r.get(3,0) < 220]
        cads = [r[4]*2 for r in recs if r.get(4,255) not in [255,0]]
        vos  = [r[39]/10 for r in recs if r.get(39,65535) != 65535]
        gcts = [r[41]/10 for r in recs if r.get(41,65535) != 65535]
        vrs  = [r[53]/10 for r in recs if r.get(53,65535) not in [65535,0]]

        avg_hr  = round(sum(hrs)/len(hrs), 1) if hrs else None
        avg_cad = round(sum(cads)/len(cads), 1) if cads else None
        avg_vo  = round(sum(vos)/len(vos), 1) if vos else None
        avg_gct = round(sum(gcts)/len(gcts), 1) if gcts else None
        avg_vr  = round(sum(vrs)/len(vrs), 1) if vrs else None

        lap_spd = dist / elapsed if elapsed > 0 else 0
        pace_min = 1000 / lap_spd / 60 if lap_spd > 0 else None
        pace_str = "%d:%02d" % (int(pace_min), int((pace_min % 1) * 60)) if pace_min and pace_min < 20 else None

        laps_out.append({
            "lap": li + 1,
            "distance_m": round(dist, 1),
            "duration_s": round(elapsed, 1),
            "pace_min_km": pace_str,
            "pace_decimal": round(pace_min, 3) if pace_min else None,
            "avg_hr_bpm": avg_hr,
            "hr_zone": get_zone(avg_hr) if avg_hr else None,
            "hr_zone_pct": get_zone_pct(avg_hr) if avg_hr else None,
            "cadence_spm": avg_cad,
            "vertical_oscillation_mm": avg_vo,
            "ground_contact_time_ms": avg_gct,
            "vertical_ratio_pct": avg_vr,
            "note": "VO from Garmin wrist — true value ~15-20mm lower (STRYD reference)"
        })

    all_hrs  = [r[3] for r in records if r.get(3,255) not in [255,0] and r.get(3,0) < 220]
    all_cads = [r[4]*2 for r in records if r.get(4,255) not in [255,0]]
    all_vos  = [r[39]/10 for r in records if r.get(39,65535) != 65535]
    all_gcts = [r[41]/10 for r in records if r.get(41,65535) != 65535]
    total_dist = records[-1].get(5,0)/100 if records else 0
    total_elapsed = sum(lap.get(7,0)/1000 for lap in laps if lap.get(9,0)/100 > 50)
    overall_spd = total_dist / total_elapsed if total_elapsed > 0 else 0
    overall_pace = 1000 / overall_spd / 60 if overall_spd > 0 else None
    overall_pace_str = "%d:%02d" % (int(overall_pace), int((overall_pace%1)*60)) if overall_pace else None

    return {
        "meta": {
            "activity_id": activity.get("activityId"),
            "activity_name": activity.get("activityName", ""),
            "start_time": activity.get("startTimeLocal", ""),
            "device": "Garmin Forerunner 570",
            "sensor_notes": {
                "vertical_oscillation": "Garmin wrist — reads ~15-20mm higher than STRYD ground truth",
                "cadence": "Reliable, cross-validated with STRYD",
                "gct": "Garmin sensor — use for trends, STRYD for absolute values",
                "pace_hr": "GPS pace and optical HR — reliable"
            }
        },
        "athlete": {
            "name": "Julius Schmidt",
            "rhr_bpm": RHR, "mhr_bpm": MHR, "hrr_bpm": HRR, "vo2max": 57,
            "zones": {
                "Z1": "102-115 bpm (50-60% HRR)",
                "Z2": "115-128 bpm (60-70% HRR)",
                "Z3": "128-141 bpm (70-80% HRR)",
                "Z4": "141-154 bpm (80-90% HRR)",
                "Z5": "154-167 bpm (90-100% HRR)"
            }
        },
        "overall": {
            "total_distance_km": round(total_dist/1000, 2),
            "avg_pace_min_km": overall_pace_str,
            "avg_hr_bpm": round(sum(all_hrs)/len(all_hrs), 1) if all_hrs else None,
            "avg_cadence_spm": round(sum(all_cads)/len(all_cads), 1) if all_cads else None,
            "avg_vo_mm_garmin": round(sum(all_vos)/len(all_vos), 1) if all_vos else None,
            "avg_gct_ms": round(sum(all_gcts)/len(all_gcts), 1) if all_gcts else None,
            "total_laps": len(laps_out)
        },
        "baseline_reference": {
            "easy_pace": {"pace": "4:52/km", "zone": "Z1", "cadence_spm": 156, "gct_ms": 278, "vo_mm_garmin": 102},
            "moderate_pace": {"pace": "4:32/km", "zone": "Z3", "cadence_spm": 160, "gct_ms": 266, "vo_mm_garmin": 103},
            "stryd_easy": {"pace": "5:06/km", "cadence_spm": 150, "gct_ms": 297, "vo_mm_stryd": 87.5},
            "stryd_tempo": {"pace": "4:21/km", "cadence_spm": 159, "gct_ms": 264, "vo_mm_stryd": 83.4},
            "leg_spring_stiffness_kn_m": 8.8,
            "form_power_pct": 32.2
        },
        "laps": laps_out
    }


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

def upload_to_dropbox(dbx, data, filename):
    path = f"{DROPBOX_FOLDER}/{filename}"
    try:
        dbx.files_get_metadata(path)
        log.info("Dropbox: already exists: %s", filename)
        return False
    except ApiError:
        pass
    dbx.files_upload(data, path, mode=WriteMode.add)
    log.info("Dropbox: uploaded %s", filename)
    return True


# ── Google Drive (personal OAuth) ────────────────────────────────────────────
def connect_gdrive():
    if not GDRIVE_TOKEN_FILE.exists():
        raise SystemExit(f"Google token not found: {GDRIVE_TOKEN_FILE}")
    with open(GDRIVE_TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes")
    )

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        log.info("Refreshing Google token ...")
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(GDRIVE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f, indent=2)

    log.info("Connecting to Google Drive ...")
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    log.info("Google Drive connected.")
    return service

def file_exists_in_drive(service, filename):
    results = service.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id, name)"
    ).execute()
    return len(results.get("files", [])) > 0

def upload_to_gdrive(service, data, filename, mimetype="application/octet-stream"):
    if file_exists_in_drive(service, filename):
        log.info("Google Drive: already exists: %s", filename)
        return False
    media = MediaInMemoryUpload(data, mimetype=mimetype)
    file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
    service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    log.info("Google Drive: uploaded %s", filename)
    return True


# ── Filename ──────────────────────────────────────────────────────────────────
def build_filename(activity, ext="fit"):
    activity_id   = activity.get("activityId", "unknown")
    activity_type = activity.get("activityType", {}).get("typeKey", "activity").lower()
    start_time    = activity.get("startTimeLocal", "")
    try:
        dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        timestamp = dt.strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        timestamp = "unknown_time"
    return f"{timestamp}_{activity_id}_{activity_type}.{ext}"


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
        log.info("First run — lookback from %s", since_date)

    garmin = connect_garmin()
    dbx    = connect_dropbox()
    gdrive = connect_gdrive()
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
            fit_data      = download_fit(garmin, activity_id)
            fit_filename  = build_filename(activity, "fit")
            json_filename = build_filename(activity, "json")

            upload_to_dropbox(dbx, fit_data, fit_filename)
            upload_to_gdrive(gdrive, fit_data, fit_filename)

            log.info("Parsing FIT → JSON summary ...")
            summary    = build_json_summary(fit_data, activity)
            json_bytes = json.dumps(summary, indent=2).encode("utf-8")
            upload_to_gdrive(gdrive, json_bytes, json_filename, "application/json")
            log.info("JSON uploaded: %s", json_filename)

            new_count += 1
            synced_ids.add(activity_id)

        except Exception as e:
            log.error("Failed to sync activity %s: %s", activity_id, e)

    state["synced_ids"] = list(synced_ids)
    save_state(state)
    log.info("Sync complete. %d new activity/activities processed.", new_count)


if __name__ == "__main__":
    sync()
