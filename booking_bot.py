#!/usr/bin/env python3
"""
PRM Chula Tennis Court Auto-Booking Bot
========================================
Logs in at ~08:55, sleeps until exactly 09:00:00, then fires
booking POSTs for courts in priority order: 10 → 9 → 5 → 4.

Slot times  : 06:00–21:00  (15 slots, slotIndex = hour - 6)
Default slot : 08:00 AM    (slotIndex = 2)
Booking opens: 09:00 AM every Saturday for NEXT Saturday

Usage
-----
  python booking_bot.py                  # book next Saturday at default hour
  python booking_bot.py --hour 10        # book 10:00–11:00 slot
  python booking_bot.py --discover       # print raw JSON to find spaceIds

Deploy (VPS — Bangkok/Singapore region)
---------------------------------------
  crontab -e
  55 8 * * 6 cd /home/user/tennis_booking_bot && python3 booking_bot.py >> booking.log 2>&1
"""

import sys
import time
import datetime
import logging
import json
import os
import argparse
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit these or set as environment variables
# ─────────────────────────────────────────────────────────────────────────────

USERNAME = os.environ.get("BOOKING_USER", "YOUR_MEMBER_ID")
PASSWORD = os.environ.get("BOOKING_PASS", "YOUR_PASSWORD")

BASE_URL    = "https://booking.prm.chula.ac.th"
SPORT_ID    = 2    # tennis
BUILDING_ID = 8

# Default booking slot — 08:00 AM (override with --hour N)
DEFAULT_HOUR = 8   # valid range: 6–20

# Court priority order (first available wins)
COURT_PRIORITY = [10, 9, 5, 4]

# Court number → spaceId (confirmed from network inspection)
# slotIndex formula: slotIndex = hour - 6
# e.g.  06:00 → 0 | 07:00 → 1 | 08:00 → 2 | ... | 20:00 → 14
COURT_SPACE_MAP: dict[int, int] = {
    10: 86,
     9: 85,
     5: 81,
     4: 80,
}

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("booking_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

BASE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/149.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,th-TH;q=0.8,th;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}

# Chula network uses SSL inspection proxy — disable certificate verification
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
VERIFY_SSL = False


def hour_to_slot(hour: int) -> int:
    """Convert a 24-hour value to slotIndex. Slots start at 06:00 (index 0)."""
    if not (6 <= hour <= 20):
        raise ValueError(f"Hour must be 6–20, got {hour}")
    return hour - 6


def next_saturday_be() -> str:
    """Return next Saturday in DD/MM/YYYY Buddhist Era (year + 543)."""
    today = datetime.date.today()
    days_ahead = (5 - today.weekday()) % 7 or 7
    sat = today + datetime.timedelta(days=days_ahead)
    return sat.strftime(f"%d/%m/{sat.year + 543}")


def extract_csrf(session: requests.Session, url: str) -> str:
    """GET a page and return the CSRF token."""
    r = session.get(url, headers=BASE_HEADERS, timeout=10, verify=VERIFY_SSL)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Hidden input: <input type="hidden" name="_token" value="...">
    inp = soup.find("input", {"name": "_token"})
    if inp and inp.get("value"):
        return inp["value"]
    # Fallback: <meta name="csrf-token" content="...">
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    # Fallback: XSRF-TOKEN cookie
    xsrf = session.cookies.get("XSRF-TOKEN", "")
    if xsrf:
        return unquote(xsrf)
    raise RuntimeError(f"CSRF token not found on {url}")


def xhr_headers(token: str, referer: str) -> dict:
    return {
        **BASE_HEADERS,
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Csrf-Token":     token,
        "X-Xsrf-Token":     token,
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          referer,
        "Origin":           BASE_URL,
    }


def sleep_until(target: datetime.datetime) -> None:
    """Sub-second precision sleep until target time."""
    delta = (target - datetime.datetime.now()).total_seconds()
    if delta > 0:
        log.info(f"Sleeping {delta:.3f}s until {target.strftime('%H:%M:%S')}")
        time.sleep(delta)

