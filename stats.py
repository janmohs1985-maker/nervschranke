"""Fahrtenbuch und Statistik für /statistik.

Jede Minute schreibt der Server die gerade gerechneten Durchfahrten in die Tabelle `passlog` (eine Zeile je
Übergang und Zug). Solange eine Sperrung noch nicht vorbei ist, wird die Zeile mit dem neuesten Stand
überschrieben; danach bleibt sie, wie sie zuletzt gerechnet wurde. Fällt ein angekündigter Zug weg, wird seine
Zeile wieder gelöscht. Daraus entsteht die Statistik – nur Zugdaten, nichts über Besucher.
"""
import json
import time
import zlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
CROSSINGS = ("elchinger", "wald", "fichten")
KEEP_DAYS = 400
FINAL_AFTER = 120_000          # ms nach Ende der Sperrung: Zeile wird nicht mehr überschrieben
DAY_FROM, DAY_TO = 6, 22       # „tagsüber“ für Wahrscheinlichkeit und Wartezeit

SCHEMA = """
CREATE TABLE IF NOT EXISTS passlog (
  crossing TEXT NOT NULL, trip TEXT NOT NULL, day TEXT NOT NULL,
  label TEXT, kind TEXT, long INTEGER, westbound INTEGER, kase TEXT,
  pass REAL NOT NULL, closeFrom REAL NOT NULL, closeTo REAL NOT NULL, delay INTEGER, src TEXT,
  PRIMARY KEY (crossing, trip)
);
CREATE INDEX IF NOT EXISTS passlog_day ON passlog(day);
"""


def _kind(label):
    n = (label or "").split(" ")[0]
    return "ice" if n in ("ICE", "TGV", "RJX") else "nj" if n in ("NJ", "EN") else "ic" if n in ("IC", "EC", "RJ", "FLX") \
        else "re" if n in ("RE", "IRE", "MEX") else "rb"


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d")


def log_passes(db, passes, now_ms):
    """Stand einer Rechnung ins Fahrtenbuch übernehmen (passes = out["passes"])."""
    for c, lst in passes.items():
        seen = []
        for p in lst:
            kind = p.get("kind") or _kind(p.get("label"))
            long = p.get("isLong") if "isLong" in p else kind in ("ice", "ic", "nj")
            db("""INSERT INTO passlog(crossing, trip, day, label, kind, long, westbound, kase, pass, closeFrom, closeTo, delay, src)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(crossing, trip) DO UPDATE SET day=excluded.day, label=excluded.label, kind=excluded.kind,
                    long=excluded.long, westbound=excluded.westbound, kase=excluded.kase, pass=excluded.pass,
                    closeFrom=excluded.closeFrom, closeTo=excluded.closeTo, delay=excluded.delay, src=excluded.src
                  WHERE passlog.closeTo > ?""",
               (c, p["id"], _day(p["pass"]), p.get("label"), kind, 1 if long else 0, 1 if p.get("westbound") else 0,
                p.get("case"), p["pass"], p["closeFrom"], p["closeTo"], p.get("delay"), p.get("src"), now_ms - FINAL_AFTER))
            seen.append(p["id"])
        # angekündigt, aber nicht mehr in der Rechnung (Ausfall, Doppel) → wieder raus
        marks = ",".join("?" * len(seen)) or "''"
        db(f"DELETE FROM passlog WHERE crossing=? AND closeFrom > ? AND pass < ? AND trip NOT IN ({marks})",
           (c, now_ms, now_ms + 3600_000, *seen))


def purge(db):
    db("DELETE FROM passlog WHERE pass < ?", ((time.time() - KEEP_DAYS * 86400) * 1000,))


def backfill_from_history(db, unpack):
    """Einmalig: die gespeicherten Minuten-Stände (history, 3 Tage) nachträglich ins Fahrtenbuch."""
    if db("SELECT 1 FROM passlog LIMIT 1", one=True):
        return 0
    rows = db("SELECT ts, data FROM history ORDER BY ts")
    for r in rows:
        try:
            st = unpack(r["data"])
        except (zlib.error, ValueError):
            continue
        log_passes(db, st.get("passes", {}), r["ts"] * 1000)
    return len(rows)


