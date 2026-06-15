#!/usr/bin/env python3
"""
garmin_sync_v11.py
------------------
Downloads new running FIT files from Garmin Connect and:
  1. Uploads raw FIT to Dropbox
  2. Uploads raw FIT + dated JSON to Google Drive
  3. Overwrites latest_run.json in Google Drive
  4. Writes latest_run.json to GitHub repo (public raw URL)

Additions in v11:
  - Grade Adjusted Pace (GAP) per lap and overall, using the
    Minetti et al. metabolic cost polynomial (same model used
    by Strava and most serious training platforms).
  - Terrain + fatigue disambiguation is baked into the data
    so every coaching analysis can separate terrain effects
    from fatigue effects on running dynamics.

GAP formula (Minetti et al. 2002):
  metabolic_cost_ratio = 280.5*g^5 - 58.7*g^4 - 76.8*g^3
                         + 51.9*g^2 + 19.6*g + 2.5
  divided by flat cost (2.5) to get the effort multiplier.
  GAP = pace / multiplier  (faster = harder equivalent effort)
  Capped at grade ±40% as the polynomial is only valid in that range.

All pace values from moving time. Elevation from barometric altitude records.

Requirements:
    pip install garminconnect dropbox google-api-python-client google-auth google-auth-oauthlib requests
"""

import os
import io
import json
import struct
import zipfile
import logging
import requests
import base64
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

DROPBOX_FOLDER      = "/Garmin/FIT_Files"
GDRIVE_FOLDER_ID    = "11m5Qr1sbsy5RcKJjXHGaRcc9v1C0HvNZ"
LATEST_RUN_FILENAME = "latest_run.json"

GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "Caesarfisken/Garmin-Fit-sync")
GITHUB_FILE_PATH = "latest_run.json"
GITHUB_BRANCH    = "main"

INITIAL_LOOKBACK_DAYS = 30
STATE_FILE         = Path.home() / ".garmin_sync_state.json"
DROPBOX_TOKEN_FILE = Path.home() / ".dropbox_refresh_token.json"
GDRIVE_TOKEN_FILE  = Path.home() / ".google_oauth_token.json"

ACTIVITY_TYPES  = ["running"]
ATHLETE_ID      = "Athlete_3B974C"
RHR = 37; MHR = 167; HRR = 130

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ── Zones ─────────────────────────────────────────────────────────────────────
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


# ── Grade Adjusted Pace (Minetti et al. 2002) ─────────────────────────────────
def minetti_cost_ratio(grade_pct: float) -> float:
    """
    Returns the metabolic cost ratio relative to flat running.
    grade_pct: grade as percentage (e.g. +5 for 5% uphill, -3 for 3% downhill)
    Polynomial valid for grade -40% to +40%.
    """
    g = max(-0.40, min(0.40, grade_pct / 100.0))  # clamp to valid range
    # Minetti et al. 2002 polynomial
    cost = (280.5 * g**5
            - 58.7 * g**4
            - 76.8 * g**3
            + 51.9 * g**2
            + 19.6 * g
            + 2.5)
    flat_cost = 2.5
    return cost / flat_cost  # ratio > 1 means harder than flat

def compute_gap(pace_min_km: float, grade_pct: float) -> float:
    """
    Returns Grade Adjusted Pace in min/km.
    pace_min_km: actual pace in decimal minutes per km
    grade_pct:   grade as percentage
    GAP = actual_pace / cost_ratio
    (lower GAP = faster equivalent flat effort)
    """
    ratio = minetti_cost_ratio(grade_pct)
    if ratio <= 0: return pace_min_km
    return pace_min_km / ratio

def pace_str(pace_min: float) -> str:
    if not pace_min or pace_min <= 0 or pace_min >= 20: return None
    return "%d:%02d" % (int(pace_min), int((pace_min % 1) * 60))


# ── FIT extraction ────────────────────────────────────────────────────────────
def extract_fit(data: bytes) -> bytes:
    if data[:2] == b'PK':
        log.info("Detected ZIP wrapper — extracting FIT...")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            fit_files = [n for n in zf.namelist() if n.lower().endswith('.fit')]
            if not fit_files:
                raise ValueError("No .fit file found inside ZIP")
            fit_data = zf.read(fit_files[0])
            log.info("Extracted %s (%d bytes)", fit_files[0], len(fit_data))
            return fit_data
    return data


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


