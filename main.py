from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta, date, timezone
from dateutil import tz
from astral import LocationInfo
from astral.sun import dawn, dusk, sunrise, sunset
from skyfield.api import load, wgs84
from skyfield import almanac
import requests, os, json, time, threading
from typing import Optional

# ----- Config -----
WORLDTIDES_KEY = os.getenv("WORLDTIDES_API_KEY", "")
TS = load.timescale()
EPH = load("de421.bsp")  # cached on first run

app = FastAPI(title="DayPack API")

# CORS for FlutterFlow preview & devices
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

def to_local(dt_utc: datetime, tzname: str) -> str:
    return dt_utc.astimezone(tz.gettz(tzname)).isoformat(timespec="minutes")

def sun_block(lat, lon, tzname, d: date):
    loc = LocationInfo(latitude=lat, longitude=lon, timezone=tzname)
    adawn = dawn(loc.observer, d, depression=18)     # astronomical
    adusk = dusk(loc.observer, d, depression=18)
    sr = sunrise(loc.observer, d)
    ss = sunset(loc.observer, d)
    return [
        {"time_local": to_local(adawn, tzname), "label": "Astronomical dawn"},
        {"time_local": to_local(sr, tzname),    "label": "Sunrise"},
        {"time_local": to_local(ss, tzname),    "label": "Sunset"},
        {"time_local": to_local(adusk, tzname), "label": "Astronomical dusk"},
    ]

def moon_block(lat, lon, tzname, d: date):
    # 36h window so events near midnight still show on the target date
    start_local = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz.gettz(tzname))
    end_local   = start_local + timedelta(hours=36)
    t0 = TS.from_datetime(start_local.astimezone(timezone.utc))
    t1 = TS.from_datetime(end_local.astimezone(timezone.utc))

    # Use lowercase keys and pass a Topos (lat/lon), not earth+topos
    topos = wgs84.latlon(lat, lon)
    moon = EPH["moon"]   # lowercase

    events = []

    # Moonrise / Moonset
    f_rs = almanac.risings_and_settings(EPH, moon, topos)
    t_rs, y_rs = almanac.find_discrete(t0, t1, f_rs)
    for t, y in zip(t_rs, y_rs):
        local = t.utc_datetime().astimezone(tz.gettz(tzname))
        if local.date() == d:
            events.append({
                "time_local": local.isoformat(timespec="minutes"),
                "label": "Moonrise" if y == 1 else "Moonset"
            })

    # Meridian transits (upper/lower)
    f_tr = almanac.meridian_transits(EPH, moon, topos)
    t_tr, y_tr = almanac.find_discrete(t0, t1, f_tr)
    for t, y in zip(t_tr, y_tr):
        local = t.utc_datetime().astimezone(tz.gettz(tzname))
        if local.date() == d:
            events.append({
                "time_local": local.isoformat(timespec="minutes"),
                "label": "Moon above" if y == 1 else "Moon below"
            })

    return events

def phases_perigee_apogee(tzname, d: date):
    # Phases in a ±1 day window (discrete events)
    t0 = TS.from_datetime(datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc) - timedelta(days=1))
    t1 = TS.from_datetime(datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc) + timedelta(days=2))
    f_phase = almanac.moon_phases(EPH)
    times, phases = almanac.find_discrete(t0, t1, f_phase)
    labels = {0: "New Moon", 1: "First Quarter", 2: "Full Moon", 3: "Last Quarter"}

    out = []
    for t, p in zip(times, phases):
        when_local = t.utc_datetime().astimezone(tz.gettz(tzname))
        if when_local.date() == d:
            out.append({"time_local": when_local.isoformat(timespec="minutes"),
                        "label": labels.get(int(p), "Moon phase")})

    # Perigee / Apogee: scan a 36h window for local minima/maxima of Earth–Moon distance
    earth, moon = EPH["Earth"], EPH["Moon"]
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc) - timedelta(hours=12)
    points = []
    for k in range(0, 72 + 1):  # 36h, 30-minute steps
        tt = TS.from_datetime(start + timedelta(minutes=30 * k))
        km = (earth - moon).at(tt).distance().km
        points.append((tt, km))
    for i in range(1, len(points) - 1):
        t_prev, d_prev = points[i - 1]
        t_curr, d_curr = points[i]
        t_next, d_next = points[i + 1]
        is_min = d_curr < d_prev and d_curr < d_next
        is_max = d_curr > d_prev and d_curr > d_next
        if not (is_min or is_max):
            continue
        when_local = t_curr.utc_datetime().astimezone(tz.gettz(tzname))
        if when_local.date() == d:
            label = "Perigee" if is_min else "Apogee"
            out.append({"time_local": when_local.isoformat(timespec="minutes"),
                        "label": f"{label} ({round(d_curr):,} km)"})
    return out

