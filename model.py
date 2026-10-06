"""Schranken-Rechnung auf dem Server – gleiche Logik wie bisher in core.js (Webseite).

compute(raw, rt, model) bekommt die Transitous-Abschnitte, die DB-Echtzeit und die Schätzwerte und liefert
je Übergang die Durchfahrten mit Schließzeiten sowie die Zugabschnitte für Gleisband und Karte.
Alle Zeiten in Millisekunden seit 1970 (wie Date in JavaScript).
"""
import math
import re
import secrets
from datetime import datetime

CROSSINGS = [
    {"id": "elchinger", "lat": 48.42915, "lon": 10.11561},
    {"id": "wald", "lat": 48.43161, "lon": 10.12236},
    {"id": "fichten", "lat": 48.43225, "lon": 10.12885},
]
PASS_DISTANCE, MAP_DISTANCE = 60, 400
RAIL_MODES = {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL", "REGIONAL_FAST_RAIL", "REGIONAL_RAIL", "RAIL", "SUBURBAN"}
LONG_MODES = {"HIGHSPEED_RAIL", "LONG_DISTANCE", "NIGHT_RAIL"}
STATION = "Nersingen"
LONG_NAMES = ("ICE", "IC", "EC", "EN", "NJ", "RJ", "RJX", "TGV", "FLX")
LAT0, LON0 = 48.43, 10.12
KX, KY = math.cos(LAT0 * math.pi / 180) * 111320, 110540


def ms(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def decode_polyline(s):
    i, lat, lon, out, n = 0, 0, 0, [], len(s)

    def nxt():
        nonlocal i
        r = sh = 0
        while True:
            b = ord(s[i]) - 63
            i += 1
            r |= (b & 31) << sh
            sh += 5
            if b < 32 or i >= n:
                break
        return ~(r >> 1) if r & 1 else r >> 1
    while i < n:
        lat += nxt()
        if i >= n:
            break
        lon += nxt()
        out.append((lat / 1e5, lon / 1e5))
    return out


def train_name(raw, mode):
    s = re.sub(r"\s*\(.*\)", "", raw or "", count=1).strip()
    first = s.split(" ")[0] if s else ""
    if first in LONG_NAMES:
        return first
    m = re.match(r"^([A-Za-z]+)(\d+)$", s)
    if m:
        s = f"{m.group(1)} {m.group(2)}"
    if not s:
        return "Fernzug" if mode in LONG_MODES else "Zug"
    return s


def kind_of(name, mode):
    n = name.split(" ")[0]
    if n in ("ICE", "TGV", "RJX"):
        return "ice"
    if n in ("NJ", "EN") or mode == "NIGHT_RAIL":
        return "nj"
    if n in ("IC", "EC", "RJ", "FLX") or mode in LONG_MODES:
        return "ic"
    if n in ("RE", "IRE", "MEX"):
        return "re"
    return "rb"


def train_number(raw):
    raw = raw or ""
    m = re.search(r"\((\d+)\)", raw) or re.match(r"^[A-Za-z]+\s*(\d{2,6})$", raw)
    return m.group(1) if m else None


def rt_shift(s, rt):
    """Um wie viele ms ist der Abschnitt laut DB später als laut Transitous – fällt er aus – welches Gleis?"""
    none = {"shift": 0, "cancel": False, "db": False, "pl": None}
    trip = (s.get("trips") or [{}])[0]
    num = train_number(trip.get("routeShortName"))
    if not rt or not rt.get("ok") or not num:
        return none
    frm, to = (s.get("from") or {}).get("name", ""), (s.get("to") or {}).get("name", "")
    e, dep = None, True
    if "Nersingen" in frm:
        e = (rt.get("nersingen", {}).get(num) or {}).get("dp")
    elif "Nersingen" in to:
        e, dep = (rt.get("nersingen", {}).get(num) or {}).get("ar"), False
    elif re.match(r"^Ulm Hauptbahnhof|^Ulm Hbf", frm):
        e = (rt.get("ulm", {}).get(num) or {}).get("dp")
    elif re.match(r"^Ulm Hauptbahnhof|^Ulm Hbf", to):
        e, dep = (rt.get("ulm", {}).get(num) or {}).get("ar"), False
    if not e:
        return none
    sched = ms(s.get("scheduledDeparture") or s.get("departure")) if dep else ms(s.get("scheduledArrival") or s.get("arrival"))
    if sched is None or not abs(e["pt"] - sched) < 120000:
        return none
    if e.get("cancel"):
        return {"shift": 0, "cancel": True, "db": True, "pl": None}
    t_delay = (ms(s.get("departure")) if dep else ms(s.get("arrival"))) - sched
    shift = (e["ct"] - e["pt"]) - t_delay
    at_here = "Nersingen" in frm or "Nersingen" in to
    return {"shift": shift if abs(shift) >= 30000 else 0, "cancel": False, "db": True, "pl": (e.get("pl") or None) if at_here else None}


def profile_t1(L, D, a=0.6):
    q = D * D - 2 * L / a
    return D - math.sqrt(q) if q > 0 else None


def profile_time(s, L, D, mode):
    """Sekunden bis zur Strecke s – mit Anfahren ("ab") bzw. Bremsen ("an") in Nersingen."""
    t1, a = profile_t1(L, D), 0.6
    if not mode or t1 is None:
        return D * s / L

    def inv(x):
        return math.sqrt(2 * x / a) if x <= 0.5 * a * t1 * t1 else t1 + (x - 0.5 * a * t1 * t1) / (a * t1)
    return inv(s) if mode == "ab" else D - inv(L - s)


def compute(raw, rt, model):
    P = {c["id"]: [] for c in CROSSINGS}
    seen = {c["id"]: set() for c in CROSSINGS}
    S, seen_seg, st_lons, station_dep = [], set(), [], {}
    rail = [s for s in raw if s.get("mode") in RAIL_MODES]
    for s in rail:
        s["_rt"] = rt_shift(s, rt)
    trip_id = lambda s: ((s.get("trips") or [{}])[0]).get("tripId")
    # Abfahrt in Nersingen nie vor der Ankunft desselben Zugs (+30 s Halt)
    arr_here = {}
    for s in rail:
        if STATION in (s.get("to") or {}).get("name", "") and ms(s.get("arrival")) is not None:
            arr_here[trip_id(s)] = ms(s["arrival"]) + s["_rt"]["shift"]
    for s in rail:
        if STATION not in (s.get("from") or {}).get("name", "") or ms(s.get("departure")) is None:
            continue
        a, d = arr_here.get(trip_id(s)), ms(s["departure"]) + s["_rt"]["shift"]
        if a and d < a + 30000:
            s["_rt"]["shift"] += a + 30000 - d
    for s in rail:
        if STATION in (s.get("from") or {}).get("name", "") and not s["_rt"]["cancel"] and ms(s.get("departure")) is not None:
            t, d = trip_id(s), ms(s["departure"]) + s["_rt"]["shift"]
            if t and (t not in station_dep or d < station_dep[t]):
                station_dep[t] = d

    for s in rail:
        r = s["_rt"]
        if r["cancel"]:
            continue
        frm, to = s.get("from") or {}, s.get("to") or {}
        if STATION in frm.get("name", ""):
            st_lons.append(frm.get("lon"))
        if STATION in to.get("name", ""):
            st_lons.append(to.get("lon"))
        dep0, arr0 = ms(s.get("departure")), ms(s.get("arrival"))
        if dep0 is None or arr0 is None:
            continue
        dep, arr = dep0 + r["shift"], arr0 + r["shift"]
        coords = decode_polyline(s.get("polyline") or "")
        if len(coords) < 2:
            continue
        rsn = ((s.get("trips") or [{}])[0]).get("routeShortName")
        name = train_name(rsn, s["mode"])
        num = train_number(rsn)
        label = f"{name.split(' ')[0]} {num}" if num else name
        kind = kind_of(name, s["mode"])
        is_long = s["mode"] in LONG_MODES
        trip = trip_id(s) or secrets.token_hex(6)
        westbound = to.get("lon", 0) < frm.get("lon", 0)
        from_here, to_here = STATION in frm.get("name", ""), STATION in to.get("name", "")
        stops = from_here or to_here
        delay = None
        if (s.get("realTime") or r["db"]) and s.get("scheduledDeparture"):
            delay = round((dep - ms(s["scheduledDeparture"])) / 60000)

        pts = [((lo - LON0) * KX, (la - LAT0) * KY) for la, lo in coords]
        cum = [0.0]
        for k in range(len(pts) - 1):
            cum.append(cum[k] + math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]))
        total = cum[-1]
        if not total > 0:
            continue
        near_any = math.inf
        for c in CROSSINGS:
            cx, cy = (c["lon"] - LON0) * KX, (c["lat"] - LAT0) * KY
            best, along = math.inf, 0.0
            for k in range(len(pts) - 1):
                ax, ay = pts[k][0] - cx, pts[k][1] - cy
                dx, dy = pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]
                l2 = dx * dx + dy * dy
                t = min(1, max(0, (-ax * dx - ay * dy) / l2)) if l2 > 0 else 0
                d = math.hypot(ax + t * dx, ay + t * dy)
                if d < best:
                    best, along = d, cum[k] + t * math.sqrt(l2)
            near_any = min(near_any, best)
            if best > PASS_DISTANCE or trip in seen[c["id"]]:
                continue
            seen[c["id"]].add(trip)
            mode = "ab" if from_here else "an" if to_here else None
            pas = dep + profile_time(along, total, (arr - dep) / 1000, mode) * 1000
            if any((p["name"] == name and abs(p["pass"] - pas) < 120000) or
                   (p["westbound"] == westbound and abs(p["pass"] - pas) < 60000) for p in P[c["id"]]):
                continue
            m = model[c["id"]]
            kase = "ab" if from_here else "an" if to_here else "durch"
            before = (m["durch_before_long"] if is_long else m["durch_before"]) if kase == "durch" else m[kase + "_before"]
            after = m[kase + "_after"]
            src = "db" if r["db"] else "rt" if s.get("realTime") else "plan"
            P[c["id"]].append({"id": trip, "name": name, "num": num, "label": label, "kind": kind, "isLong": is_long,
                               "westbound": westbound, "stops": stops, "fromHere": from_here, "case": kase,
                               "pass": int(pas), "delay": delay, "src": src,
                               "closeFrom": int(pas - before * 1000), "closeTo": int(pas + after * 1000)})
        if near_any <= MAP_DISTANCE:
            key = f"{name}|{dep // 60000}|{westbound}"
            plat = r["pl"] or (("3" if stops and not is_long else "2") if westbound else "1")
            mode = "ab" if from_here else "an" if to_here else None
            twin = any(x["westbound"] == westbound and ((abs(x["dep"] - dep) < 60000 and abs(x["arr"] - arr) < 60000) or
                                                     (num and x["num"] == num and x["dep"] < arr and dep < x["arr"])) for x in S)
            if not twin and key not in seen_seg:
                seen_seg.add(key)
                S.append({"id": key, "trip": trip, "name": name, "num": num, "label": label, "kind": kind,
                          "westbound": westbound, "dep": dep, "arr": arr, "poly": s["polyline"], "plat": plat, "mode": mode})

    # hält der Zug nach dem Übergang in Nersingen: Schranke bleibt bis zu seiner Abfahrt zu
    for cid, lst in P.items():
        for p in lst:
            if p["case"] != "an" or p["id"] not in station_dep:
                continue
            p["stationDep"] = station_dep[p["id"]]
            t = p["stationDep"] + model[cid]["an_after_dep"] * 1000
            if t > p["closeTo"]:
                p["closeTo"] = int(t)
    # hinter dem Bahnhof frühestens zu, wenn derselbe Zug an der Schranke davor angekündigt wird
    by_trip = {}
    for lst in P.values():
        for p in lst:
            by_trip.setdefault(p["id"], []).append(p)
    for lst in by_trip.values():
        lst.sort(key=lambda p: p["pass"])
        for i in range(1, len(lst)):
            if lst[i]["fromHere"]:
                earliest = min(p["closeFrom"] for p in lst[:i])
                if lst[i]["closeFrom"] < earliest:
                    lst[i]["closeFrom"] = earliest
    for lst in P.values():
        lst.sort(key=lambda p: p["pass"])
    st_lons = [x for x in st_lons if x is not None]
    return {"passes": P, "segments": S, "stationLon": (sum(st_lons) / len(st_lons)) if st_lons else None}