# ── Per-record GAP computation ────────────────────────────────────────────────
def compute_record_gap(records, lap_records_list, lap_start_dists):
    """
    Compute GAP for each record using local grade from adjacent altitude readings.
    Returns list of (record_index, gap_min_km) for valid records.
    """
    # We need altitude and speed per record
    # Use a 5-record rolling window for grade smoothing (reduces GPS noise)
    WINDOW = 5
    record_gaps = []
    alts = [r.get(83, None) for r in records]  # field 83 = enhanced_altitude
    dists = [r.get(5, 0)/100 for r in records]

    for i in range(len(records)):
        r = records[i]
        spd_raw = r.get(140, 0)
        if spd_raw < 500: continue
        speed_ms = spd_raw / 1000
        pace_mk = 1000 / speed_ms / 60

        # Grade from windowed altitude difference
        i_start = max(0, i - WINDOW)
        i_end   = min(len(records)-1, i + WINDOW)
        alt_s = alts[i_start]; alt_e = alts[i_end]
        if alt_s is None or alt_e is None: continue
        dist_delta = dists[i_end] - dists[i_start]
        if dist_delta < 10: continue  # too short to compute reliable grade
        grade_pct = ((alt_e - alt_s) / 100 / dist_delta) * 100  # field83/100 = metres

        gap = compute_gap(pace_mk, grade_pct)
        record_gaps.append((i, round(gap, 3), round(grade_pct, 2)))

    return record_gaps