def tides(lat, lon, tzname, d: date):
    # If no key, just skip tides
    if not WORLDTIDES_KEY:
        return []

    # Build the request
    start_ts = int(datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc).timestamp())
    url = (
        "https://www.worldtides.info/api/v3"
        f"?extremes&lat={lat}&lon={lon}&start={start_ts}&length=1&key={WORLDTIDES_KEY}"
    )

    try:
        # Shorter timeouts + tiny retry so it doesn’t hang your whole request
        data = None
        for _ in range(2):  # up to 2 attempts
            r = requests.get(url, timeout=(5, 10))  # (connect, read) seconds
            if r.status_code == 200:
                data = r.json()
                break
            # non-200 → stop trying; treat as no-tide-data
            break

        if not data:
            return []

        out = []
        for e in data.get("extremes", []):
            when_utc = datetime.fromtimestamp(e["dt"], tz=timezone.utc)
            when_local = when_utc.astimezone(tz.gettz(tzname))
            if when_local.date() == d:
                label = f"{e['type'].title()} tide"
                if e.get("height") is not None:
                    label += f" {e['height']:.2f} m"
                out.append({"time_local": when_local.isoformat(timespec="minutes"), "label": label})
        return out

    except Exception as exc:
        # Log and carry on with no tides instead of crashing the endpoint
        print(f"[tides] error: {exc}")
        return []


@app.get("/daypack")
def daypack(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    date_str: str = Query(..., description="YYYY-MM-DD"),
    tzname: str = Query("Australia/Brisbane", description="IANA timezone"),
):
    d = datetime.fromisoformat(date_str).date()
    events = []
    events += sun_block(lat, lon, tzname, d)
    events += moon_block(lat, lon, tzname, d)
    events += phases_perigee_apogee(tzname, d)
    events += tides(lat, lon, tzname, d)
    events.sort(key=lambda x: x["time_local"])
    return {"events": events, "meta": {"lat": lat, "lon": lon, "date": date_str, "tz": tzname}}

# ---------- Kp endpoints ----------
NOAA_KP_1M = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

def kp_to_g(kp: float) -> Optional[str]:
    if kp >= 9: return "G5"
    if kp >= 8: return "G4"
    if kp >= 7: return "G3"
    if kp >= 6: return "G2"
    if kp >= 5: return "G1"
    return None

@app.get("/kp")
def kp_last_3_days():
    r = requests.get(NOAA_KP_1M, timeout=20)
    r.raise_for_status()
    data = r.json()
    series = []
    for row in data:
        t = row.get("time_tag")
        est = row.get("estimated_kp")
        if t is None or est is None:
            continue
        series.append({"time_utc": t, "kp": float(est)})
    series.sort(key=lambda x: x["time_utc"])
    last = series[-1] if series else None
    g = kp_to_g(last["kp"]) if last else None
    return {"series": series, "last_point": last, "g_level": g}

@app.get("/kp_line")
def kp_line():
    r = requests.get(NOAA_KP_1M, timeout=20)
    r.raise_for_status()
    data = r.json()
    xs, ys = [], []
    for row in data:
        t = row.get("time_tag")
        est = row.get("estimated_kp")
        if t is None or est is None:
            continue
        xs.append(t)
        ys.append(float(est))
    return {"x": xs, "y": ys}

# ---------- Optional: Push via Firebase Admin ----------
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON")
FIREBASE_ENABLED = bool(FIREBASE_CRED_JSON)
if FIREBASE_ENABLED:
    import firebase_admin
    from firebase_admin import credentials, messaging
    cred = credentials.Certificate(json.loads(FIREBASE_CRED_JSON))
    try:
        firebase_admin.initialize_app(cred)
    except ValueError:
        pass

DEVICE_TOKENS = set()

@app.post("/register_device")
def register_device(token: str):
    DEVICE_TOKENS.add(token)
    return {"ok": True, "count": len(DEVICE_TOKENS)}

@app.post("/unregister_device")
def unregister_device(token: str):
    DEVICE_TOKENS.discard(token)
    return {"ok": True, "count": len(DEVICE_TOKENS)}

def send_push_all(title: str, body: str):
    if not FIREBASE_ENABLED or not DEVICE_TOKENS:
        return
    try:
        from firebase_admin import messaging
    except Exception:
        return
    for token in list(DEVICE_TOKENS):
        try:
            messaging.send(messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token
            ))
        except Exception:
            DEVICE_TOKENS.discard(token)

LAST_SENT_LEVEL = None

def kp_watch_loop():
    global LAST_SENT_LEVEL
    while True:
        try:
            resp = requests.get(NOAA_KP_1M, timeout=20).json()
            if resp:
                row = resp[-1]
                kp_val = float(row.get("estimated_kp", 0))
                g = kp_to_g(kp_val)
                order = {"G1":1,"G2":2,"G3":3,"G4":4,"G5":5}
                if g and (LAST_SENT_LEVEL is None or order[g] > order.get(LAST_SENT_LEVEL, 0)):
                    send_push_all(f"Geomagnetic storm {g}", f"Current Kp ≈ {kp_val:.1f}")
                    LAST_SENT_LEVEL = g
                if not g:
                    LAST_SENT_LEVEL = None
        except Exception:
            pass
        time.sleep(300)

def start_kp_thread():
    t = threading.Thread(target=kp_watch_loop, daemon=True)
    t.start()

@app.on_event("startup")
def on_startup():
    start_kp_thread()