# ─────────────────────────────────────────────────────────────────────────────
# BOOKING STEPS
# ─────────────────────────────────────────────────────────────────────────────

def login(session: requests.Session) -> bool:
    log.info("Step 1 — Login")
    token = extract_csrf(session, f"{BASE_URL}/auth/login")
    r = session.post(
        f"{BASE_URL}/auth/login",
        data={
            "_token":     token,
            "username":   USERNAME,
            "password":   PASSWORD,
            "login_type": "ofa",
        },
        headers={
            **BASE_HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      f"{BASE_URL}/auth/login",
        },
        allow_redirects=False,
        timeout=10,
        verify=VERIFY_SSL,
    )
    ok = r.status_code in (301, 302)
    log.info(f"  {'✅ OK' if ok else '❌ FAILED'} ({r.status_code})")
    return ok


def search_step1(session: requests.Session, token: str) -> dict:
    """Step 1 search: select sport type → returns buildings/spaces list."""
    r = session.post(
        f"{BASE_URL}/bookingsportform-step1/search?page=1",
        data={"sportId": SPORT_ID, "_token": token},
        headers=xhr_headers(token, f"{BASE_URL}/bookingsportform-step1"),
        timeout=10,
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json()


def search_step2(session: requests.Session, token: str, date_str: str) -> dict:
    """Step 2 search: check availability for all courts on target date."""
    space_ids = list(COURT_SPACE_MAP.values())
    all_spaces = list(range(77, 87))   # full court range 77–86
    data = {"date": date_str, "buildingId": BUILDING_ID, "_token": token}
    for sid in all_spaces:
        data.setdefault("space[]", [])
        if isinstance(data["space[]"], list):
            data["space[]"].append(sid)

    r = session.post(
        f"{BASE_URL}/bookingsportform-step2/search?page=1",
        data=data,
        headers=xhr_headers(token, f"{BASE_URL}/bookingsportform-step1"),
        timeout=10,
        verify=VERIFY_SSL,
    )
    r.raise_for_status()
    return r.json()


def book_space(
    session: requests.Session,
    space_id: int,
    date_str: str,
    slot_index: int,
    token: str,
) -> requests.Response:
    """Final booking POST to /bookingsportform-step2/store."""
    return session.post(
        f"{BASE_URL}/bookingsportform-step2/store",
        data={
            "spaceId":    space_id,
            "date":       date_str,
            "slotIndex":  slot_index,
            "buildingId": BUILDING_ID,
            "_token":     token,
        },
        headers=xhr_headers(token, f"{BASE_URL}/bookingsportform-step1"),
        timeout=10,
        verify=VERIFY_SSL,
    )

# ─────────────────────────────────────────────────────────────────────────────
# DISCOVER MODE
# ─────────────────────────────────────────────────────────────────────────────

def discover():
    """Login and dump step-1 + step-2 JSON to verify court → spaceId mapping."""
    log.info("═" * 60)
    log.info("DISCOVER MODE")
    log.info("═" * 60)
    session = requests.Session()
    if not login(session):
        log.error("Login failed"); return

    token = extract_csrf(session, f"{BASE_URL}/bookingsportform-step1")

    print("\n── Step 1 search response ──")
    data1 = search_step1(session, token)
    print(json.dumps(data1, indent=2, ensure_ascii=False))

    date_str = next_saturday_be()
    print(f"\n── Step 2 search response (date={date_str}) ──")
    token = extract_csrf(session, f"{BASE_URL}/bookingsportform-step1")
    data2 = search_step2(session, token, date_str)
    print(json.dumps(data2, indent=2, ensure_ascii=False))
    print("\nUse the output above to verify COURT_SPACE_MAP in this script.")

# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

LINE_TOKEN = os.environ.get("LINE_NOTIFY_TOKEN", "")


def notify(message: str) -> None:
    if not LINE_TOKEN:
        return
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_TOKEN}"},
            data={"message": f"\n{message}"},
            timeout=5,
            verify=VERIFY_SSL,
        )
        log.info("LINE notification sent")
    except Exception as e:
        log.warning(f"LINE notify failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN BOOKING FLOW
# ─────────────────────────────────────────────────────────────────────────────

def run(target_hour: int = DEFAULT_HOUR):
    slot_index  = hour_to_slot(target_hour)
    slot_label  = f"{target_hour:02d}:00–{target_hour+1:02d}:00"

    log.info("═" * 60)
    log.info(f"PRM Chula Tennis Bot  |  slot={slot_label}  slotIndex={slot_index}")
    log.info("═" * 60)

    session = requests.Session()

    # ── Phase A: Login + prepare BEFORE 09:00 ────────────────────────────────
    if not login(session):
        log.error("Login failed — aborting"); return

    token       = extract_csrf(session, f"{BASE_URL}/bookingsportform-step1")
    target_date = next_saturday_be()

    log.info(f"Target date : {target_date}")
    log.info(f"Target slot : {slot_label} (slotIndex={slot_index})")
    log.info(f"Court order : {COURT_PRIORITY}")
    log.info(f"Space map   : { {c: COURT_SPACE_MAP[c] for c in COURT_PRIORITY if c in COURT_SPACE_MAP} }")

    # ── Phase B: Precision sleep until exactly 09:00:00 ──────────────────────
    today   = datetime.date.today()
    fire_at = datetime.datetime(today.year, today.month, today.day, 9, 0, 0, 0)
    sleep_until(fire_at)

    # ── Phase C: Check availability then fire bookings ────────────────────────
    log.info("Checking availability …")
    try:
        avail_data = search_step2(session, token, target_date)
        slots_map  = avail_data.get("datas", {}).get("data", {})
        # slots_map: {"86": [1,1,0,...], "85": [...], ...}
        # 1 = available, 0 = booked; index = slotIndex
    except Exception as e:
        log.warning(f"Availability check failed ({e}) — proceeding blind")
        slots_map = {}

    # Refresh token after step2 search
    token = extract_csrf(session, f"{BASE_URL}/bookingsportform-step1")

    # ── Phase D: Fire booking POSTs in priority order ─────────────────────────
    log.info(f"🚀 FIRING at {datetime.datetime.now().strftime('%H:%M:%S.%f')[:12]}")

    for court in COURT_PRIORITY:
        space_id = COURT_SPACE_MAP.get(court)
        if not space_id:
            log.warning(f"Court {court}: not in COURT_SPACE_MAP — skipping")
            continue

        # Skip courts already shown as booked (0=available, 1=booked)
        court_slots = slots_map.get(str(space_id), [])
        if court_slots and len(court_slots) > slot_index:
            if court_slots[slot_index] == 1:
                log.info(f"Court {court} (spaceId={space_id}): slot already booked — skipping")
                continue

        log.info(f"Trying Court {court} (spaceId={space_id}) …")
        try:
            r    = book_space(session, space_id, target_date, slot_index, token)
            ct   = r.headers.get("Content-Type", "")
            body = r.json() if "application/json" in ct else r.text[:400]
            log.info(f"  HTTP {r.status_code} → {body}")

            if r.status_code == 200:
                log.info(f"✅ SUCCESS — Court {court} booked on {target_date} {slot_label}")
                notify(f"✅ Booked Court {court} on {target_date} {slot_label}")
                return

            if r.status_code == 429:
                log.warning("  Rate limited — waiting 0.5s")
                time.sleep(0.5)

        except requests.RequestException as e:
            log.error(f"  Request error: {e}")

    log.error("❌ All preferred courts failed")
    notify(f"❌ Tennis booking FAILED on {target_date} {slot_label} — all courts taken or error")

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PRM Chula Tennis Auto-Booking Bot")
    parser.add_argument(
        "--hour", type=int, default=DEFAULT_HOUR,
        help=f"Target booking hour in 24h format, 6–20 (default: {DEFAULT_HOUR} = 08:00 AM)"
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="Dump court list JSON to verify spaceId mapping (run this first)"
    )
    args = parser.parse_args()

    if args.discover:
        discover()
    else:
        run(target_hour=args.hour)