def build_json_summary(raw_download, activity):
    fit_data = extract_fit(raw_download)
    laps, records = parse_fit(fit_data)
    log.info("Parsed %d laps and %d records", len(laps), len(records))

    # Assign records to laps
    lap_start_dist = []
    cum = 0
    for lap in laps:
        lap_start_dist.append(cum)
        cum += lap.get(9, 0) / 100

    def get_lap_idx(rdist):
        for i in range(len(lap_start_dist) - 1, -1, -1):
            if rdist >= lap_start_dist[i]: return i
        return 0

    lap_records = [[] for _ in range(len(laps))]
    for r in records:
        lap_records[get_lap_idx(r.get(5, 0) / 100)].append(r)

    laps_out = []
    for li in range(len(laps)):
        lap = laps[li]; recs = lap_records[li]
        dist = lap.get(9, 0) / 100
        if dist < 50: continue

        # Moving time
        elapsed_s = lap.get(7, 0) / 1000
        moving_s  = lap.get(8, 0) / 1000
        if moving_s <= 0 or moving_s > elapsed_s:
            moving_s = elapsed_s

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

        moving_spd   = dist / moving_s if moving_s > 0 else 0
        pace_min     = 1000 / moving_spd / 60 if moving_spd > 0 else None
        elapsed_spd  = dist / elapsed_s if elapsed_s > 0 else 0
        elapsed_pace = 1000 / elapsed_spd / 60 if elapsed_spd > 0 else None

        # Elevation
        # Field 83 = enhanced_altitude, stored in cm on Garmin Forerunner 570 -> /100 = metres
        # Field 2  = altitude (legacy, not present on this device)
        altitudes = [r[83]/100 for r in recs if r.get(83) not in [None, 0, 65535, 4294967295]]
        elev_gain_m = None; elev_loss_m = None; elev_net_m = None
        grade_pct = None
        if len(altitudes) >= 2:
            gain = 0.0; loss = 0.0
            for i in range(1, len(altitudes)):
                delta = altitudes[i] - altitudes[i-1]
                if delta > 0: gain += delta
                else: loss += abs(delta)
            elev_gain_m = round(gain, 1)
            elev_loss_m = round(loss, 1)
            elev_net_m  = round(altitudes[-1] - altitudes[0], 1)
            if dist > 0:
                grade_pct = round((elev_net_m / dist) * 100, 2)

        # Terrain flag
        terrain_note = None
        if grade_pct is not None:
            if grade_pct <= -1.5:   terrain_note = "net downhill"
            elif grade_pct >= 1.5:  terrain_note = "net uphill"
            else:                   terrain_note = "flat"

        # GAP for this lap
        gap_min = None; gap_str = None
        if pace_min and grade_pct is not None:
            gap_min = compute_gap(pace_min, grade_pct)
            gap_str = pace_str(gap_min)
            gap_min = round(gap_min, 3)

        # Per-record GAP average for the lap (granular)
        rec_gaps = []
        for r in recs:
            spd_raw = r.get(140, 0)
            if spd_raw < 500: continue
            speed_ms = spd_raw / 1000
            p = 1000 / speed_ms / 60
            # Local grade from adjacent records — field 83 enhanced_altitude /100 = metres
            idx = recs.index(r)
            win = 5
            i0 = max(0, idx - win); i1 = min(len(recs)-1, idx + win)
            a0 = recs[i0].get(83, None); a1 = recs[i1].get(83, None)
            d0 = recs[i0].get(5,0)/100; d1 = recs[i1].get(5,0)/100
            if a0 and a1 and a0 not in [0,65535,4294967295] and a1 not in [0,65535,4294967295] and (d1-d0) > 10:
                g_pct = ((a1-a0)/100 / (d1-d0)) * 100
                rec_gaps.append(compute_gap(p, g_pct))
        avg_gap_granular = round(sum(rec_gaps)/len(rec_gaps), 3) if rec_gaps else None
        avg_gap_granular_str = pace_str(avg_gap_granular) if avg_gap_granular else None

        laps_out.append({
            "lap": li + 1,
            "distance_m": round(dist, 1),
            "duration_s": round(elapsed_s, 1),
            "moving_time_s": round(moving_s, 1),
            "pace_min_km": pace_str(pace_min),
            "pace_elapsed_min_km": pace_str(elapsed_pace),
            "pace_decimal": round(pace_min, 3) if pace_min else None,
            "gap_min_km": gap_str,               # GAP from lap-level grade
            "gap_decimal": gap_min,
            "gap_granular_min_km": avg_gap_granular_str,  # GAP averaged from per-record grades
            "gap_granular_decimal": avg_gap_granular,
            "avg_hr_bpm": avg_hr,
            "hr_zone": get_zone(avg_hr) if avg_hr else None,
            "hr_zone_pct": get_zone_pct(avg_hr) if avg_hr else None,
            "cadence_spm": avg_cad,
            "vertical_oscillation_mm": avg_vo,
            "ground_contact_time_ms": avg_gct,
            "vertical_ratio_pct": avg_vr,
            "elevation_gain_m": elev_gain_m,
            "elevation_loss_m": elev_loss_m,
            "elevation_net_m": elev_net_m,
            "grade_pct": grade_pct,
            "terrain": terrain_note,
            "note": "VO from Garmin wrist — true value ~15-20mm lower (STRYD reference)"
        })

    # Overall
    all_hrs  = [r[3] for r in records if r.get(3,255) not in [255,0] and r.get(3,0) < 220]
    all_cads = [r[4]*2 for r in records if r.get(4,255) not in [255,0]]
    all_vos  = [r[39]/10 for r in records if r.get(39,65535) != 65535]
    all_gcts = [r[41]/10 for r in records if r.get(41,65535) != 65535]
    total_dist = records[-1].get(5,0)/100 if records else 0

    total_moving  = sum(lap.get(8,0)/1000 for lap in laps if lap.get(9,0)/100 > 50)
    total_elapsed = sum(lap.get(7,0)/1000 for lap in laps if lap.get(9,0)/100 > 50)
    if total_moving <= 0: total_moving = total_elapsed

    overall_spd  = total_dist / total_moving if total_moving > 0 else 0
    overall_pace = 1000 / overall_spd / 60 if overall_spd > 0 else None

    # Overall GAP — weighted average of per-lap GAP by distance
    gap_laps = [(l["gap_granular_decimal"], l["distance_m"]) for l in laps_out
                if l["gap_granular_decimal"] and l["distance_m"]]
    if gap_laps:
        total_gap_dist = sum(d for _, d in gap_laps)
        weighted_gap   = sum(g * d for g, d in gap_laps) / total_gap_dist
        overall_gap_str = pace_str(weighted_gap)
        overall_gap_dec = round(weighted_gap, 3)
    else:
        overall_gap_str = None; overall_gap_dec = None

    total_gain = round(sum(l["elevation_gain_m"] for l in laps_out if l["elevation_gain_m"] is not None), 1)
    total_loss = round(sum(l["elevation_loss_m"] for l in laps_out if l["elevation_loss_m"] is not None), 1)

    return {
        "meta": {
            "activity_id": activity.get("activityId"),
            "activity_name": activity.get("activityName", ""),
            "start_time": activity.get("startTimeLocal", ""),
            "device": "Garmin Forerunner 570",
            "pace_note": "All pace from moving time (total_timer_time) — matches Garmin Connect display",
            "gap_note": "GAP uses Minetti et al. 2002 polynomial. gap_min_km = lap-level grade. gap_granular_min_km = avg of per-record grades (more accurate on rolling terrain).",
            "elevation_note": "Per-lap elevation from barometric altitude. Use elevation_net_m + terrain flag to separate terrain effects from fatigue effects on dynamics.",
            "sensor_notes": {
                "vertical_oscillation": "Garmin wrist — reads ~15-20mm higher than STRYD ground truth",
                "cadence": "Reliable, cross-validated with STRYD",
                "gct": "Garmin sensor — use for trends, STRYD for absolute values",
                "pace_hr": "GPS pace and optical HR — reliable"
            }
        },
        "athlete": {
            "id": ATHLETE_ID,
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
            "avg_pace_min_km": pace_str(overall_pace),
            "avg_gap_min_km": overall_gap_str,
            "avg_gap_decimal": overall_gap_dec,
            "avg_hr_bpm": round(sum(all_hrs)/len(all_hrs), 1) if all_hrs else None,
            "avg_cadence_spm": round(sum(all_cads)/len(all_cads), 1) if all_cads else None,
            "avg_vo_mm_garmin": round(sum(all_vos)/len(all_vos), 1) if all_vos else None,
            "avg_gct_ms": round(sum(all_gcts)/len(all_gcts), 1) if all_gcts else None,
            "total_laps": len(laps_out),
            "total_elevation_gain_m": total_gain,
            "total_elevation_loss_m": total_loss
        },
        "baseline_reference": {
            "aerobic_pace":  {"pace": "4:52/km", "zone": "Z2", "cadence_spm": 156, "gct_ms": 278, "vo_mm_garmin": 102},
            "moderate_pace": {"pace": "4:32/km", "zone": "Z3", "cadence_spm": 160, "gct_ms": 266, "vo_mm_garmin": 103},
            "stryd_easy":    {"pace": "5:06/km", "cadence_spm": 150, "gct_ms": 297, "vo_mm_stryd": 87.5},
            "stryd_tempo":   {"pace": "4:21/km", "cadence_spm": 159, "gct_ms": 264, "vo_mm_stryd": 83.4},
            "leg_spring_stiffness_kn_m": 8.8,
            "form_power_pct": 32.2
        },
        "laps": laps_out
    }


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f: return json.load(f)
    return {"synced_ids": [], "last_sync": None}