# ---------------------------------------------------------------- Auswertung

def _merge(iv, gap_ms):
    """Sperrzeiten zusammenfassen (kürzere Lücken als gap = bleibt zu). iv: [(von, bis, zug)] → [(von, bis, züge)]"""
    out = []
    for a, b, t in sorted(iv):
        if out and a - out[-1][1] < gap_ms:
            out[-1][1] = max(out[-1][1], b)
            out[-1][2].add(t)
        else:
            out.append([a, b, {t}])
    return out


def _clip(iv, lo, hi):
    return sum(max(0, min(b, hi) - max(a, lo)) for a, b, *_ in iv)


def _intersect(a, b):
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _hm(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%H:%M")


def _day_start(d):
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ).timestamp() * 1000


def compute(db, model_cfg, now_ms=None):
    now_ms = now_ms or time.time() * 1000
    rows = db("SELECT * FROM passlog WHERE closeFrom < ? ORDER BY closeFrom", (now_ms,))
    if not rows:
        return {"ok": True, "empty": True}
    start_ms = rows[0]["closeFrom"]
    gap = {c: (model_cfg.get(c, {}).get("stay_closed") or 60) * 1000 for c in CROSSINGS}
    # nur was bis jetzt passiert ist (Sperrungen, die gerade laufen, bis jetzt)
    by_c = {c: _merge([(r["closeFrom"], min(r["closeTo"], now_ms), r["trip"]) for r in rows if r["crossing"] == c], gap[c])
            for c in CROSSINGS}
    trains = {}
    for r in rows:
        if r["pass"] <= now_ms and r["trip"] not in trains:
            trains[r["trip"]] = r
    today = _day(now_ms)
    t0 = _day_start(today)
    first_day = _day(start_ms)

    def day_list(n):
        return [(datetime.fromtimestamp(now_ms / 1000, TZ) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)][::-1]

    def train_stats(trs):
        known = [t for t in trs if t["delay"] is not None and t["src"] in ("db", "rt")]
        late = max(known, key=lambda t: t["delay"], default=None)
        return {"total": len(trs),
                "byKind": {"fern": sum(1 for t in trs if t["kind"] in ("ice", "ic", "nj")),
                           "re": sum(1 for t in trs if t["kind"] == "re"), "rb": sum(1 for t in trs if t["kind"] == "rb")},
                "toUlm": sum(1 for t in trs if t["westbound"]), "toAug": sum(1 for t in trs if not t["westbound"]),
                "punctual": round(100 * sum(1 for t in known if t["delay"] <= 5) / len(known)) if known else None,
                "avgDelay": round(sum(max(0, t["delay"]) for t in known) / len(known), 1) if known else None,
                "maxDelay": {"label": late["label"], "min": late["delay"]} if late and late["delay"] > 0 else None}

    # --- heute je Übergang
    day_ms = {}
    cross_today = {}
    for c in CROSSINGS:
        iv = [x for x in by_c[c] if x[1] > t0]
        closed = _clip(iv, t0, now_ms)
        lo, hi = t0 + DAY_FROM * 3600_000, min(now_ms, t0 + DAY_TO * 3600_000)
        day_iv = [(max(a, lo), min(b, hi)) for a, b, _ in iv if b > lo and a < hi]
        dsum = sum(b - a for a, b in day_iv)
        longest = max(iv, key=lambda x: x[1] - x[0], default=None)
        cross_today[c] = {
            "closedMin": round(closed / 60000), "closures": len(iv),
            "trains": len({r["trip"] for r in rows if r["crossing"] == c and r["day"] == today and r["pass"] <= now_ms}),
            "longest": {"min": round((longest[1] - longest[0]) / 60000, 1), "at": _hm(longest[0]), "trains": len(longest[2])} if longest else None,
            # Wer tagsüber zufällig kommt: wie wahrscheinlich ist sie zu – und wie lange wartet man dann im Schnitt?
            "chance": round(100 * dsum / (hi - lo)) if hi > lo else None,
            "wait": round(sum((b - a) ** 2 for a, b in day_iv) / (2 * dsum) / 1000) if dsum else None,
        }
    all3 = _intersect(_intersect([(a, b) for a, b, _ in by_c["elchinger"]], [(a, b) for a, b, _ in by_c["wald"]]),
                      [(a, b) for a, b, _ in by_c["fichten"]])
    today_trains = [t for t in trains.values() if t["day"] == today]

    # --- letzte 7 Tage
    week = []
    for d in day_list(7):
        if d < first_day:
            continue
        a, b = _day_start(d), min(now_ms, _day_start(d) + 86400_000)
        week.append({"day": d, "partial": d == first_day and start_ms > a + 3600_000 or d == today,
                     "closedMin": {c: round(_clip(by_c[c], a, b) / 60000) for c in CROSSINGS},
                     "trains": sum(1 for t in trains.values() if t["day"] == d)})

    # --- Tagesverlauf: Minuten zu je Stunde im Schnitt (nur Stunden, für die es Daten gibt) + nervigste halbe Stunde
    hourly, worst = {}, {}
    for c in CROSSINGS:
        bins, cover = [0.0] * 144, [0] * 144
        d = _day(start_ms)
        while d <= today:
            ds = _day_start(d)
            for k in range(144):
                a, b = ds + k * 600_000, ds + (k + 1) * 600_000
                if a >= start_ms and b <= now_ms:
                    cover[k] += 1
                    bins[k] += _clip(by_c[c], a, b)
            d = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        avg = [bins[k] / cover[k] / 60000 if cover[k] else None for k in range(144)]
        hourly[c] = [round(sum(avg[h * 6 + i] or 0 for i in range(6)), 1) if any(avg[h * 6 + i] is not None for i in range(6)) else None
                     for h in range(24)]
        best, at = -1, None
        for k in range(5 * 6, 23 * 6 - 2):
            if None in avg[k:k + 3]:
                continue
            s = sum(avg[k:k + 3])
            if s > best:
                best, at = s, k
        if at is not None:
            worst[c] = {"from": f"{at // 6:02d}:{at % 6 * 10:02d}", "to": f"{(at + 3) // 6:02d}:{(at + 3) % 6 * 10:02d}",
                        "min": round(best, 1)}

    # --- Rekorde (alles, was im Fahrtenbuch steht)
    longest = max(((c, x) for c in CROSSINGS for x in by_c[c]), key=lambda cx: cx[1][1] - cx[1][0], default=None)
    per_day = {}
    for t in trains.values():
        per_day[t["day"]] = per_day.get(t["day"], 0) + 1
    full_days = {d: n for d, n in per_day.items() if d != today and d != first_day} or per_day
    most_day = max(full_days.items(), key=lambda x: x[1], default=None)
    closed_days = [(d, c, _clip(by_c[c], _day_start(d), _day_start(d) + 86400_000)) for d in full_days for c in CROSSINGS]
    most_closed = max(closed_days, key=lambda x: x[2], default=None)
    late = max((t for t in trains.values() if t["delay"] is not None and t["src"] in ("db", "rt")), key=lambda t: t["delay"], default=None)
    records = {
        "longest": {"crossing": longest[0], "min": round((longest[1][1] - longest[1][0]) / 60000, 1), "day": _day(longest[1][0]),
                    "at": _hm(longest[1][0]), "trains": len(longest[1][2])} if longest else None,
        "mostTrainsDay": {"day": most_day[0], "n": most_day[1]} if most_day else None,
        "mostClosedDay": {"day": most_closed[0], "crossing": most_closed[1], "min": round(most_closed[2] / 60000)} if most_closed else None,
        "maxDelay": {"label": late["label"], "min": late["delay"], "day": late["day"]} if late and late["delay"] > 0 else None,
    }

    return {
        "ok": True, "generated": int(now_ms), "since": first_day, "today": today,
        "crossings": cross_today, "allThreeMin": round(_clip(all3, t0, now_ms) / 60000),
        "trainsToday": train_stats(today_trains), "week": week, "hourly": hourly, "worst": worst, "records": records,
        "total": {"trains": len(trains), "days": len(per_day),
                  "closedHours": {c: round(_clip(by_c[c], start_ms, now_ms) / 3600_000, 1) for c in CROSSINGS}},
    }
