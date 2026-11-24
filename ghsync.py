#!/usr/bin/env python3
"""ghsync - pull your own Google Health (Fitbit) data into SQLite.

Standard library only. Commands:
  login    one-time OAuth consent; saves a refresh token to $DATA_DIR/token.json
  whoami   check the account link (GET /users/me/identity) and paired devices
  sync     pull new data once
  run      sync every $SYNC_INTERVAL_MIN minutes, forever (container default)
"""
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://health.googleapis.com/v4"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE_BASE = "https://www.googleapis.com/auth/googlehealth."
DEFAULT_SCOPES = "activity_and_fitness.readonly health_metrics_and_measurements.readonly sleep.readonly"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
DB_FILE = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "health.db"))
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8765/")
SCOPES = os.environ.get("SCOPES", DEFAULT_SCOPES).split()
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "48"))  # re-read recent data: syncs arrive late
CHUNK_DAYS = int(os.environ.get("CHUNK_DAYS", "7"))
SYNC_INTERVAL_MIN = int(os.environ.get("SYNC_INTERVAL_MIN", "30"))

# kind decides the filter field:
#   interval -> <snake>.interval.start_time   sample  -> <snake>.sample_time.physical_time
#   daily    -> <snake>.date (YYYY-MM-DD)     sleep   -> sleep.interval.end_time
#   civil    -> <snake>.interval.civil_start_time (exercise only supports civil time)
#   rollup   -> no list support; one dailyRollUp value per day
DATA_TYPES = {
    "steps": "interval",
    "distance": "interval",
    "floors": "rollup",
    "active-minutes": "interval",
    "active-zone-minutes": "interval",
    "sedentary-period": "interval",
    "total-calories": "rollup",
    "exercise": "civil",
    "sleep": "sleep",
    "heart-rate": "sample",
    "heart-rate-variability": "sample",
    "oxygen-saturation": "sample",
    "respiratory-rate-sleep-summary": "sample",
    "vo2-max": "sample",
    "run-vo2-max": "sample",
    "daily-heart-rate-variability": "daily",
    "daily-resting-heart-rate": "daily",
    "daily-respiratory-rate": "daily",
    "daily-oxygen-saturation": "daily",
    "daily-vo2-max": "daily",
    "daily-sleep-temperature-derivations": "daily",
}
if os.environ.get("DATA_TYPES"):
    DATA_TYPES = {t: DATA_TYPES.get(t, "interval") for t in os.environ["DATA_TYPES"].replace(",", " ").split()}

# Field inside the payload that becomes the numeric `value` column.
VALUE_FIELD = {
    "steps": "count", "floors": "count", "distance": "millimeters",
    "heartRate": "beatsPerMinute",
    "heartRateVariability": "rootMeanSquareOfSuccessiveDifferencesMilliseconds",
    "oxygenSaturation": "percentage", "vo2Max": "vo2Max", "runVo2Max": "runVo2Max",
    "dailyHeartRateVariability": "averageHeartRateVariabilityMilliseconds",
    "dailyRestingHeartRate": "beatsPerMinute", "dailyRespiratoryRate": "breathsPerMinute",
    "dailyOxygenSaturation": "averagePercentage", "dailyVo2Max": "vo2Max",
    "dailySleepTemperatureDerivations": "nightlyTemperatureCelsius",
    "activeZoneMinutes": "activeZoneMinutes",
    "respiratoryRateSleepSummary": "fullSleepStats.breathsPerMinute",
    "sleep": "summary.minutesAsleep",
}


def log(*a):
    print(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def camel(kebab):
    head, *rest = kebab.split("-")
    return head + "".join(p.title() for p in rest)


def rfc3339(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def num(v):
    try:
        return float(v[:-1]) if isinstance(v, str) and v.endswith("s") else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- HTTP / OAuth

def http(method, url, data=None, headers=None, form=False):
    body = None
    headers = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw.decode(errors="replace")}


def need_client():
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (see .env.example).")


def save_token(tok):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tok, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKEN_FILE)