def save_state(state):
    state["last_sync"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
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
    activities = client.get_activities_by_date(since_date, datetime.now().strftime("%Y-%m-%d"))
    if ACTIVITY_TYPES:
        activities = [a for a in activities if a.get("activityType", {}).get("typeKey", "").lower() in ACTIVITY_TYPES]
    log.info("Found %d running activities.", len(activities))
    return activities

def download_fit(client, activity_id):
    log.info("Downloading FIT for activity %s ...", activity_id)
    return client.download_activity(activity_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL)


# ── Dropbox ───────────────────────────────────────────────────────────────────
def connect_dropbox():
    with open(DROPBOX_TOKEN_FILE) as f: t = json.load(f)
    dbx = dropbox.Dropbox(oauth2_refresh_token=t["refresh_token"], app_key=t["app_key"], app_secret=t["app_secret"])
    account = dbx.users_get_current_account()
    log.info("Dropbox connected as %s", account.name.display_name)
    return dbx

def ensure_dropbox_folder(dbx, folder_path):
    try: dbx.files_get_metadata(folder_path)
    except ApiError: dbx.files_create_folder_v2(folder_path)

def upload_to_dropbox(dbx, data, filename):
    path = f"{DROPBOX_FOLDER}/{filename}"
    try:
        dbx.files_get_metadata(path)
        log.info("Dropbox: already exists: %s", filename)
        return False
    except ApiError: pass
    dbx.files_upload(data, path, mode=WriteMode.add)
    log.info("Dropbox: uploaded %s", filename)
    return True


# ── Google Drive ──────────────────────────────────────────────────────────────
def connect_gdrive():
    with open(GDRIVE_TOKEN_FILE) as f: token_data = json.load(f)
    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_data.get("client_id"),
        client_secret=token_data.get("client_secret"),
        scopes=token_data.get("scopes")
    )
    if creds.expired and creds.refresh_token:
        log.info("Refreshing Google token ...")
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(GDRIVE_TOKEN_FILE, "w") as f: json.dump(token_data, f, indent=2)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    log.info("Google Drive connected.")
    return service

