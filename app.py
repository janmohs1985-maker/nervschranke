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
from pathlib import Path

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
                      "ab_before": 250, "ab_after": 55, "an_before": 120, "an_after": 45},
        "wald": {"durch_before": 120, "durch_before_long": 90, "durch_after": 30,
                 "ab_before": 250, "ab_after": 55, "an_before": 120, "an_after": 45},
        "fichten": {"durch_before": 80, "durch_before_long": 60, "durch_after": 20,
                    "ab_before": 250, "ab_after": 55, "an_before": 80, "an_after": 45},
    },
    "visitors": {"confirm": 2, "window": 90, "rate": 60, "live": True},
}
PARAM_KEYS = tuple(DEFAULT_CONFIG["crossings"]["elchinger"].keys())

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
""")
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
NOCACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html", headers=NOCACHE)


@app.get("/impressum")
def impressum():
    return FileResponse(ROOT / "impressum.html", headers=NOCACHE)


@app.get("/datenschutz")
def datenschutz():
    return FileResponse(ROOT / "datenschutz.html", headers=NOCACHE)


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


@app.get("/api/trips")
async def trips(request: Request):
    q = str(request.url.query)
    hit = _trips_cache.get(q)
    if hit and hit[0] > time.time():
        return Response(hit[1], media_type="application/json", headers={"X-Cache": "HIT"})
    async with _trips_lock:
        hit = _trips_cache.get(q)
        if hit and hit[0] > time.time():
            return Response(hit[1], media_type="application/json", headers={"X-Cache": "HIT"})
        try:
            r = await _http.get(TRANSITOUS + "?" + q)
            r.raise_for_status()
        except Exception:
            if hit:  # lieber alte Daten als keine
                return Response(hit[1], media_type="application/json", headers={"X-Cache": "STALE"})
            raise HTTPException(502, "Transitous nicht erreichbar")
        for k in [k for k, v in _trips_cache.items() if v[0] < time.time() - 300]:
            _trips_cache.pop(k, None)
        _trips_cache[q] = (time.time() + 30, r.content)
        return Response(r.content, media_type="application/json", headers={"X-Cache": "MISS"})

# ---------------------------------------------------------------- Modell + Meldungen (öffentlich)


@app.get("/api/config")
def config_public():
    return JSONResponse(get_config(), headers=NOCACHE)


def clean_snapshot(s):
    if not isinstance(s, dict):
        return None
    keep = {}
    for k in ("name", "kind", "case", "westbound", "long", "pass", "closeFrom", "closeTo", "delay"):
        if k in s:
            keep[k] = s[k]
    return json.dumps(keep)[:2000]


@app.post("/api/report")
async def report(request: Request):
    body = await request.json()
    crossing, state = body.get("crossing"), body.get("state")
    if crossing not in CROSSINGS or state not in ("zu", "auf"):
        raise HTTPException(400, "ungültig")
    device = str(body.get("device", ""))[:40]
    ip = iphash(request)
    cfg = get_config()["visitors"]
    # pro Gerät 1 Meldung je `rate` Sekunden; pro Anschluss (IP) höchstens 5, damit z. B. eine Familie
    # im selben WLAN sich gegenseitig bestätigen kann, aber niemand die Seite zuspammt
    since = time.time() - cfg["rate"]
    same_dev = device and db("SELECT 1 FROM reports WHERE source='visitor' AND ts > ? AND device=?", (since, device), one=True)
    same_ip = db("SELECT COUNT(*) AS n FROM reports WHERE source='visitor' AND ts > ? AND iphash=?", (since, ip), one=True)["n"]
    if same_dev or same_ip >= 5 or (not device and same_ip >= 1):
        raise HTTPException(429, "Du hast gerade schon gemeldet.")
    db("INSERT INTO reports(ts, crossing, state, source, device, iphash, snapshot) VALUES(?,?,?,?,?,?,?)",
       (time.time(), crossing, state, "visitor", device, ip, clean_snapshot(body.get("snapshot"))))
    # Besucher-Meldungen höchstens 12 Monate aufheben (so steht es in der Datenschutzerklärung)
    db("DELETE FROM reports WHERE source='visitor' AND ts < ?", (time.time() - 365 * 86400,))
    return live()


def confirmed_ids(rows, cfg):
    """Besucher-Meldungen zählen erst, wenn genug verschiedene Geräte dasselbe melden."""
    ok = set()
    vis = [r for r in rows if r["source"] == "visitor"]
    for r in vis:
        same = {x["device"] or x["iphash"] for x in vis if x["crossing"] == r["crossing"] and x["state"] == r["state"]
                and abs(x["ts"] - r["ts"]) <= cfg["window"]}
        if len(same) >= cfg["confirm"]:
            ok.add(r["id"])
    return ok


@app.get("/api/live")
def live():
    """Meldungen der letzten 3 Minuten je Übergang."""
    cfg = get_config()["visitors"]
    now = time.time()
    rows = db("SELECT * FROM reports WHERE ts > ? AND ignored=0 AND state IN ('zu','auf') ORDER BY ts", (now - 180,))
    conf = confirmed_ids(rows, cfg)
    out = {}
    for c in CROSSINGS:
        mine = [r for r in rows if r["crossing"] == c]
        win = [r for r in mine if r["ts"] > now - cfg["window"] and r["source"] == "visitor"]
        trusted = [r for r in mine if r["source"] == "admin" or r["id"] in conf]
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
    db("INSERT INTO reports(ts, crossing, state, source, device, iphash, snapshot) VALUES(?,?,?,?,?,?,?)",
       (time.time(), crossing, state, "admin", "jan", "", clean_snapshot(body.get("snapshot"))))
    row = db("SELECT last_insert_rowid() AS id", one=True)
    return {"ok": True, "id": row["id"]}


@app.delete("/api/admin/report/{rid}")
def admin_delete(rid: int, request: Request):
    need_admin(request)
    db("DELETE FROM reports WHERE id=?", (rid,))
    return {"ok": True}


@app.post("/api/admin/report/{rid}/ignore")
async def admin_ignore(rid: int, request: Request):
    need_admin(request)
    body = await request.json()
    db("UPDATE reports SET ignored=? WHERE id=?", (1 if body.get("ignored") else 0, rid))
    return {"ok": True}


def evaluate(r, cfg):
    """Abweichung einer Meldung zum Modell und welcher Wert damit gemessen wurde."""
    try:
        s = json.loads(r["snapshot"]) if r["snapshot"] else None
    except ValueError:
        s = None
    out = {"id": r["id"], "ts": r["ts"], "crossing": r["crossing"], "state": r["state"], "source": r["source"],
           "ignored": bool(r["ignored"]), "train": None, "dev": None, "param": None, "measured": None, "outlier": False}
    if not s or "pass" not in s:
        out["outlier"] = True
        out["why"] = "kein Zug in der Nähe"
        return out
    ps, cf, ct = s["pass"] / 1000, s.get("closeFrom", 0) / 1000, s.get("closeTo", 0) / 1000
    out["train"] = {"name": s.get("name"), "kind": s.get("kind"), "westbound": s.get("westbound"), "case": s.get("case"),
                    "pass": ps}
    case = s.get("case") or "durch"
    if r["state"] == "zu":
        out["dev"] = r["ts"] - cf
        out["measured"] = ps - r["ts"]
        out["param"] = case + "_before" + ("_long" if case == "durch" and s.get("long") else "")
    elif r["state"] == "auf":
        out["dev"] = r["ts"] - ct
        out["measured"] = r["ts"] - ps
        out["param"] = case + "_after"
    else:  # Zug fährt drüber: wie genau stimmt die Durchfahrtszeit?
        out["dev"] = r["ts"] - ps
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
    out = []
    for r in rows:
        e = evaluate(r, cfg)
        e["confirmed"] = r["source"] == "admin" or r["id"] in conf
        out.append(e)
    return {"reports": out}


@app.get("/api/admin/analysis")
def admin_analysis(request: Request, days: float = 60):
    need_admin(request)
    cfg = get_config()
    rows = db("SELECT * FROM reports WHERE ts > ? ORDER BY ts", (time.time() - days * 86400,))
    conf = confirmed_ids(rows, cfg["visitors"])
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
            e = evaluate(r, cfg)
            if not trusted or e["ignored"] or e["outlier"]:
                continue
            if e["dev"] is not None and r["state"] in ("zu", "auf"):
                devs.append({"ts": r["ts"], "dev": e["dev"], "source": r["source"]})
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
        recent = [d for d in devs if d["ts"] > time.time() - 7 * 86400]
        res[c] = {
            "params": params,
            "devs": devs[-200:],
            "hit": (sum(abs(d["dev"]) <= 60 for d in recent) / len(recent)) if recent else None,
            "meanDev": (sum(abs(d["dev"]) for d in recent) / len(recent)) if recent else None,
            "counts": {"admin": n_admin, "visitor": n_vis, "visitorConfirmed": n_vis_ok},
        }
    return {"crossings": res, "config": cfg}


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
    save_config(cfg)
    return cfg
