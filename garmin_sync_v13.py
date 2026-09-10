#!/usr/bin/env python3
"""
garmin_sync_v13.py
-------------------
Downloads new running FIT files from Garmin Connect and:
  1. Uploads raw FIT to Dropbox
  2. Uploads raw FIT + dated JSON to Google Drive
  3. Overwrites latest_run.json in Google Drive + GitHub (RUNNING ONLY)
  4. NEW: Pulls ALL activity types (30-day window) + daily health
     metrics (RHR, HRV, Garmin Training Status, Garmin Training Load,
     VO2max trend) into a separate training_load.json for coach context.

latest_run.json remains running-only, unchanged in structure.
training_load.json is new — broader context, not lap-level detail.

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
LATEST_RUN_FILENAME     = "latest_run.json"
TRAINING_LOAD_FILENAME  = "training_load.json"

GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "Caesarfisken/Garmin-Fit-sync")
GITHUB_BRANCH    = "main"
GITHUB_LATEST_RUN_PATH    = "latest_run.json"
GITHUB_TRAINING_LOAD_PATH = "training_load.json"

INITIAL_LOOKBACK_DAYS   = 30
TRAINING_LOAD_WINDOW_D  = 30

STATE_FILE         = Path.home() / ".garmin_sync_state.json"
DROPBOX_TOKEN_FILE = Path.home() / ".dropbox_refresh_token.json"
GDRIVE_TOKEN_FILE  = Path.home() / ".google_oauth_token.json"

RUNNING_ACTIVITY_TYPES = ["running"]
ATHLETE_ID = "Athlete_3B974C"
MHR = 171
RHR_FALLBACK = 37

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


# ── Live RHR ──────────────────────────────────────────────────────────────────
def fetch_resting_hr(client) -> int:
    try:
        readings = []
        d = datetime.now()
        for _ in range(7):
            date_str = d.strftime("%Y-%m-%d")
            try:
                stats = client.get_stats(date_str)
                rhr = stats.get("restingHeartRate")
                if rhr and 30 <= rhr <= 80:
                    readings.append(rhr)
            except Exception:
                pass
            d -= timedelta(days=1)
        if readings:
            avg_rhr = round(sum(readings) / len(readings))
            log.info("Live RHR (7-day avg, %d readings): %d bpm", len(readings), avg_rhr)
            return avg_rhr
        log.warning("No valid RHR readings — using fallback %d bpm", RHR_FALLBACK)
        return RHR_FALLBACK
    except Exception as e:
        log.warning("RHR fetch failed (%s) — using fallback %d bpm", e, RHR_FALLBACK)
        return RHR_FALLBACK


# ── NEW v13: Daily health metrics ─────────────────────────────────────────────
def fetch_daily_health_metrics(client, days=7):
    """
    Pull recent daily health data: RHR, HRV, Garmin Training Status,
    Garmin Training Load, VO2max trend. Returns a list of daily dicts,
    most recent first. Any individual metric that fails to fetch is
    set to null rather than breaking the whole pull.
    """
    daily_metrics = []
    d = datetime.now()

    for i in range(days):
        date_str = d.strftime("%Y-%m-%d")
        entry = {"date": date_str}

        # RHR + basic stats
        try:
            stats = client.get_stats(date_str)
            entry["rhr_bpm"] = stats.get("restingHeartRate")
            entry["calories_total"] = stats.get("totalKilocalories")
            entry["steps"] = stats.get("totalSteps")
        except Exception as e:
            entry["rhr_bpm"] = None
            log.warning("Stats fetch failed for %s: %s", date_str, e)

        # HRV (overnight average)
        try:
            hrv = client.get_hrv_data(date_str)
            if hrv and isinstance(hrv, dict):
                summary = hrv.get("hrvSummary", {})
                entry["hrv_avg_ms"] = summary.get("lastNightAvg")
                entry["hrv_status"] = summary.get("status")
            else:
                entry["hrv_avg_ms"] = None
                entry["hrv_status"] = None
        except Exception as e:
            entry["hrv_avg_ms"] = None
            entry["hrv_status"] = None
            log.warning("HRV fetch failed for %s: %s", date_str, e)

        # Training status / training load (Garmin native)
        try:
            training_status = client.get_training_status(date_str)
            if training_status and isinstance(training_status, dict):
                latest = training_status.get("mostRecentTrainingStatus", {})
                entry["training_status"] = latest.get("trainingStatusKey") if latest else None
                load_data = training_status.get("mostRecentTrainingLoadBalance", {})
                entry["training_load_acute"] = load_data.get("monthlyLoadAerobicLow") if load_data else None
            else:
                entry["training_status"] = None
                entry["training_load_acute"] = None
        except Exception as e:
            entry["training_status"] = None
            entry["training_load_acute"] = None
            log.warning("Training status fetch failed for %s: %s", date_str, e)

        # VO2max trend
        try:
            vo2 = client.get_max_metrics(date_str)
            if vo2 and isinstance(vo2, list) and len(vo2) > 0:
                generic = vo2[0].get("generic", {})
                entry["vo2max"] = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
            else:
                entry["vo2max"] = None
        except Exception as e:
            entry["vo2max"] = None
            log.warning("VO2max fetch failed for %s: %s", date_str, e)

        daily_metrics.append(entry)
        d -= timedelta(days=1)

    return daily_metrics


# ── NEW v13: All-activity training load pull ──────────────────────────────────
def fetch_all_activities_for_load(client, days=TRAINING_LOAD_WINDOW_D):
    """
    Pull ALL activity types (not just running) for training load context.
    Returns lightweight summaries — no lap-level dynamics, just enough
    for the coach to see overall training volume and load distribution.
    """
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date   = datetime.now().strftime("%Y-%m-%d")

    log.info("Fetching ALL activities (any type) from %s to %s for training load...", start_date, end_date)
    try:
        activities = client.get_activities_by_date(start_date, end_date)
    except Exception as e:
        log.error("Failed to fetch all-activity list: %s", e)
        return []

    load_entries = []
    for a in activities:
        try:
            entry = {
                "activity_id": a.get("activityId"),
                "activity_type": a.get("activityType", {}).get("typeKey", "unknown"),
                "start_time": a.get("startTimeLocal", ""),
                "duration_min": round(a.get("duration", 0) / 60, 1) if a.get("duration") else None,
                "distance_km": round(a.get("distance", 0) / 1000, 2) if a.get("distance") else None,
                "avg_hr_bpm": a.get("averageHR"),
                "max_hr_bpm": a.get("maxHR"),
                "calories": a.get("calories"),
                "training_effect_aerobic": a.get("aerobicTrainingEffect"),
                "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
            }
            load_entries.append(entry)
        except Exception as e:
            log.warning("Skipping malformed activity entry: %s", e)
            continue

    log.info("Training load: %d total activities across all types in %d-day window", len(load_entries), days)
    return load_entries


def build_training_load_summary(client, rhr, mhr):
    """Assemble the full training_load.json payload."""
    all_activities = fetch_all_activities_for_load(client, TRAINING_LOAD_WINDOW_D)
    daily_health   = fetch_daily_health_metrics(client, days=7)

    # Quick breakdown by activity type for at-a-glance load
    type_summary = {}
    for a in all_activities:
        t = a["activity_type"]
        if t not in type_summary:
            type_summary[t] = {"count": 0, "total_duration_min": 0.0, "total_distance_km": 0.0}
        type_summary[t]["count"] += 1
        if a["duration_min"]:
            type_summary[t]["total_duration_min"] += a["duration_min"]
        if a["distance_km"]:
            type_summary[t]["total_distance_km"] += a["distance_km"]

    for t in type_summary:
        type_summary[t]["total_duration_min"] = round(type_summary[t]["total_duration_min"], 1)
        type_summary[t]["total_distance_km"]  = round(type_summary[t]["total_distance_km"], 2)

    return {
        "meta": {
            "athlete_id": ATHLETE_ID,
            "generated_at": datetime.now().isoformat(),
            "window_days": TRAINING_LOAD_WINDOW_D,
            "note": "This file covers ALL activity types for training load context. "
                    "Detailed running lap dynamics remain in latest_run.json (running only). "
                    "ATL/CTL not calculated here — Garmin's native training_status and "
                    "training_load_acute fields are the closest available proxies. "
                    "HRV and training status may be null on dates where Garmin has no data."
        },
        "current_health": {
            "rhr_bpm_live": rhr,
            "mhr_bpm_fixed": mhr,
            "hrr_bpm": mhr - rhr
        },
        "daily_health_metrics_7d": daily_health,
        "activity_type_summary_30d": type_summary,
        "all_activities_30d": all_activities
    }


# ── Zones ─────────────────────────────────────────────────────────────────────
def make_zone_functions(rhr, mhr):
    hrr = mhr - rhr
    def get_zone(hr):
        if not hr or hr <= 0: return "-"
        pct = (hr - rhr) / hrr * 100
        if pct < 60: return "Z1"
        if pct < 70: return "Z2"
        if pct < 80: return "Z3"
        if pct < 90: return "Z4"
        return "Z5"
    def get_zone_pct(hr):
        if not hr or hr <= 0: return 0
        return round((hr - rhr) / hrr * 100, 1)
    return get_zone, get_zone_pct, hrr


# ── GAP ───────────────────────────────────────────────────────────────────────
def minetti_cost_ratio(grade_pct: float) -> float:
    g = max(-0.40, min(0.40, grade_pct / 100.0))
    cost = (280.5 * g**5 - 58.7 * g**4 - 76.8 * g**3 + 51.9 * g**2 + 19.6 * g + 2.5)
    return cost / 2.5

def compute_gap(pace_min_km: float, grade_pct: float) -> float:
    ratio = minetti_cost_ratio(grade_pct)
    if ratio <= 0: return pace_min_km
    return pace_min_km / ratio

def pace_str(pace_min: float) -> str:
    if not pace_min or pace_min <= 0 or pace_min >= 20: return None
    return "%d:%02d" % (int(pace_min), int((pace_min % 1) * 60))


# ── FIT extraction / parsing ───────────────────────────────────────────────────
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


def build_json_summary(raw_download, activity, rhr, mhr):
    get_zone, get_zone_pct, hrr = make_zone_functions(rhr, mhr)
    fit_data = extract_fit(raw_download)
    laps, records = parse_fit(fit_data)
    log.info("Parsed %d laps and %d records", len(laps), len(records))

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

        terrain_note = None
        if grade_pct is not None:
            if grade_pct <= -1.5:   terrain_note = "net downhill"
            elif grade_pct >= 1.5:  terrain_note = "net uphill"
            else:                   terrain_note = "flat"

        gap_min = None; gap_str = None
        if pace_min and grade_pct is not None:
            gap_min = compute_gap(pace_min, grade_pct)
            gap_str = pace_str(gap_min)
            gap_min = round(gap_min, 3)

        rec_gaps = []
        for r in recs:
            spd_raw = r.get(140, 0)
            if spd_raw < 500: continue
            speed_ms = spd_raw / 1000
            p = 1000 / speed_ms / 60
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
            "gap_min_km": gap_str,
            "gap_decimal": gap_min,
            "gap_granular_min_km": avg_gap_granular_str,
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
            "gap_note": "GAP uses Minetti et al. 2002 polynomial. gap_min_km = lap-level grade. gap_granular_min_km = avg of per-record grades.",
            "elevation_note": "Per-lap elevation from barometric altitude. Use elevation_net_m + terrain flag to separate terrain effects from fatigue effects on dynamics.",
            "hr_zone_note": "RHR is live 7-day rolling average pulled from Garmin at sync time. MHR is manually set — update only after a genuine max-effort test.",
            "training_load_note": "For broader training load context (all activity types, HRV, training status), see training_load.json in the same repo/folder.",
            "sensor_notes": {
                "vertical_oscillation": "Garmin wrist — reads ~15-20mm higher than STRYD ground truth",
                "cadence": "Reliable, cross-validated with STRYD",
                "gct": "Garmin sensor — use for trends, STRYD for absolute values",
                "pace_hr": "GPS pace and optical HR — reliable"
            }
        },
        "athlete": {
            "id": ATHLETE_ID,
            "rhr_bpm": rhr, "mhr_bpm": mhr, "hrr_bpm": hrr, "vo2max": 57,
            "zones": {
                "Z1": f"{round(rhr+0.50*hrr)}-{round(rhr+0.60*hrr)} bpm (50-60% HRR)",
                "Z2": f"{round(rhr+0.60*hrr)}-{round(rhr+0.70*hrr)} bpm (60-70% HRR)",
                "Z3": f"{round(rhr+0.70*hrr)}-{round(rhr+0.80*hrr)} bpm (70-80% HRR)",
                "Z4": f"{round(rhr+0.80*hrr)}-{round(rhr+0.90*hrr)} bpm (80-90% HRR)",
                "Z5": f"{round(rhr+0.90*hrr)}-{mhr} bpm (90-100% HRR)"
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


# ── Garmin connect ────────────────────────────────────────────────────────────
def connect_garmin():
    log.info("Connecting to Garmin Connect as %s ...", GARMIN_EMAIL)
    client = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    log.info("Garmin login successful.")
    return client

def get_recent_running_activities(client, since_date):
    log.info("Fetching RUNNING activities since %s ...", since_date)
    activities = client.get_activities_by_date(since_date, datetime.now().strftime("%Y-%m-%d"))
    activities = [a for a in activities if a.get("activityType", {}).get("typeKey", "").lower() in RUNNING_ACTIVITY_TYPES]
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
def push_to_github(json_bytes, github_path):
    if not GITHUB_TOKEN:
        log.warning("GITHUB_TOKEN not set — skipping GitHub push for %s.", github_path)
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    resp = requests.get(url, headers=headers)
    sha = resp.json().get("sha") if resp.status_code == 200 else None
    payload = {
        "message": f"Update {github_path} [{datetime.now().strftime('%Y-%m-%d %H:%M')}]",
        "content": base64.b64encode(json_bytes).decode("utf-8"),
        "branch": GITHUB_BRANCH
    }
    if sha: payload["sha"] = sha
    resp = requests.put(url, headers=headers, json=payload)
    if resp.status_code in [200, 201]:
        raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{github_path}"
        log.info("GitHub: %s updated → %s", github_path, raw_url)
        return raw_url
    else:
        log.error("GitHub push failed for %s: %s %s", github_path, resp.status_code, resp.text)
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
    rhr = fetch_resting_hr(garmin)
    mhr = MHR
    log.info("Using RHR=%d MHR=%d HRR=%d for this sync", rhr, mhr, mhr - rhr)

    dbx    = connect_dropbox()
    gdrive = connect_gdrive()
    ensure_dropbox_folder(dbx, DROPBOX_FOLDER)

    # ── PART 1: Running-only detailed sync (unchanged behaviour) ──
    running_activities = get_recent_running_activities(garmin, since_date)
    running_activities = sorted(running_activities, key=lambda a: a.get("startTimeLocal", ""))

    new_count = 0
    for activity in running_activities:
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

            summary    = build_json_summary(raw_download, activity, rhr, mhr)
            json_bytes = json.dumps(summary, indent=2).encode("utf-8")

            log.info("Laps: %d | Pace: %s | GAP: %s",
                     len(summary.get("laps",[])),
                     summary["overall"]["avg_pace_min_km"],
                     summary["overall"]["avg_gap_min_km"])

            upload_to_gdrive(gdrive, json_bytes, json_filename, "application/json")
            latest_id = upload_to_gdrive(gdrive, json_bytes, LATEST_RUN_FILENAME, "application/json")
            make_file_public(gdrive, latest_id)
            push_to_github(json_bytes, GITHUB_LATEST_RUN_PATH)

            new_count += 1
            synced_ids.add(activity_id)

        except Exception as e:
            log.error("Failed to sync running activity %s: %s", activity_id, e)
            import traceback; traceback.print_exc()

    state["synced_ids"] = list(synced_ids)

    # ── PART 2 (NEW v13): Training load + health metrics, all activity types ──
    try:
        log.info("Building training load summary (all activity types, %d-day window)...", TRAINING_LOAD_WINDOW_D)
        training_load = build_training_load_summary(garmin, rhr, mhr)
        tl_bytes = json.dumps(training_load, indent=2).encode("utf-8")

        tl_id = upload_to_gdrive(gdrive, tl_bytes, TRAINING_LOAD_FILENAME, "application/json")
        make_file_public(gdrive, tl_id)
        push_to_github(tl_bytes, GITHUB_TRAINING_LOAD_PATH)

        log.info("Training load summary uploaded: %d activities, %d days of health metrics",
                 len(training_load["all_activities_30d"]),
                 len(training_load["daily_health_metrics_7d"]))
    except Exception as e:
        log.error("Failed to build/upload training load summary: %s", e)
        import traceback; traceback.print_exc()

    save_state(state)
    log.info("Sync complete. %d new running activity/activities processed.", new_count)


if __name__ == "__main__":
    sync()
