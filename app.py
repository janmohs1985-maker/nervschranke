"""Nervschranke – Seite, Zugdaten-Zwischenspeicher, Meldungen und Admin-Bereich.

Läuft hinter Traefik. Daten (SQLite, Schlüssel) liegen in /data.
Das Admin-Passwort kommt aus der Umgebung (ADMIN_PASSWORD), es steht nie im Code.
"""
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import statistics
import threading
import time
import xml.etree.ElementTree as ET
import zlib

import checks
import model
import stats
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
# strip(): Leerzeichen oder ein Windows-Zeilenende (\r) aus der .env-Datei sollen das Anmelden nicht verhindern
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip().strip('"').strip("'")
TRANSITOUS = "https://api.transitous.org/api/v1/map/trips"
CROSSINGS = ("elchinger", "wald", "fichten")
STATES = ("zu", "auf", "zug")

# Schätzwerte in Sekunden (wie bisher in der Seite). Fälle:
#   durch = Zug fährt durch, ab = fährt in Nersingen ab und dann über den Übergang,
#   an = fährt über den Übergang und hält dann in Nersingen.
DEFAULT_CONFIG = {
    "crossings": {
        "elchinger": {"durch_before": 120, "durch_before_long": 90, "durch_after": 30,
                      "ab_before": 250, "ab_after": 55, "an_before": 120, "an_after": 45, "an_after_dep": 60, "stay_closed": 60},
        "wald": {"durch_before": 120, "durch_before_long": 90, "durch_after": 30,
                 "ab_before": 250, "ab_after": 55, "an_before": 120, "an_after": 45, "an_after_dep": 60, "stay_closed": 60},
        "fichten": {"durch_before": 80, "durch_before_long": 60, "durch_after": 20,
                    "ab_before": 250, "ab_after": 55, "an_before": 80, "an_after": 45, "an_after_dep": 60, "stay_closed": 60},
    },
    "visitors": {"confirm": 2, "window": 90, "rate": 60, "live": True},
    # Selbst lernen aus Jans genauen Messungen: Halbwertszeit (Tage) und „Vorwissen“ (so viele Messungen wiegt
    # der manuelle Wert – mit wenigen Messungen bleibt es nah daran, mit vielen folgt es den Messungen)
    "learn": {"auto": True, "halflife": 30, "prior": 4, "visitors": True, "visitorWeight": 0.3},
    # Zeitkorrektur in Sekunden (+ = Zug kommt später als berechnet), gelernt aus „Zug fährt drüber“.
    # Gruppen: fern = Fernzug, regio = Regionalzug ohne Halt, halt = hält in Nersingen; _w = Richtung Ulm, _o = Richtung Augsburg
    "timing": {"fern_w": 0, "fern_o": 0, "regio_w": 0, "regio_o": 0, "halt_w": 0, "halt_o": 0},
}
PARAM_KEYS = tuple(DEFAULT_CONFIG["crossings"]["elchinger"].keys())
TIMING_KEYS = tuple(DEFAULT_CONFIG["timing"].keys())


def timing_group(long, stops, westbound):
    return ("fern" if long else "halt" if stops else "regio") + ("_w" if westbound else "_o")

# ---------------------------------------------------------------- Speicher

_secret_file = DATA / "secret"
if not _secret_file.exists():
    _secret_file.write_text(secrets.token_hex(32))
SECRET = _secret_file.read_text().strip().encode()