def cmd_login():
    need_client()
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPE_BASE + s for s in SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    print("\n1. Open this URL in a browser and approve access:\n")
    print(AUTH_URL + "?" + urllib.parse.urlencode(params))
    print("\n2. The browser then goes to " + REDIRECT_URI + " and shows an error page; that's expected.")
    print("   Copy the FULL address from the address bar and paste it here.\n")
    pasted = input("Redirected URL: ").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    code = qs.get("code", [pasted])[0]
    status, tok = http("POST", TOKEN_URL, {
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code",
    }, form=True)
    if status != 200 or "refresh_token" not in tok:
        sys.exit(f"Token exchange failed ({status}): {tok}")
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    save_token(tok)
    print(f"Saved token to {TOKEN_FILE}. Granted scopes: {tok.get('scope')}")
    cmd_whoami()


class Client:
    def __init__(self):
        need_client()
        if not os.path.exists(TOKEN_FILE):
            sys.exit(f"No token at {TOKEN_FILE}; run `login` first.")
        with open(TOKEN_FILE) as f:
            self.tok = json.load(f)

    def access_token(self, force=False):
        if force or time.time() >= self.tok.get("expires_at", 0):
            status, new = http("POST", TOKEN_URL, {
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "refresh_token": self.tok["refresh_token"], "grant_type": "refresh_token",
            }, form=True)
            if status != 200:
                raise RuntimeError(
                    f"Token refresh failed ({status}): {new}. If this says invalid_grant, the refresh "
                    "token expired (7 days while the OAuth app is in Testing) - run `login` again.")
            self.tok.update(new)
            self.tok["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
            save_token(self.tok)
        return self.tok["access_token"]

    def get(self, path, params=None):
        return self.request("GET", path, params)

    def post(self, path, data):
        return self.request("POST", path, None, data)

    def request(self, method, path, params=None, data=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        delay, status = 2, None
        for attempt in range(6):
            token = self.access_token(force=status == 401)
            status, body = http(method, url, data, headers={"Authorization": "Bearer " + token})
            if status == 200:
                return body
            if status in (401, 429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"{method} {path} -> {status}: {json.dumps(body)[:500]}")


def cmd_whoami():
    c = Client()
    print(json.dumps(c.get("/users/me/identity"), indent=2))
    try:
        print(json.dumps(c.get("/users/me/pairedDevices"), indent=2))
    except RuntimeError as e:
        print("pairedDevices:", e)


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_points (
  id         TEXT PRIMARY KEY,
  data_type  TEXT NOT NULL,
  start_time TEXT NOT NULL,   -- UTC RFC3339, or YYYY-MM-DD for daily types
  end_time   TEXT,
  value      REAL,            -- main number for the type (see VALUE_FIELD in ghsync.py)
  source     TEXT,            -- device name / platform
  raw        TEXT NOT NULL,   -- type payload as JSON, minus the time fields; query with json_extract()
  synced_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_type_start ON data_points(data_type, start_time);
CREATE TABLE IF NOT EXISTS sync_state (data_type TEXT PRIMARY KEY, last_end TEXT NOT NULL);
"""


def db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def dig(obj, path):
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


TIME_KEYS = ("interval", "sampleTime", "date")


def first_number(payload):
    return next((num(v) for k, v in payload.items() if k not in TIME_KEYS and num(v) is not None), None)


def to_row(data_type, dp):
    pkey = camel(data_type)
    payload = dp.get(pkey) or {}
    now = rfc3339(dt.datetime.now(dt.timezone.utc))
    if "civilStartTime" in dp:  # dailyRollUp result
        d = dp["civilStartTime"]["date"]
        day = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
        return (f"{data_type}|{day}", data_type, day, None, first_number(payload), "rollup",
                json.dumps(payload, separators=(",", ":")), now)
    if "interval" in payload:
        start, end = payload["interval"].get("startTime"), payload["interval"].get("endTime")
    elif "sampleTime" in payload:
        start, end = payload["sampleTime"].get("physicalTime"), None
    elif "date" in payload:
        d = payload["date"]
        start, end = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}", None
    else:
        start, end = None, None

    field = VALUE_FIELD.get(pkey)
    if pkey == "activeMinutes":
        value = sum(num(x.get("activeMinutes")) or 0 for x in payload.get("activeMinutesByActivityLevel", []))
    elif pkey == "exercise":
        value = num(payload.get("activeDuration"))  # seconds
    elif pkey == "sedentaryPeriod" and start and end:
        value = (parse_time(end) - parse_time(start)).total_seconds() / 60  # minutes
    elif field:
        value = num(dig(payload, field))
    else:  # unknown type: take the first numeric-looking scalar
        value = first_number(payload)

    ds = dp.get("dataSource") or {}
    source = (ds.get("device") or {}).get("displayName") or ds.get("platform")
    name = dp.get("name") or ""
    ident = name if name and not name.endswith("/") else f"{data_type}|{start}|{source}"  # some types send an empty id
    if start is None:
        ident = data_type + "|" + hashlib.sha1(json.dumps(dp, sort_keys=True).encode()).hexdigest()
        start = ""
    slim = {k: v for k, v in payload.items() if k not in TIME_KEYS}  # times/source have own columns
    return (ident, data_type, start, end, value, source, json.dumps(slim, separators=(",", ":")), now)


# ---------------------------------------------------------------- sync

def build_filter(data_type, kind, a, b):
    snake = data_type.replace("-", "_")
    if kind == "daily":
        return f'{snake}.date >= "{a.date().isoformat()}" AND {snake}.date < "{(b.date() + dt.timedelta(days=1)).isoformat()}"'
    if kind == "civil":
        f = f"{snake}.interval.civil_start_time"
        return f'{f} >= "{a.strftime("%Y-%m-%dT%H:%M:%S")}" AND {f} < "{b.strftime("%Y-%m-%dT%H:%M:%S")}"'
    f = {"interval": f"{snake}.interval.start_time",
         "sample": f"{snake}.sample_time.physical_time",
         "sleep": f"{snake}.interval.end_time"}[kind]
    return f'{f} >= "{rfc3339(a)}" AND {f} < "{rfc3339(b)}"'


def sync_type(c, con, data_type, kind, now):
    row = con.execute("SELECT last_end FROM sync_state WHERE data_type=?", (data_type,)).fetchone()
    start = parse_time(row[0]) - dt.timedelta(hours=LOOKBACK_HOURS) if row else now - dt.timedelta(days=BACKFILL_DAYS)
    page_size = 25 if data_type in ("sleep", "exercise") else 10000
    total = 0
    a = start
    while a < now:
        b = min(a + dt.timedelta(days=CHUNK_DAYS), now)
        if kind == "rollup":
            day = lambda t: {"date": {"year": t.year, "month": t.month, "day": t.day}}
            body = c.post(f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", {
                "range": {"start": day(a), "end": day(b + dt.timedelta(days=1))}, "windowSizeDays": 1})
            rows = [to_row(data_type, dp) for dp in body.get("rollupDataPoints", [])]
            con.executemany("INSERT OR REPLACE INTO data_points VALUES (?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            params = None
        else:
            params = {"filter": build_filter(data_type, kind, a, b), "pageSize": page_size}
        while params:
            body = c.get(f"/users/me/dataTypes/{data_type}/dataPoints", params)
            rows = [to_row(data_type, dp) for dp in body.get("dataPoints", [])]
            con.executemany("INSERT OR REPLACE INTO data_points VALUES (?,?,?,?,?,?,?,?)", rows)
            total += len(rows)
            if not body.get("nextPageToken"):
                break
            params["pageToken"] = body["nextPageToken"]
        con.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)", (data_type, rfc3339(b)))
        con.commit()
        a = b
    return total


def cmd_sync():
    c, con = Client(), db()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    ok = True
    for data_type, kind in DATA_TYPES.items():
        try:
            log(f"{data_type}: {sync_type(c, con, data_type, kind, now)} points")
        except RuntimeError as e:
            ok = False
            log(f"{data_type}: FAILED {e}")
            if "Token refresh failed" in str(e):
                break
    con.close()
    return ok


def cmd_run():
    while True:
        try:
            cmd_sync()
        except Exception as e:  # keep the loop alive; errors are logged
            log("sync error:", e)
        time.sleep(SYNC_INTERVAL_MIN * 60)


if __name__ == "__main__":
    cmds = {"login": cmd_login, "whoami": cmd_whoami, "sync": cmd_sync, "run": cmd_run}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    result = cmds[sys.argv[1]]()
    sys.exit(0 if result is not False else 1)
