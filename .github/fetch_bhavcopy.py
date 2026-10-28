"""
NSE Full Bhavcopy & Security Deliverable data  ->  Google Sheet (via Apps Script web app)

Runs inside GitHub Actions. Modes (chosen from workflow inputs):
  - Scheduled run          -> today's file (IST), skipped if already in the sheet
  - Manual, date given     -> that date, always re-loaded (replaces existing rows)
  - Manual, from/to given  -> backfill weekdays in range, skips dates already loaded
  - Manual, nothing given  -> today's file, re-loaded

Env vars:
  SHEET_WEBAPP_URL, SHEET_TOKEN          (GitHub secrets)
  INPUT_DATE, INPUT_FROM, INPUT_TO       (DD-MM-YYYY, optional)
  EVENT_NAME                             (schedule / workflow_dispatch)
"""

import datetime as dt
import os
import sys
import time

import requests

WEBAPP_URL = os.environ["SHEET_WEBAPP_URL"].strip()
TOKEN = os.environ["SHEET_TOKEN"].strip()
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

HOSTS = ["https://nsearchives.nseindia.com", "https://archives.nseindia.com"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
BACKFILL_DELAY = 2  # seconds between NSE requests


def parse_input(s):
    s = (s or "").strip()
    if not s:
        return None
    for f in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            pass
    sys.exit(f"Invalid date '{s}'. Use DD-MM-YYYY.")


def new_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    try:  # warm-up for cookies; harmless if it fails
        s.get("https://www.nseindia.com/", timeout=10)
    except requests.RequestException:
        pass
    return s


def fetch(session, d):
    """Returns (status, csv_text, message). status: OK / NO_FILE / ERROR"""
    key = d.strftime("%d%m%Y")
    last = "no attempt"
    for host in HOSTS:
        url = f"{host}/products/content/sec_bhavdata_full_{key}.csv"
        for attempt in range(3):
            try:
                r = session.get(url, timeout=30)
            except requests.RequestException as e:
                last = f"{host}: {type(e).__name__}"
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200 and "SYMBOL" in r.text[:300]:
                return "OK", r.text, url
            if r.status_code == 404:
                return "NO_FILE", "", "File not published yet, or market holiday"
            last = f"{host}: HTTP {r.status_code}"
            time.sleep(5 * (attempt + 1))
    return "ERROR", "", last


def call_sheet(payload):
    payload["token"] = TOKEN
    r = requests.post(WEBAPP_URL, json=payload, timeout=180)
    try:
        res = r.json()
    except ValueError:
        raise RuntimeError(f"Web app returned non-JSON (HTTP {r.status_code}): {r.text[:300]}")
    if not res.get("ok"):
        raise RuntimeError(f"Web app error: {res.get('error')}")
    return res


def process(session, d, force, mode):
    iso = d.isoformat()
    if not force and call_sheet({"action": "check", "date": iso}).get("loaded"):
        print(f"{iso}: already loaded, skipped")
        return "SKIPPED"

    status, text, msg = fetch(session, d)
    res = call_sheet({"action": "load", "date": iso, "status": status, "csv": text,
                      "message": msg, "force": force, "mode": mode})
    print(f"{iso}: {res.get('status')} rows={res.get('rows')} {res.get('msg', '')}")
    return res.get("status")


def main():
    one = parse_input(os.environ.get("INPUT_DATE"))
    frm = parse_input(os.environ.get("INPUT_FROM"))
    to = parse_input(os.environ.get("INPUT_TO"))
    scheduled = os.environ.get("EVENT_NAME") == "schedule"
    today = dt.datetime.now(IST).date()

    if one:
        jobs, force, mode = [one], True, "MANUAL"
    elif frm or to:
        if not (frm and to) or frm > to:
            sys.exit("Backfill needs both from_date and to_date, with from <= to.")
        days = (to - frm).days + 1
        jobs = [frm + dt.timedelta(n) for n in range(days)]
        jobs = [d for d in jobs if d.weekday() < 5]
        force, mode = False, "BACKFILL"
    else:
        if today.weekday() >= 5:
            print("Weekend - nothing to do.")
            return
        jobs, force, mode = [today], not scheduled, "AUTO" if scheduled else "MANUAL"

    session = new_session()
    results = []
    for i, d in enumerate(jobs):
        results.append(process(session, d, force, mode))
        if mode == "BACKFILL" and i < len(jobs) - 1 and results[-1] != "SKIPPED":
            time.sleep(BACKFILL_DELAY)

    summary = {s: results.count(s) for s in set(results)}
    print("Summary:", summary)
    if "ERROR" in results:
        sys.exit(1)  # marks the Actions run as failed -> GitHub emails you


if __name__ == "__main__":
    main()