_db_lock = threading.Lock()
_db = sqlite3.connect(DATA / "nervschranke.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  crossing TEXT NOT NULL,
  state TEXT NOT NULL,
  source TEXT NOT NULL,
  device TEXT,
  iphash TEXT,
  snapshot TEXT,
  ignored INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reports_ts ON reports(ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- gesperrte Besucher (Geräte-Kennung oder Anschluss-Code): ihre Meldungen werden still angenommen, zählen aber nicht
CREATE TABLE IF NOT EXISTS blocked (k TEXT PRIMARY KEY, ts REAL NOT NULL);
-- Besucherzählung ohne Cookies: vhash = täglich wechselnder Code aus IP + Browser, wird nach 2 Tagen gelöscht
CREATE TABLE IF NOT EXISTS visits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  day TEXT NOT NULL,
  hour INTEGER NOT NULL,
  kind TEXT NOT NULL,
  vhash TEXT,
  mobile INTEGER NOT NULL DEFAULT 0,
  ref TEXT
);
CREATE INDEX IF NOT EXISTS visits_day ON visits(day);
-- Verlauf der Rechnung (jede Minute, gepackt) und Auffälligkeiten der Plausibilitätsprüfung – nur Zugdaten
CREATE TABLE IF NOT EXISTS history (ts INTEGER PRIMARY KEY, data BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  key TEXT NOT NULL,
  kind TEXT NOT NULL,
  crossing TEXT,
  train TEXT,
  detail TEXT,
  at REAL,
  raw INTEGER
);
CREATE INDEX IF NOT EXISTS anomalies_ts ON anomalies(ts);
CREATE INDEX IF NOT EXISTS anomalies_key ON anomalies(key);
CREATE TABLE IF NOT EXISTS rawdata (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, data BLOB NOT NULL);
""")
_db.commit()
_db.executescript(stats.SCHEMA)
_db.commit()
# exact=1: genauer Moment („geht gerade zu/auf“), exact=0: Beobachtung („ist schon zu/auf“, alle Besucher-Meldungen)
# traintype: beim Mitmessen „personen“ oder „gueter“ (bei „auf“: Art des letzten Zugs). Güterzüge stehen in
# keinem Fahrplan – ihre Messungen dürfen nicht einem Personenzug zugeordnet werden.
if "traintype" not in [r[1] for r in _db.execute("PRAGMA table_info(reports)")]:
    _db.execute("ALTER TABLE reports ADD COLUMN traintype TEXT")
    _db.commit()
if "exact" not in [r[1] for r in _db.execute("PRAGMA table_info(reports)")]:
    _db.execute("ALTER TABLE reports ADD COLUMN exact INTEGER NOT NULL DEFAULT 0")
    _db.execute("UPDATE reports SET exact=1 WHERE source='admin'")
    _db.commit()


def db(sql, args=(), one=False):
    with _db_lock:
        cur = _db.execute(sql, args)
        _db.commit()
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def get_config():
    row = db("SELECT v FROM kv WHERE k='config'", one=True)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if row:
        saved = json.loads(row["v"])
        for c in CROSSINGS:
            cfg["crossings"][c].update(saved.get("crossings", {}).get(c, {}))
        cfg["visitors"].update(saved.get("visitors", {}))
        cfg["learn"].update(saved.get("learn", {}))
        cfg["timing"].update({k: v for k, v in saved.get("timing", {}).items() if k in TIMING_KEYS})
    return cfg


def save_config(cfg):
    db("INSERT INTO kv(k, v) VALUES('config', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(cfg),))


def iphash(request: Request):
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")
    return hmac.new(SECRET, ip.encode(), hashlib.sha256).hexdigest()[:16]

# ---------------------------------------------------------------- Anmeldung


def make_token(days=60):
    exp = str(int(time.time() + days * 86400))
    sig = hmac.new(SECRET, ("admin|" + exp).encode(), hashlib.sha256).hexdigest()
    return exp + "." + sig


def is_admin(request: Request):
    tok = request.cookies.get("ns_admin", "")
    if "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(SECRET, ("admin|" + exp).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


def need_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(401, "nicht angemeldet")

# ---------------------------------------------------------------- App

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Sicherheits-Header für alle Antworten. Die Seite lädt nur von sich selbst – außer Kartenkacheln (OpenStreetMap)
# und, als Notlösung ohne Server, Zugdaten direkt von Transitous. Inline-Skripte/-Stile sind in den Seiten nötig.
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: https://tile.openstreetmap.org; font-src 'self'; "
                               "connect-src 'self' https://api.transitous.org; frame-ancestors 'self'; base-uri 'self'; "
                               "form-action 'self'; object-src 'none'",
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
}


@app.middleware("http")
async def security_headers(request, call_next):
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp
NOCACHE = {"Cache-Control": "no-cache"}


# ---------------------------------------------------------------- Besucherzählung

TZ = ZoneInfo("Europe/Berlin")
BOTS = ("bot", "crawl", "spider", "slurp", "curl", "wget", "python", "httpx", "go-http", "headless", "monitor")
PREVIEWS = ("whatsapp", "telegrambot", "facebookexternalhit", "twitterbot", "signal", "discordbot", "slackbot",
            "linkedinbot", "skypeuripreview", "applebot", "iframely", "embedly", "mastodon")
_active = {}          # vhash → zuletzt gesehen (nur im Speicher, für „gerade auf der Seite“)
_last_purge = [0.0]
_peak_mem = {}        # Tag → höchste Zahl gleichzeitig (Zwischenspeicher, damit nicht jeder Abruf schreibt)


def note_peak(n, ts):
    """Höchste Zahl gleichzeitiger Besucher je Tag und insgesamt merken (kv: peak:<Tag>, peak:all = "n|ts")."""
    day = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")
    if n <= _peak_mem.get(day, -1):
        return
    if day not in _peak_mem:
        row = db("SELECT v FROM kv WHERE k=?", ("peak:" + day,), one=True)
        _peak_mem[day] = int(row["v"].split("|")[0]) if row else 0
        if n <= _peak_mem[day]:
            return
    _peak_mem[day] = n
    db("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("peak:" + day, f"{n}|{ts:.0f}"))
    best = db("SELECT v FROM kv WHERE k='peak:all'", one=True)
    if not best or n > int(best["v"].split("|")[0]):
        db("INSERT INTO kv(k, v) VALUES('peak:all', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (f"{n}|{ts:.0f}",))
    for d in [d for d in _peak_mem if d < day]:
        _peak_mem.pop(d, None)


def visitor_hash(request: Request, day: str):
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or (request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    return hmac.new(SECRET, f"{day}|{ip}|{ua}".encode(), hashlib.sha256).hexdigest()[:16]


def record_visit(request: Request):
    if is_admin(request):
        return
    ua = (request.headers.get("user-agent") or "").lower()
    kind = "preview" if any(p in ua for p in PREVIEWS) else "bot" if (not ua or any(b in ua for b in BOTS)) else "view"
    if kind == "bot":
        return
    now = datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    ref = None
    r = request.headers.get("referer") or ""
    if r:
        host = urlparse(r).hostname or ""
        if host and not host.endswith("nervschranke.de") and not host.endswith("mohs.es"):
            ref = host.removeprefix("www.")[:60]
    mobile = 1 if any(m in ua for m in ("iphone", "android", "mobile")) else 0
    db("INSERT INTO visits(ts, day, hour, kind, vhash, mobile, ref) VALUES(?,?,?,?,?,?,?)",
       (time.time(), day, now.hour, kind, visitor_hash(request, day) if kind == "view" else None, mobile, ref))
    if time.time() - _last_purge[0] > 3600:
        _last_purge[0] = time.time()
        purge_visitor_codes()


def purge_visitor_codes():
    """Tagescodes älter als gestern löschen – vorher die Zahl verschiedener Besucher je Tag merken."""
    keep_from = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    for row in db("SELECT day, COUNT(DISTINCT vhash) AS n FROM visits WHERE vhash IS NOT NULL AND day < ? GROUP BY day", (keep_from,)):
        db("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("uniq:" + row["day"], str(row["n"])))
    db("UPDATE visits SET vhash=NULL WHERE vhash IS NOT NULL AND day < ?", (keep_from,))
    db("DELETE FROM visits WHERE ts < ?", (time.time() - 400 * 86400,))


@app.get("/")
def index(request: Request):
    try:
        record_visit(request)
    except Exception:
        pass  # Zählen darf die Seite nie stören
    return FileResponse(ROOT / "index.html", headers=NOCACHE)


@app.get("/statistik")
def statistik_page(request: Request):
    try:
        record_visit(request)
    except Exception:
        pass
    return FileResponse(ROOT / "statistik.html", headers=NOCACHE)


@app.get("/impressum")
def impressum():
    return FileResponse(ROOT / "impressum.html", headers=NOCACHE)


@app.get("/datenschutz")
def datenschutz():
    return FileResponse(ROOT / "datenschutz.html", headers=NOCACHE)


@app.get("/favicon.ico")
def favicon():
    return FileResponse(ROOT / "static" / "favicon-32.png", media_type="image/png")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/core.js")
def core_js():
    return FileResponse(ROOT / "core.js", media_type="text/javascript", headers=NOCACHE)


@app.get("/admin")
def admin_page(request: Request):
    page = "admin.html" if is_admin(request) else "login.html"
    return FileResponse(ROOT / page, headers=NOCACHE)


_login_fail = {}


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    key = iphash(request)
    if _login_fail.get(key, 0) > time.time():
        await asyncio.sleep(2)
        return RedirectResponse("/admin?fehler=warte", status_code=303)
    pw = str(form.get("passwort", "")).strip()
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        _login_fail[key] = time.time() + 5
        await asyncio.sleep(1)
        return RedirectResponse("/admin?fehler=1", status_code=303)
    r = RedirectResponse("/admin", status_code=303)
    r.set_cookie("ns_admin", make_token(), max_age=60 * 86400, httponly=True, secure=True, samesite="strict")
    return r


@app.get("/admin/logout")
def admin_logout():
    r = RedirectResponse("/", status_code=303)
    r.delete_cookie("ns_admin")
    return r

# ---------------------------------------------------------------- Zugdaten (30 s zwischengespeichert)

_trips_cache = {}
_trips_lock = asyncio.Lock()
# Transitous möchte Name, Version und Kontakt im User-Agent
_http = httpx.AsyncClient(timeout=25, headers={"User-Agent": "Nervschranke/1.1 (+https://nervschranke.de; jan@mohs.es)"})


# Zwei Abfragen: (1) kleiner Ausschnitt um Nersingen für Regionalzüge, (2) ganzer Abschnitt Ulm–Augsburg
# für Fernzüge. Die ICE-Abschnitte Ulm↔Augsburg liefert Transitous beim kleinen Ausschnitt nämlich nicht
# (geprüft 6.10.2026: nur 2 von ~8 ICE in 2 h), obwohl ihr Fahrweg genau über die Übergänge führt.
NEAR = {"min": "48.405,10.06", "max": "48.45,10.17", "zoom": "15"}
WIDE = {"min": "48.30,9.95", "max": "48.50,10.95", "zoom": "6"}   # Zoom 6 = nur Fernverkehr
LONG_MODES = ("HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL")


@app.get("/api/trips")
async def trips(request: Request):
    start, end = request.query_params.get("startTime", ""), request.query_params.get("endTime", "")
    if not start or not end:
        raise HTTPException(400, "startTime/endTime fehlt")
    body, how = await trips_body(start, end)
    return Response(body, media_type="application/json", headers={"X-Cache": how})


async def trips_body(start, end):
    """Transitous-Abschnitte (Regional + Fernzüge) als JSON-Bytes, 30 s zwischengespeichert."""
    q = start + "|" + end
    hit = _trips_cache.get(q)
    if hit and hit[0] > time.time():
        return hit[1], "HIT"
    async with _trips_lock:
        hit = _trips_cache.get(q)
        if hit and hit[0] > time.time():
            return hit[1], "HIT"
        try:
            near, wide = await asyncio.gather(
                _http.get(TRANSITOUS, params={**NEAR, "startTime": start, "endTime": end}),
                _http.get(TRANSITOUS, params={**WIDE, "startTime": start, "endTime": end}))
            near.raise_for_status()
            segs = [s for s in near.json() if s.get("mode") not in ("BUS", "COACH", "AIRPLANE", "TRAM", "FERRY")]
            if wide.status_code == 200:
                seen = {(s["trips"][0].get("tripId"), s.get("departure")) for s in segs if s.get("trips")}
                for s in wide.json():
                    key = (s["trips"][0].get("tripId"), s.get("departure")) if s.get("trips") else None
                    if s.get("mode") in LONG_MODES and key not in seen:
                        segs.append(s)
                        seen.add(key)
            body = json.dumps(segs).encode()
        except Exception:
            if hit:  # lieber alte Daten als keine
                return hit[1], "STALE"
            raise HTTPException(502, "Transitous nicht erreichbar")
        for k in [k for k, v in _trips_cache.items() if v[0] < time.time() - 300]:
            _trips_cache.pop(k, None)
        _trips_cache[q] = (time.time() + 30, body)
        return body, "MISS"

# ---------------------------------------------------------------- DB-Echtzeit (Timetables-API)
# Echte Ankunfts-/Abfahrtszeiten in Nersingen (RE/RB) und Ulm Hbf (ICE) – genauer als Transitous
# (z. B. 6.10.2026: RB 15 in Nersingen Abfahrt 13:07 statt 13:06, Transitous kannte das nicht).
# Schlüssel aus db.env; ohne Schlüssel liefert /api/realtime einfach nichts.
DB_API = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"
DB_STATIONS = {"nersingen": "8004239", "ulm": "8000170"}
_db_plan_cache = {}     # (eva, yymmddHH) → (gültig bis, [stops])
_db_rt_cache = [0.0, None]
_db_lock_rt = asyncio.Lock()


def _db_headers():
    cid, key = os.environ.get("DB_CLIENT_ID", "").strip(), os.environ.get("DB_API_KEY", "").strip()
    return {"DB-Client-Id": cid, "DB-Api-Key": key, "Accept": "application/xml"} if cid and key else None


def _db_ts(s):
    """'2610061327' → Unix-Zeit (Europe/Berlin)."""
    try:
        return datetime.strptime(s, "%y%m%d%H%M").replace(tzinfo=TZ).timestamp()
    except (TypeError, ValueError):
        return None


def _db_parse(xml_text):
    out = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for s in root.findall("s"):
        sid = s.get("id")
        if not sid:
            continue
        e = out.setdefault(sid, {})
        tl = s.find("tl")
        if tl is not None:
            e["n"], e["c"] = tl.get("n"), tl.get("c")
        for k in ("ar", "dp"):
            x = s.find(k)
            if x is None:
                continue
            d = e.setdefault(k, {})
            if x.get("pt"):
                d["pt"] = _db_ts(x.get("pt"))
            if x.get("ct"):
                d["ct"] = _db_ts(x.get("ct"))
            if x.get("ppth"):
                d["path"] = x.get("ppth")
            if x.get("pp") or x.get("cp"):   # Gleis (geplant / geändert)
                d["pl"] = x.get("cp") or x.get("pp")
            if x.get("cs") == "c":
                d["cancel"] = True
    return out


async def _db_station(eva, hdr):
    now = datetime.now(TZ)
    stops = {}
    for h in range(-1, 3):  # Plan der vorigen, aktuellen und nächsten zwei Stunden (Plan ändert sich kaum → 15 Min. merken)
        slot = (now + timedelta(hours=h)).strftime("%y%m%d%H")
        hit = _db_plan_cache.get((eva, slot))
        if hit and hit[0] > time.time():
            plan = hit[1]
        else:
            r = await _http.get(f"{DB_API}/plan/{eva}/{slot[:6]}/{slot[6:]}", headers=hdr)
            plan = _db_parse(r.text) if r.status_code == 200 else {}
            _db_plan_cache[(eva, slot)] = (time.time() + 900, plan)
        for sid, e in plan.items():
            stops[sid] = json.loads(json.dumps(e))
    r = await _http.get(f"{DB_API}/fchg/{eva}", headers=hdr)
    if r.status_code == 200:
        for sid, ch in _db_parse(r.text).items():
            if sid not in stops:
                continue
            for k in ("ar", "dp"):
                if k in ch:
                    stops[sid].setdefault(k, {}).update({kk: vv for kk, vv in ch[k].items() if kk in ("ct", "cancel", "pl")})
    # nach Zugnummer, Zeiten in ms für die Seite
    out = {}
    for e in stops.values():
        if not e.get("n"):
            continue
        item = {"cat": e.get("c")}
        for k in ("ar", "dp"):
            if k in e and e[k].get("pt"):
                item[k] = {"pt": int(e[k]["pt"] * 1000), "ct": int((e[k].get("ct") or e[k]["pt"]) * 1000),
                           "cancel": bool(e[k].get("cancel")), "path": e[k].get("path", ""), "pl": e[k].get("pl")}
        out[e["n"]] = item
    return out


@app.get("/api/realtime")
async def realtime():
    return JSONResponse(await realtime_data(), headers=NOCACHE)


async def realtime_data():
    hdr = _db_headers()
    if not hdr:
        return {"ok": False}
    if _db_rt_cache[1] and _db_rt_cache[0] > time.time():
        return _db_rt_cache[1]
    async with _db_lock_rt:
        if _db_rt_cache[1] and _db_rt_cache[0] > time.time():
            return _db_rt_cache[1]
        try:
            data = {"ok": True, "ts": time.time()}
            for key, eva in DB_STATIONS.items():
                data[key] = await _db_station(eva, hdr)
        except Exception:
            data = _db_rt_cache[1] or {"ok": False}
        _db_rt_cache[0], _db_rt_cache[1] = time.time() + 10, data
        for k in [k for k, v in _db_plan_cache.items() if v[0] < time.time() - 3600]:
            _db_plan_cache.pop(k, None)
        return data


# ---------------------------------------------------------------- Zentrale Rechnung
# Der Server rechnet die Schranken für alle (Webseite, iPhone-App, Widgets): Transitous + DB-Echtzeit +
# gelernte Schätzwerte → je Übergang Durchfahrten mit Schließzeiten und die Züge für Gleisband/Karte.
_state_cache = [0.0, None]
_state_lock = asyncio.Lock()


async def compute_state():
    """Rechnung wie für die Seite. Liefert (Ergebnis, Transitous-Rohdaten, DB-Echtzeit)."""
    now = int(time.time() // 60 * 60)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body, _ = await trips_body(iso(now - 1800), iso(now + 5400))
    rt = await realtime_data()
    cfg = effective_config()
    out = model.compute(json.loads(body), rt, cfg["crossings"], cfg["timing"])
    out.update({"ok": True, "generated": int(time.time() * 1000), "db": bool(rt.get("ok")),
                "visitors": cfg["visitors"], "model": cfg["crossings"]})
    return out, body, rt


@app.get("/api/state")
async def state():
    if _state_cache[1] and _state_cache[0] > time.time():
        return Response(_state_cache[1], media_type="application/json", headers=NOCACHE)
    async with _state_lock:
        if _state_cache[1] and _state_cache[0] > time.time():
            return Response(_state_cache[1], media_type="application/json", headers=NOCACHE)
        out, _, _ = await compute_state()
        out.pop("dropped", None)
        data = json.dumps(out, separators=(",", ":")).encode()
        _state_cache[0], _state_cache[1] = time.time() + 10, data
        return Response(data, media_type="application/json", headers=NOCACHE)

# ---------------------------------------------------------------- Verlauf + Plausibilitätsprüfung
# Jede Minute rechnet der Server einmal (auch ohne Besucher), merkt sich die Durchfahrten (3 Tage) und prüft
# sie mit checks.py. Bei einer Auffälligkeit werden auch die Rohdaten (Transitous + DB) aufgehoben (14 Tage),
# damit sich der Fall später genau nachrechnen lässt. Nur Zugdaten, nichts über Besucher.
# Abrufbar mit Admin-Anmeldung unter /api/admin/diag (Auffälligkeiten), /api/admin/diag/at (Stand zu einem
# Zeitpunkt), /api/admin/diag/raw/<id> (Rohdaten).
HISTORY_DAYS, ANOMALY_DAYS = 3, 14
_prev_state = [None]


def _pack(obj):
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode(), 6)


def _unpack(blob):
    return json.loads(zlib.decompress(blob))


def _slim(out):
    """Nur was zum Nachvollziehen nötig ist (ohne Fahrwege)."""
    keep = ("label", "num", "westbound", "case", "src", "delay", "shift", "pass", "closeFrom", "closeTo", "stationDep", "id")
    return {"generated": out["generated"], "db": out["db"], "dropped": out.get("dropped", []),
            "passes": {c: [{k: p[k] for k in keep if k in p} for p in l] for c, l in out["passes"].items()}}


def run_checks(out, body, rt):
    now = out["generated"]
    db("INSERT OR REPLACE INTO history(ts, data) VALUES(?, ?)", (now // 1000, _pack(_slim(out))))
    stats.log_passes(db, out["passes"], now)   # Fahrtenbuch für /statistik
    found = checks.check(out, _prev_state[0], now)
    _prev_state[0] = out
    raw_id = None
    for a in found:
        # gleiche Auffälligkeit (Art, Übergang, Zug, Zeitpunkt auf 5 Min.) nur einmal
        key = f"{a['kind']}|{a['crossing']}|{a['train']}|{(a['at'] or 0) // 300000}"
        if db("SELECT 1 FROM anomalies WHERE key=? AND ts > ?", (key, now / 1000 - 3600), one=True):
            continue
        if raw_id is None:
            with _db_lock:
                raw_id = _db.execute("INSERT INTO rawdata(ts, data) VALUES(?, ?)",
                                     (now / 1000, _pack({"trips": json.loads(body), "rt": rt}))).lastrowid
                _db.commit()
        db("INSERT INTO anomalies(ts, key, kind, crossing, train, detail, at, raw) VALUES(?,?,?,?,?,?,?,?)",
           (now / 1000, key, a["kind"], a["crossing"], a["train"], a["detail"], (a["at"] or 0) / 1000 or None, raw_id))
    if datetime.now(TZ).minute == 0:
        db("DELETE FROM history WHERE ts < ?", (time.time() - HISTORY_DAYS * 86400,))
        db("DELETE FROM anomalies WHERE ts < ?", (time.time() - ANOMALY_DAYS * 86400,))
        db("DELETE FROM rawdata WHERE ts < ?", (time.time() - ANOMALY_DAYS * 86400,))
        stats.purge(db)


async def watch_loop():
    while True:
        await asyncio.sleep(60 - time.time() % 60 + 5)   # kurz nach jeder vollen Minute
        try:
            out, body, rt = await compute_state()
            await asyncio.to_thread(run_checks, out, body, rt)
        except Exception as e:  # Prüfen darf nie die Seite stören
            print("Prüfung fehlgeschlagen:", repr(e))


@app.on_event("startup")
async def start_watch():
    asyncio.create_task(watch_loop())
    try:   # Fahrtenbuch einmalig aus den gespeicherten Minuten-Ständen füllen
        n = await asyncio.to_thread(stats.backfill_from_history, db, _unpack)
        if n:
            print(f"Fahrtenbuch aus {n} gespeicherten Ständen nachgetragen")
    except Exception as e:
        print("Fahrtenbuch nachtragen fehlgeschlagen:", repr(e))


# ---------------------------------------------------------------- Statistik (öffentlich, nur Zugdaten)
_stats_cache = [0.0, None]


def forecast_accuracy(days=7):
    """Wie oft lag die Vorhersage richtig? Nach Jans Messungen (genauer Moment ±60 s, Beobachtung stimmte)."""
    cfg = get_config()
    rows = db("SELECT * FROM reports WHERE ts > ? AND ignored=0 AND source='admin' ORDER BY ts", (time.time() - days * 86400,))
    real = actual_passes(rows)
    hit = n = 0
    for r in rows:
        if r["state"] not in ("zu", "auf"):
            continue
        e = evaluate(r, cfg, real)
        if e["outlier"] or e["dev"] is None:
            continue
        n += 1
        hit += (abs(e["dev"]) <= 60) if e["exact"] else bool(e["match"])
    return {"pct": round(100 * hit / n), "n": n} if n >= 5 else None


@app.get("/api/stats")
async def stats_api():
    if _stats_cache[1] is None or _stats_cache[0] < time.time():
        out = await asyncio.to_thread(stats.compute, db, effective_config()["crossings"])
        out["accuracy"] = await asyncio.to_thread(forecast_accuracy)
        _stats_cache[0], _stats_cache[1] = time.time() + 60, json.dumps(out, separators=(",", ":")).encode()
    return Response(_stats_cache[1], media_type="application/json", headers=NOCACHE)


def _t(s):
    """Zeitpunkt aus Unix-Sekunden oder ISO (ohne Zone = deutsche Zeit)."""
    try:
        return float(s)
    except ValueError:
        d = datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()


def _local(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m. %H:%M:%S") if ts else None


@app.get("/api/admin/diag")
def diag(request: Request, hours: float = 24, kind: str = ""):
    """Auffälligkeiten der letzten Stunden (neueste zuerst)."""
    need_admin(request)
    since = time.time() - min(hours, ANOMALY_DAYS * 24) * 3600
    rows = db("SELECT * FROM anomalies WHERE ts > ? AND (?='' OR kind=?) ORDER BY ts DESC LIMIT 500", (since, kind, kind))
    count = {}
    for r in rows:
        count[r["kind"]] = count.get(r["kind"], 0) + 1
    last = db("SELECT MAX(ts) AS t FROM history", one=True)
    return JSONResponse({"lastCheck": _local(last["t"]) if last else None, "count": count,
                         "anomalies": [{"id": r["id"], "seen": _local(r["ts"]), "kind": r["kind"], "crossing": r["crossing"],
                                        "train": r["train"], "detail": r["detail"], "at": _local(r["at"]), "raw": r["raw"]}
                                       for r in rows]}, headers=NOCACHE)


@app.get("/api/admin/diag/at")
def diag_at(request: Request, t: str):
    """Stand der Rechnung zu einem Zeitpunkt (nächste gespeicherte Minute davor), Zeiten als deutsche Uhrzeit."""
    need_admin(request)
    try:
        ts = _t(t)
    except ValueError:
        raise HTTPException(400, "t = Unix-Sekunden oder ISO-Zeit, z. B. 2026-10-07T07:58")
    row = db("SELECT ts, data FROM history WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,), one=True)
    if not row or ts - row["ts"] > 600:
        raise HTTPException(404, "kein Stand gespeichert")
    s = _unpack(row["data"])
    hm = lambda ms: datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M:%S") if ms else None
    for c, l in s["passes"].items():
        s["passes"][c] = [{**p, "pass": hm(p["pass"]), "closeFrom": hm(p["closeFrom"]), "closeTo": hm(p["closeTo"]),
                           "stationDep": hm(p.get("stationDep"))} for p in l
                          if abs(p["pass"] / 1000 - ts) < 3600]
    for d in s["dropped"]:
        d["pass"] = hm(d["pass"])
    s["at"] = _local(row["ts"])
    return JSONResponse(s, headers=NOCACHE)


@app.get("/api/admin/diag/raw/{rid}")
def diag_raw(request: Request, rid: int):
    """Rohdaten (Transitous-Abschnitte + DB-Echtzeit) zu einer Auffälligkeit – zum Nachrechnen mit model.compute."""
    need_admin(request)
    row = db("SELECT ts, data FROM rawdata WHERE id=?", (rid,), one=True)
    if not row:
        raise HTTPException(404, "nicht gefunden")
    return Response(zlib.decompress(row["data"]), media_type="application/json", headers=NOCACHE)

# ---------------------------------------------------------------- Modell + Meldungen (öffentlich)


@app.get("/api/config")
def config_public():
    return JSONResponse(effective_config(), headers=NOCACHE)


def clean_snapshot(s):
    if not isinstance(s, dict):
        return None
    keep = {}
    for k in ("name", "kind", "case", "westbound", "long", "pass", "closeFrom", "closeTo", "delay", "stationDep", "shift", "stops"):
        if k in s:
            keep[k] = s[k]
    return json.dumps(keep)[:2000]


@app.post("/api/report")
async def report(request: Request):
    body = await request.json()
    crossing, state = body.get("crossing"), body.get("state")
    exact = bool(body.get("exact"))
    if crossing not in CROSSINGS or state not in ("zu", "auf", "zug") or (state == "zug" and not exact):
        raise HTTPException(400, "ungültig")
    device = str(body.get("device", ""))[:40]
    ip = iphash(request)
    cfg = get_config()["visitors"]
    now = time.time()
    if exact:
        # Mitmessen nach einer Meldung: „Zug kam jetzt“ (je Zug einmal, bei mehreren Zügen mehrmals) und zum
        # Schluss „Schranke ist offen“ – nur mit Geräte-Kennung, nur nach eigener „zu“-Meldung an diesem
        # Übergang (max. 20 Min. her). Nach „offen“ ist die Messung vorbei.
        if state == "zu" or not device:
            raise HTTPException(400, "ungültig")
        start = db("SELECT ts FROM reports WHERE source='visitor' AND exact=0 AND state='zu' AND device=? AND crossing=? AND ts > ? "
                   "ORDER BY ts DESC LIMIT 1", (device, crossing, now - 1200), one=True)
        if not start:
            raise HTTPException(409, "Bitte zuerst melden, dass die Schranke zu ist.")
        done = db("SELECT state, ts FROM reports WHERE source='visitor' AND exact=1 AND device=? AND ts > ?", (device, start["ts"]))
        trains = [r["ts"] for r in done if r["state"] == "zug"]
        if any(r["state"] == "auf" for r in done) or (state == "zug" and (len(trains) >= 5 or (trains and now - max(trains) < 5))):
            raise HTTPException(429, "Schon gemessen.")
    else:
        # pro Gerät 1 Meldung je `rate` Sekunden; pro Anschluss (IP) höchstens 5, damit z. B. eine Familie
        # im selben WLAN sich gegenseitig bestätigen kann, aber niemand die Seite zuspammt
        since = now - cfg["rate"]
        same_dev = device and db("SELECT 1 FROM reports WHERE source='visitor' AND exact=0 AND ts > ? AND device=?", (since, device), one=True)
        same_ip = db("SELECT COUNT(*) AS n FROM reports WHERE source='visitor' AND exact=0 AND ts > ? AND iphash=?", (since, ip), one=True)["n"]
        if same_dev or same_ip >= 5 or (not device and same_ip >= 1):
            raise HTTPException(429, "Du hast gerade schon gemeldet.")
    # gesperrt? Dann still annehmen (der Störer merkt nichts), aber nie zählen lassen.
    # Geräte bleiben gesperrt, Anschlüsse nur 24 h (Handy-Netze teilen sich IP-Adressen)
    blocked = db("SELECT 1 FROM blocked WHERE (k=? AND k != '') OR (k=? AND ts > ?)", ("d:" + device if device else "", "i:" + ip, now - 86400), one=True)
    traintype = body.get("train") if exact and body.get("train") in ("personen", "gueter") else None
    db("INSERT INTO reports(ts, crossing, state, source, device, iphash, snapshot, exact, ignored, traintype) VALUES(?,?,?,?,?,?,?,?,?,?)",
       (now, crossing, state, "visitor", device, ip, None if traintype == "gueter" else clean_snapshot(body.get("snapshot")),
        1 if exact else 0, 1 if blocked else 0, traintype))
    if exact:
        learn_invalidate()
    # Besucher-Meldungen höchstens 12 Monate aufheben (so steht es in der Datenschutzerklärung)
    db("DELETE FROM reports WHERE source='visitor' AND ts < ?", (time.time() - 365 * 86400,))
    return live()


_parsed_state = [None, None]


def train_just_passed(crossing, ts):
    """Ist laut Vorhersage kurz vor `ts` (höchstens 5 Min., bis 1 Min. danach) ein Zug über den Übergang gefahren?
    Nur dann zeigt die Seite ein „Schranke ist offen“ vom Mitmessen sofort an – sonst braucht es eine Bestätigung."""
    model_view(crossing, ts)   # sorgt dafür, dass der aktuelle Stand gelesen ist
    st = _parsed_state[1]
    if not st:
        return False
    t = ts * 1000
    return any(t - 300000 <= p["pass"] <= t + 60000 for p in st["passes"].get(crossing, []))


def model_view(crossing, ts):
    """Was sagt die Vorhersage für diesen Moment? ("zu"/"auf", Zug in ±10 Min.?) – None, wenn unbekannt."""
    if _parsed_state[0] != _state_cache[1]:
        try:
            _parsed_state[:] = [_state_cache[1], json.loads(_state_cache[1]) if _state_cache[1] else None]
        except ValueError:
            _parsed_state[:] = [_state_cache[1], None]
    st = _parsed_state[1]
    if not st:
        return None
    t = ts * 1000
    # kürzere Lücke zwischen zwei Zügen = bleibt zu (wie auf der Seite, je Übergang im Admin einstellbar)
    gap = (st.get("model", {}).get(crossing, {}).get("stay_closed") or 60) * 1000
    wins = []
    for p in sorted(st["passes"].get(crossing, []), key=lambda p: p["closeFrom"]):
        if wins and p["closeFrom"] - wins[-1][1] < gap:
            wins[-1][1] = max(wins[-1][1], p["closeTo"])
        else:
            wins.append([p["closeFrom"], p["closeTo"]])
    lo = min((p["pass"] for p in st["passes"].get(crossing, [])), default=None)
    if lo is None or t < lo - 1800_000:
        return None   # so alte Zeiten deckt der zwischengespeicherte Stand nicht ab
    closed = any(a <= t <= b for a, b in wins)
    near = any(a - 600_000 <= t <= b + 600_000 for a, b in wins)
    return {"closed": closed, "near": near}


def contradicts(r):
    """Widerspricht die Besucher-Meldung der Vorhersage deutlich? (auf mitten in einer Sperrung, zu ohne Zug weit und breit)"""
    m = model_view(r["crossing"], r["ts"])
    if m is None:
        try:
            s = json.loads(r["snapshot"]) if r["snapshot"] else None
        except ValueError:
            s = None
        if r["state"] == "auf":
            return bool(s and s.get("closeFrom", 0) <= r["ts"] * 1000 <= s.get("closeTo", 0))
        return s is None
    return (r["state"] == "auf" and m["closed"]) or (r["state"] == "zu" and not m["near"])


def confirmed_ids(rows, cfg):
    """Besucher-Meldungen zählen erst, wenn genug verschiedene Geräte dasselbe melden.
    Gegen Sabotage: Widerspricht die Meldung der Vorhersage, braucht es eine Bestätigung mehr – und zwar von
    verschiedenen Anschlüssen (Handy + Laptop im selben WLAN zählen als einer). Hat Jan an dem Übergang in den
    5 Minuten davor gemessen, zählen Besucher-Meldungen gegen seine Messung gar nicht."""
    ok = set()
    vis = [r for r in rows if r["source"] == "visitor" and not r["ignored"] and not r["exact"] and r["state"] in ("zu", "auf")]
    if not vis:
        return ok
    adm = db("SELECT ts, crossing, state FROM reports WHERE source='admin' AND ignored=0 AND ts BETWEEN ? AND ? ORDER BY ts",
             (min(r["ts"] for r in vis) - 300, max(r["ts"] for r in vis)))
    for r in vis:
        jan = [a for a in adm if a["crossing"] == r["crossing"] and r["ts"] - 300 <= a["ts"] <= r["ts"]]
        if jan:
            last = "zu" if jan[-1]["state"] in ("zu", "zug") else "auf"
            if last != r["state"]:
                continue
        others = [x for x in vis if x["crossing"] == r["crossing"] and x["state"] == r["state"] and abs(x["ts"] - r["ts"]) <= cfg["window"]]
        if contradicts(r):
            if len({x["iphash"] for x in others}) >= cfg["confirm"] + 1:
                ok.add(r["id"])
        elif len({x["device"] or x["iphash"] for x in others}) >= cfg["confirm"]:
            ok.add(r["id"])
    return ok


@app.get("/api/live")
def live(request: Request = None):
    """Meldungen der letzten 3 Minuten je Übergang."""
    cfg = get_config()["visitors"]
    now = time.time()
    # die Seite fragt alle 15 s hier nach → daraus „gerade auf der Seite“ (nur im Speicher)
    if request is not None and not is_admin(request):
        _active[visitor_hash(request, datetime.now(TZ).strftime("%Y-%m-%d"))] = now
        for k in [k for k, v in _active.items() if v < now - 60]:
            _active.pop(k, None)
        note_peak(len(_active), now)
    # Beobachtungen, dazu genaue Momente: Jans „geht zu/auf“ und beim Mitmessen „Schranke ist offen“ – wer zu gemeldet,
    # den Zug gesehen und dann „offen“ getippt hat, steht davor; das gilt sofort für alle (beobachtet 7.10.2026 15:53,
    # Elchinger: nach dem ICE war sie offen, die Seite zeigte wegen einer verspäteten RB noch zu)
    rows = db("SELECT * FROM reports WHERE ts > ? AND ignored=0 AND state IN ('zu','auf') AND "
              "(exact=0 OR source='admin' OR (source='visitor' AND state='auf')) ORDER BY ts", (now - 180,))
    conf = confirmed_ids(rows, cfg)
    out = {}
    for c in CROSSINGS:
        mine = [r for r in rows if r["crossing"] == c]
        win = [r for r in mine if r["ts"] > now - cfg["window"] and r["source"] == "visitor" and not r["exact"]]
        trusted = [r for r in mine if r["source"] == "admin" or r["id"] in conf
                   or (r["exact"] and r["state"] == "auf" and train_just_passed(c, r["ts"]))]
        last = trusted[-1] if trusted else None
        out[c] = {
            "zu": len({r["device"] or r["iphash"] for r in win if r["state"] == "zu"}),
            "auf": len({r["device"] or r["iphash"] for r in win if r["state"] == "auf"}),
            "state": last["state"] if last else None,
            "at": last["ts"] if last else None,
        }
    return JSONResponse({"live": cfg["live"], "crossings": out, "now": now}, headers=NOCACHE)

# ---------------------------------------------------------------- Admin


@app.post("/api/admin/report")
async def admin_report(request: Request):
    need_admin(request)
    body = await request.json()
    crossing, state = body.get("crossing"), body.get("state")
    if crossing not in CROSSINGS or state not in STATES:
        raise HTTPException(400, "ungültig")
    exact = 1 if body.get("exact", True) or state == "zug" else 0
    # Zugart beim Messen: „personen“ oder „gueter“ (bei „auf“ die des letzten Zugs) – Güterzüge zählen nicht fürs Lernen
    traintype = body.get("train") if exact and body.get("train") in ("personen", "gueter") else None
    now = time.time()
    with _db_lock:
        # doppelt getippt: dieselbe Messung am selben Übergang innerhalb einer Minute zählt nur einmal
        # (Zug drüber: nur 5 s – es können mehrere Züge kurz nacheinander kommen)
        dup = _db.execute("SELECT id FROM reports WHERE source='admin' AND crossing=? AND state=? AND exact=? AND ts > ? "
                          "ORDER BY ts DESC LIMIT 1", (crossing, state, exact, now - (5 if state == "zug" else 60))).fetchone()
        if dup:
            return {"ok": True, "id": dup["id"], "dup": True}
        cur = _db.execute("INSERT INTO reports(ts, crossing, state, source, device, iphash, snapshot, exact, traintype) VALUES(?,?,?,?,?,?,?,?,?)",
                          (now, crossing, state, "admin", "jan", "", None if traintype == "gueter" else clean_snapshot(body.get("snapshot")),
                           exact, traintype))
        _db.commit()
        rid = cur.lastrowid
    learn_invalidate()
    return {"ok": True, "id": rid}


@app.delete("/api/admin/report/{rid}")
def admin_delete(rid: int, request: Request):
    need_admin(request)
    db("DELETE FROM reports WHERE id=?", (rid,))
    learn_invalidate()
    return {"ok": True}


@app.post("/api/admin/report/{rid}/ignore")
async def admin_ignore(rid: int, request: Request):
    need_admin(request)
    body = await request.json()
    db("UPDATE reports SET ignored=? WHERE id=?", (1 if body.get("ignored") else 0, rid))
    learn_invalidate()
    return {"ok": True}


@app.post("/api/admin/report/{rid}/block")
def admin_block(rid: int, request: Request):
    """Besucher hinter dieser Meldung sperren: Gerät dauerhaft, Anschluss 24 h; seine Meldungen der letzten 24 h zählen nicht mehr."""
    need_admin(request)
    r = db("SELECT * FROM reports WHERE id=? AND source='visitor'", (rid,), one=True)
    if not r:
        raise HTTPException(404, "nicht gefunden")
    now = time.time()
    for k in (["d:" + r["device"]] if r["device"] else []) + (["i:" + r["iphash"]] if r["iphash"] else []):
        db("INSERT OR REPLACE INTO blocked(k, ts) VALUES(?, ?)", (k, now))
    db("UPDATE reports SET ignored=1 WHERE source='visitor' AND ts > ? AND ((device=? AND device != '') OR iphash=?)",
       (now - 86400, r["device"] or "", r["iphash"] or ""))
    learn_invalidate()
    return {"ok": True}


def actual_passes(rows):
    """(Übergang, geschätzte Durchfahrt) → echte Durchfahrt laut „Zug fährt drüber“."""
    out = {}
    for r in rows:
        if r["state"] != "zug" or r["ignored"] or not r["snapshot"] or r["traintype"] == "gueter":
            continue
        try:
            s = json.loads(r["snapshot"])
        except ValueError:
            continue
        if s.get("pass") and abs(r["ts"] - s["pass"] / 1000) < 600:
            out[(r["crossing"], round(s["pass"] / 1000))] = r["ts"]
    return out


def evaluate(r, cfg, actual_pass=None):
    """Abweichung einer Meldung zum Modell und welcher Wert damit gemessen wurde."""
    try:
        s = json.loads(r["snapshot"]) if r["snapshot"] else None
    except ValueError:
        s = None
    exact = bool(r["exact"]) or r["state"] == "zug"
    out = {"id": r["id"], "ts": r["ts"], "crossing": r["crossing"], "state": r["state"], "source": r["source"],
           "exact": exact, "match": None, "ignored": bool(r["ignored"]), "train": None, "dev": None, "param": None,
           "measured": None, "outlier": False}
    out["traintype"] = r["traintype"]
    if r["traintype"] == "gueter":
        # Güterzug (oder „auf“ direkt nach einem): steht in keinem Fahrplan, zählt nicht fürs Lernen
        out["outlier"], out["why"] = True, "Güterzug – nicht im Fahrplan"
        return out
    if not s or "pass" not in s:
        if not exact and r["state"] == "auf":  # „ist auf“ ohne Zug in der Nähe – passt zur Schätzung
            out["match"], out["dev"] = True, 0.0
            return out
        out["outlier"] = True
        out["why"] = "kein Zug in der Nähe"
        return out
    ps, cf, ct = s["pass"] / 1000, s.get("closeFrom", 0) / 1000, s.get("closeTo", 0) / 1000
    out["train"] = {"name": s.get("name"), "kind": s.get("kind"), "westbound": s.get("westbound"), "case": s.get("case"),
                    "pass": ps}
    case = s.get("case") or "durch"
    if not exact:
        # Beobachtung: sagt nur, ob die Schätzung in diesem Moment stimmte – nicht, wann sie zu-/aufging
        ts, model_zu, seen_zu = r["ts"], cf <= r["ts"] <= ct, r["state"] == "zu"
        out["match"] = model_zu == seen_zu
        if out["match"]:
            out["dev"] = 0.0
        elif seen_zu:  # war zu, Schätzung sagt offen
            out["dev"] = ts - cf if ts < cf else ts - ct
        else:          # war auf, Schätzung sagt zu
            out["dev"] = ts - cf if ts - cf < ct - ts else ts - ct
        if abs(out["dev"]) > 600:
            out["outlier"], out["why"] = True, "über 10 Min. daneben"
        return out
    # Hat Jan „Zug fährt drüber“ für denselben Zug gemeldet, ist das die echte Durchfahrt – dann werden
    # die Schließzeiten getrennt vom Fehler im Fahrplan gemessen.
    real = actual_pass.get((r["crossing"], round(ps))) if actual_pass else None
    if real:
        out["realPass"] = real
    ref = real or ps
    if r["state"] == "zu":
        out["dev"] = r["ts"] - cf
        out["measured"] = ref - r["ts"]
        out["param"] = case + "_before" + ("_long" if case == "durch" and s.get("long") else "")
    elif r["state"] == "auf" and case == "an" and s.get("stationDep") and r["crossing"] != "elchinger":
        # Zug hält nach dem Übergang: Schranke geht erst nach seiner Abfahrt in Nersingen auf
        out["dev"] = r["ts"] - ct
        out["measured"] = r["ts"] - s["stationDep"] / 1000
        out["param"] = "an_after_dep"
    elif r["state"] == "auf":
        out["dev"] = r["ts"] - ct
        out["measured"] = r["ts"] - ref
        out["param"] = case + "_after"
    else:  # Zug fährt drüber: wie genau stimmt die Durchfahrtszeit?
        out["dev"] = r["ts"] - ps
        # die damals schon angewandte Korrektur zurückrechnen → Fehler der reinen Fahrplan-Rechnung
        stops = s.get("stops") if "stops" in s else case in ("an", "ab")
        out["tgroup"] = timing_group(bool(s.get("long")), bool(stops), bool(s.get("westbound")))
        out["tmeasured"] = out["dev"] + float(s.get("shift") or 0)
    if out["dev"] is not None and abs(out["dev"]) > 600:
        out["outlier"], out["why"] = True, "über 10 Min. daneben"
    if out["measured"] is not None and out["measured"] < -30:
        out["outlier"], out["why"] = True, "passt nicht zum Zug"
    return out


@app.get("/api/admin/reports")
def admin_reports(request: Request, days: float = 7):
    need_admin(request)
    cfg = get_config()
    rows = db("SELECT * FROM reports WHERE ts > ? ORDER BY ts DESC LIMIT 500", (time.time() - days * 86400,))
    conf = confirmed_ids(rows, cfg["visitors"])
    real = actual_passes(rows)
    out = []
    for r in rows:
        e = evaluate(r, cfg, real)
        e["confirmed"] = r["source"] == "admin" or r["id"] in conf
        out.append(e)
    return {"reports": out}


# ---------------------------------------------------------------- Selbst lernen (Stufe 1)
# Nur Jans genaue Messungen (Admin, „geht gerade zu/auf“) stellen die Schließzeiten ein. Neuere zählen mehr
# (Halbwertszeit), Ausreißer fliegen raus, und der manuelle Wert zählt wie `prior` Messungen mit.
_learn_cache = [0.0, None]


def learn_invalidate():
    _learn_cache[0] = 0.0
    try:
        _tlearn_cache[0] = 0.0
    except NameError:
        pass


def _wmedian(pairs):
    pairs = sorted(pairs)
    total, acc = sum(w for _, w in pairs), 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def corroborated(lst, tol):
    """Besucher-Messungen zählen erst, wenn jemand anderes ähnlich gemessen hat (Jan oder ein anderes Gerät,
    höchstens `tol` Sekunden daneben) – sonst könnte z. B. ein Güterzug, der als Personenzug getippt wurde,
    die Zeiten verziehen. Jans Messungen zählen immer. Einträge: (Wert, Zeit, Gewicht, Besucher?, Gerät)."""
    out = []
    for m in lst:
        if not m[3] or any(o is not m and (not o[3] or o[4] != m[4]) and abs(o[0] - m[0]) <= tol for o in lst):
            out.append(m)
    return out


def learned(cfg=None):
    if _learn_cache[1] is not None and _learn_cache[0] > time.time():
        return _learn_cache[1]
    cfg = cfg or get_config()
    lc = cfg["learn"]
    # Jans Messungen voll; Besucher-Messungen (Mitmessen) nur wenn erlaubt, mit kleinerem Gewicht
    # und nur, wenn sie plausibel sind (höchstens 3 Min. neben der Schätzung)
    rows = db("SELECT * FROM reports WHERE ignored=0 AND exact=1 AND ts > ? AND (source='admin' OR source='visitor') ORDER BY ts",
              (time.time() - 365 * 86400,))
    real = actual_passes(rows)
    vals = {}
    for r in rows:
        vis = r["source"] == "visitor"
        if vis and not lc.get("visitors", True):
            continue
        e = evaluate(r, cfg, real)
        if not e["exact"] or e["outlier"] or e["param"] not in PARAM_KEYS or e["measured"] is None:
            continue
        if vis and (e["dev"] is None or abs(e["dev"]) > 180):
            continue
        w = float(lc.get("visitorWeight", 0.3)) if vis else 1.0
        vals.setdefault((r["crossing"], e["param"]), []).append((e["measured"], r["ts"], w, vis, r["device"] or r["iphash"]))
    vals = {k: v for k, lst in vals.items() if (v := corroborated(lst, 45))}   # leer = nichts Bestätigtes
    out = {c: {} for c in CROSSINGS}
    now = time.time()
    for (c, k), lst in vals.items():
        v = [x for x, *_ in lst]
        if len(v) >= 5:  # Ausreißer: mehr als 3 × MAD (mind. 10 s) vom Median weg
            med = statistics.median(v)
            mad = max(10.0, statistics.median([abs(x - med) for x in v]))
            lst = [m for m in lst if abs(m[0] - med) <= 3 * mad] or lst
        pairs = [(x, w * 0.5 ** ((now - t) / 86400 / lc["halflife"])) for x, t, w, *_ in lst]
        W = sum(w for _, w in pairs)
        manual = cfg["crossings"][c][k]
        wmed = _wmedian(pairs)
        est = (W * wmed + lc["prior"] * manual) / (W + lc["prior"])
        out[c][k] = {"value": int(round(max(0, min(900, est)))), "n": len(lst), "conf": W / (W + lc["prior"]),
                     "median": wmed, "nVisitor": sum(1 for m in lst if m[3])}
    _learn_cache[0], _learn_cache[1] = time.time() + 60, out
    return out


_tlearn_cache = [0.0, None]


def learned_timing(cfg=None):
    """Zeitkorrektur je Gruppe aus „Zug fährt drüber“ (wie die Schließzeiten: neuere zählen mehr, Ausreißer raus)."""
    if _tlearn_cache[1] is not None and _tlearn_cache[0] > time.time():
        return _tlearn_cache[1]
    cfg = cfg or get_config()
    lc = cfg["learn"]
    rows = db("SELECT * FROM reports WHERE ignored=0 AND state='zug' AND ts > ? ORDER BY ts", (time.time() - 365 * 86400,))
    vals = {}
    for r in rows:
        vis = r["source"] == "visitor"
        if vis and not lc.get("visitors", True):
            continue
        e = evaluate(r, cfg)
        if e.get("tgroup") is None or e["outlier"] or abs(e["tmeasured"]) > 480:
            continue
        if vis and abs(e["dev"]) > 180:
            continue
        vals.setdefault(e["tgroup"], []).append((e["tmeasured"], r["ts"], float(lc.get("visitorWeight", 0.3)) if vis else 1.0,
                                                 vis, r["device"] or r["iphash"]))
    vals = {k: v for k, lst in vals.items() if (v := corroborated(lst, 60))}
    out, now = {}, time.time()
    for k, lst in vals.items():
        v = [x for x, *_ in lst]
        if len(v) >= 5:
            med = statistics.median(v)
            mad = max(10.0, statistics.median([abs(x - med) for x in v]))
            lst = [m for m in lst if abs(m[0] - med) <= 3 * mad] or lst
        pairs = [(x, w * 0.5 ** ((now - t) / 86400 / lc["halflife"])) for x, t, w, *_ in lst]
        W = sum(w for _, w in pairs)
        wmed = _wmedian(pairs)
        manual = cfg["timing"][k]
        est = (W * wmed + lc["prior"] * manual) / (W + lc["prior"])
        out[k] = {"value": int(round(max(-600, min(600, est)))), "n": len(lst), "conf": W / (W + lc["prior"]), "median": wmed,
                  "values": [round(x) for x, *_ in lst][-30:]}
    _tlearn_cache[0], _tlearn_cache[1] = time.time() + 60, out
    return out


def effective_config():
    """Was die Seite benutzt: bei „automatisch lernen“ die gelernten Werte, sonst die manuellen."""
    cfg = get_config()
    if cfg["learn"]["auto"]:
        for c, ks in learned(cfg).items():
            for k, l in ks.items():
                cfg["crossings"][c][k] = l["value"]
        for k, l in learned_timing(cfg).items():
            cfg["timing"][k] = l["value"]
    return cfg


@app.get("/api/admin/analysis")
def admin_analysis(request: Request, days: float = 60):
    need_admin(request)
    cfg = get_config()
    rows = db("SELECT * FROM reports WHERE ts > ? ORDER BY ts", (time.time() - days * 86400,))
    conf = confirmed_ids(rows, cfg["visitors"])
    real = actual_passes(rows)
    res = {}
    for c in CROSSINGS:
        params = {k: {"model": cfg["crossings"][c][k], "values": []} for k in PARAM_KEYS}
        devs, n_admin, n_vis, n_vis_ok = [], 0, 0, 0
        for r in rows:
            if r["crossing"] != c:
                continue
            if r["source"] == "admin":
                n_admin += 1
            else:
                n_vis += 1
                n_vis_ok += r["id"] in conf
            trusted = r["source"] == "admin" or r["id"] in conf
            e = evaluate(r, cfg, real)
            if not trusted or e["ignored"] or e["outlier"]:
                continue
            if e["dev"] is not None and r["state"] in ("zu", "auf"):
                devs.append({"ts": r["ts"], "dev": e["dev"], "source": r["source"], "exact": e["exact"], "match": e["match"]})
            if e["param"] in params and e["measured"] is not None:
                params[e["param"]]["values"].append(e["measured"])
        for k, p in params.items():
            v = sorted(p.pop("values"))
            p["n"] = len(v)
            if v:
                p["median"] = statistics.median(v)
                p["p25"] = v[len(v) // 4]
                p["p75"] = v[(len(v) * 3) // 4] if len(v) > 1 else v[0]
                p["suggest"] = int(round(p["median"] / 5) * 5) if len(v) >= 3 else None
        lr = learned(cfg).get(c, {})
        for k, p in params.items():
            p["learned"] = lr.get(k)
            p["effective"] = lr[k]["value"] if cfg["learn"]["auto"] and k in lr else p["model"]
        recent = [d for d in devs if d["ts"] > time.time() - 7 * 86400]
        recent_exact = [d for d in recent if d["exact"]]
        vis_obs = [d for d in recent if d["source"] == "visitor"]
        warn = {"n": len(vis_obs), "wrong": sum(1 for d in vis_obs if not d["match"])}
        # Treffer: Moment ±60 s genau, Beobachtung = Schätzung lag richtig (zu/auf)
        hits = [abs(d["dev"]) <= 60 if d["exact"] else bool(d["match"]) for d in recent]
        res[c] = {
            "params": params,
            "devs": devs[-200:],
            "hit": (sum(hits) / len(hits)) if hits else None,
            "meanDev": (sum(abs(d["dev"]) for d in recent_exact) / len(recent_exact)) if recent_exact else None,
            "warn": warn,
            "counts": {"admin": n_admin, "visitor": n_vis, "visitorConfirmed": n_vis_ok,
                       "exact": sum(1 for d in devs if d["exact"]), "observed": sum(1 for d in devs if not d["exact"])},
        }
    return {"crossings": res, "config": cfg, "timing": {"learned": learned_timing(cfg), "effective": effective_config()["timing"]}}


@app.get("/api/admin/visits")
def admin_visits(request: Request, days: int = 30):
    need_admin(request)
    days = max(1, min(days, 365))
    purge_visitor_codes()
    today = datetime.now(TZ).date()
    day_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    first = day_list[0]
    rows = db("SELECT day, kind, COUNT(*) AS n, COUNT(DISTINCT vhash) AS u, SUM(mobile) AS m FROM visits WHERE day >= ? GROUP BY day, kind", (first,))
    stored = {r["k"][5:]: int(r["v"]) for r in db("SELECT k, v FROM kv WHERE k >= ? AND k LIKE 'uniq:%'", ("uniq:" + first,))}
    per = {d: {"day": d, "views": 0, "uniques": 0, "previews": 0, "reports": 0} for d in day_list}
    mobile = views_total = 0
    for r in rows:
        p = per.get(r["day"])
        if not p:
            continue
        if r["kind"] == "view":
            p["views"] = r["n"]
            p["uniques"] = r["u"] or stored.get(r["day"], 0)
            mobile += r["m"] or 0
            views_total += r["n"]
        elif r["kind"] == "preview":
            p["previews"] = r["n"]
    for r in db("SELECT ts FROM reports WHERE ts > ?", (time.time() - (days + 1) * 86400,)):
        d = datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d")
        if d in per:
            per[d]["reports"] += 1
    hours = [0] * 24
    for r in db("SELECT hour, COUNT(*) AS n FROM visits WHERE kind='view' AND day >= ? GROUP BY hour", (first,)):
        hours[r["hour"]] = r["n"]
    refs = [{"ref": r["ref"], "n": r["n"]} for r in
            db("SELECT ref, COUNT(*) AS n FROM visits WHERE kind='view' AND ref IS NOT NULL AND day >= ? GROUP BY ref ORDER BY n DESC LIMIT 8", (first,))]
    peaks = {r["k"][5:]: r["v"] for r in db("SELECT k, v FROM kv WHERE k >= ? AND k LIKE 'peak:%' AND k != 'peak:all'", ("peak:" + first,))}
    for d in day_list:
        per[d]["peak"] = int(peaks[d].split("|")[0]) if d in peaks else 0

    def peak(key):
        row = db("SELECT v FROM kv WHERE k=?", ("peak:" + key,), one=True)
        if not row:
            return None
        n, ts = row["v"].split("|")
        return {"n": int(n), "ts": float(ts)}
    series = [per[d] for d in day_list]

    def total(n, key):
        return sum(s[key] for s in series[-n:])
    return {
        "active": sum(1 for v in _active.values() if v > time.time() - 60),
        "peakAll": peak("all"), "peakToday": peak(day_list[-1]),
        "today": series[-1],
        "yesterday": series[-2] if len(series) > 1 else None,
        "week": {"views": total(7, "views"), "uniques": total(7, "uniques"), "previews": total(7, "previews")},
        "month": {"views": total(30, "views"), "uniques": total(30, "uniques"), "previews": total(30, "previews")},
        "mobileShare": (mobile / views_total) if views_total else None,
        "days": series, "hours": hours, "refs": refs,
    }


@app.put("/api/admin/config")
async def admin_config(request: Request):
    need_admin(request)
    body = await request.json()
    cfg = get_config()
    for c in CROSSINGS:
        for k, v in body.get("crossings", {}).get(c, {}).items():
            if k in PARAM_KEYS and isinstance(v, (int, float)) and 0 <= v <= 900:
                cfg["crossings"][c][k] = int(v)
    vis = body.get("visitors", {})
    for k, lo, hi in (("confirm", 1, 10), ("window", 10, 600), ("rate", 0, 3600)):
        if isinstance(vis.get(k), (int, float)) and lo <= vis[k] <= hi:
            cfg["visitors"][k] = int(vis[k])
    if isinstance(vis.get("live"), bool):
        cfg["visitors"]["live"] = vis["live"]
    lb = body.get("learn", {})
    if isinstance(lb.get("auto"), bool):
        cfg["learn"]["auto"] = lb["auto"]
    for k, lo, hi in (("halflife", 3, 365), ("prior", 1, 50)):
        if isinstance(lb.get(k), (int, float)) and lo <= lb[k] <= hi:
            cfg["learn"][k] = int(lb[k])
    if isinstance(lb.get("visitors"), bool):
        cfg["learn"]["visitors"] = lb["visitors"]
    if isinstance(lb.get("visitorWeight"), (int, float)) and 0 <= lb["visitorWeight"] <= 1:
        cfg["learn"]["visitorWeight"] = float(lb["visitorWeight"])
    for k, v in body.get("timing", {}).items():
        if k in TIMING_KEYS and isinstance(v, (int, float)) and -600 <= v <= 600:
            cfg["timing"][k] = int(v)
    save_config(cfg)
    learn_invalidate()
    return cfg