def get_file_id(service, filename):
    results = service.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id, name)"
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None

def upload_to_gdrive(service, data, filename, mimetype="application/octet-stream"):
    existing_id = get_file_id(service, filename)
    media = MediaInMemoryUpload(data, mimetype=mimetype)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        log.info("Google Drive: updated %s", filename)
        return existing_id
    else:
        file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
        f = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        log.info("Google Drive: uploaded %s", filename)
        return f.get("id")

def make_file_public(service, file_id):
    service.permissions().create(fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()


# ── GitHub ────────────────────────────────────────────────────────────────────
def push_to_github(json_bytes):
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set — skipping GitHub push.")
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    resp = requests.get(url, headers=headers)
    sha = resp.json().get("sha") if resp.status_code == 200 else None
    payload = {
        "message": f"Update latest_run.json [{datetime.now().strftime('%Y-%m-%d %H:%M')}]",
        "content": base64.b64encode(json_bytes).decode("utf-8"),
        "branch": GITHUB_BRANCH
    }
    if sha: payload["sha"] = sha
    resp = requests.put(url, headers=headers, json=payload)
    if resp.status_code in [200, 201]:
        raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{GITHUB_FILE_PATH}"
        log.info("GitHub: latest_run.json updated → %s", raw_url)
        return raw_url
    else:
        log.error("GitHub push failed: %s %s", resp.status_code, resp.text)
        return None


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

    activities = sorted(activities, key=lambda a: a.get("startTimeLocal", ""))

    github_raw_url = None
    new_count = 0

    for activity in activities:
        activity_id = activity.get("activityId")
        if activity_id in synced_ids:
            log.info("Already synced: %s", activity_id)
            continue
        try:
            raw_download  = download_fit(garmin, activity_id)
            fit_data      = extract_fit(raw_download)
            fit_filename  = build_filename(activity, "fit")
            json_filename = build_filename(activity, "json")

            upload_to_dropbox(dbx, fit_data, fit_filename)
            upload_to_gdrive(gdrive, fit_data, fit_filename)

            summary    = build_json_summary(raw_download, activity)
            json_bytes = json.dumps(summary, indent=2).encode("utf-8")

            log.info("Laps: %d | Pace: %s | GAP: %s | Elev +%s/-%s m",
                     len(summary.get("laps",[])),
                     summary["overall"]["avg_pace_min_km"],
                     summary["overall"]["avg_gap_min_km"],
                     summary["overall"]["total_elevation_gain_m"],
                     summary["overall"]["total_elevation_loss_m"])

            upload_to_gdrive(gdrive, json_bytes, json_filename, "application/json")
            latest_id = upload_to_gdrive(gdrive, json_bytes, LATEST_RUN_FILENAME, "application/json")
            make_file_public(gdrive, latest_id)
            github_raw_url = push_to_github(json_bytes)

            new_count += 1
            synced_ids.add(activity_id)

        except Exception as e:
            log.error("Failed to sync activity %s: %s", activity_id, e)
            import traceback; traceback.print_exc()

    state["synced_ids"] = list(synced_ids)
    if github_raw_url:
        state["github_raw_url"] = github_raw_url
    save_state(state)

    if github_raw_url:
        log.info("=" * 60)
        log.info("LATEST RUN URL: %s", github_raw_url)
        log.info("=" * 60)

    log.info("Sync complete. %d new activity/activities processed.", new_count)


if __name__ == "__main__":
    sync()
