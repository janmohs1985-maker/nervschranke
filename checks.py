"""Plausibilitätsprüfung der Rechnung – läuft jede Minute auf dem Server (app.py, Hintergrund).

check(state, prev, now) bekommt das Ergebnis von model.compute (und das von vor einer Minute) und liefert
Auffälligkeiten: Dinge, die es an den drei Schranken so nicht geben kann. Sie landen in der Tabelle
`anomalies` und sind unter /api/diag abrufbar. Alle Zeiten in ms.

Lage (West → Ost): Elchinger – Bahnhof Nersingen – Wald – Fichten. Ein Zug Richtung Ulm (westbound)
kommt also erst an Fichten, dann Wald, dann Elchinger vorbei; Richtung Augsburg umgekehrt.
"""
ORDER = ("elchinger", "wald", "fichten")   # von West nach Ost
LOOK_BACK, LOOK_AHEAD = 10 * 60000, 60 * 60000


def merged(passes, gap=120000):
    """Sperrzeiten wie auf der Seite zusammengefasst (Lücke unter 2 Min. → bleibt zu)."""
    out = []
    for p in sorted(passes, key=lambda p: p["closeFrom"]):
        if out and p["closeFrom"] - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], p["closeTo"])
        else:
            out.append([p["closeFrom"], p["closeTo"]])
    return out


def _minus(a, b):
    """Zeiträume aus a ohne die aus b."""
    out = []
    for s, e in a:
        cur = [(s, e)]
        for bs, be in b:
            nxt = []
            for cs, ce in cur:
                if be <= cs or bs >= ce:
                    nxt.append((cs, ce))
                    continue
                if bs > cs:
                    nxt.append((cs, bs))
                if be < ce:
                    nxt.append((be, ce))
            cur = nxt
        out += cur
    return out


def _key(p):
    return p.get("num") or p["label"], p["westbound"]


def check(state, prev, now):
    P = state["passes"]
    found = []

    def add(kind, crossing, train, detail, t=None):
        found.append({"kind": kind, "crossing": crossing, "train": train, "detail": detail, "at": t})

    near = lambda p: now - LOOK_BACK <= p["pass"] <= now + LOOK_AHEAD

    # 1. Waldstraße allein zu, beide äußeren offen (länger als 30 s)
    outer = merged(P["elchinger"]) + merged(P["fichten"])
    for s, e in _minus(merged(P["wald"]), outer):
        if e - s > 30000 and e > now - LOOK_BACK and s < now + LOOK_AHEAD:
            add("wald_allein", "wald", None, f"Wald zu, Elchinger und Fichten offen ({round((e - s) / 1000)} s)", s)

    # 2. Durchfahrten je Zug über alle drei Übergänge
    trips = {}
    for cid in ORDER:
        for p in P[cid]:
            trips.setdefault(p["id"], {})[cid] = p
    for t in trips.values():
        any_p = next(iter(t.values()))
        if not near(any_p):
            continue
        label = any_p["label"]
        # Reihenfolge: Richtung Ulm Fichten → Wald → Elchinger, Richtung Augsburg umgekehrt
        seq = [t[c]["pass"] for c in (reversed(ORDER) if any_p["westbound"] else ORDER) if c in t]
        if any(b < a - 20000 for a, b in zip(seq, seq[1:])):
            add("reihenfolge", None, label, "Durchfahrten in falscher Reihenfolge: " +
                ", ".join(f"{c} {round((t[c]['pass'] - now) / 1000)} s" for c in ORDER if c in t), any_p["pass"])
        # Durchfahrender Zug muss über alle drei; Zug mit Halt mindestens über die beiden auf seiner Seite
        if all(p["case"] == "durch" for p in t.values()) and len(t) < 3:
            missing = [c for c in ORDER if c not in t]
            add("fehlt", ",".join(missing), label, "fährt durch, fehlt an " + ", ".join(missing), any_p["pass"])
        elif "elchinger" in t and "fichten" in t and "wald" not in t:
            add("fehlt", "wald", label, "an Elchinger und Fichten, fehlt an Wald", any_p["pass"])
        # einzelne Sperrung unplausibel lang oder verkehrt
        for c, p in t.items():
            if p["closeTo"] - p["closeFrom"] > 12 * 60000:
                add("zu_lang", c, label, f"eine Sperrung {round((p['closeTo'] - p['closeFrom']) / 60000)} Min.", p["closeFrom"])
            if p["closeTo"] < p["closeFrom"]:
                add("verkehrt", c, label, "Ende der Sperrung vor dem Anfang", p["closeFrom"])

    # 3. aussortierte „Geisterzüge“ (gleiche Nummer, Zeiten weit auseinander) – zur Kontrolle, dass kein echter
    #    Zug fehlt. Gleicher Zug aus zwei Quellen kurz hintereinander (DRF / WB 963) ist normal und zählt nicht.
    for d in state.get("dropped", []):
        if d["why"].startswith("doppelt") and now - LOOK_BACK <= d["pass"] <= now + LOOK_AHEAD:
            add("aussortiert", d["crossing"], d["label"], d["why"], d["pass"])

    # 4. Vergleich mit der Rechnung von vorhin: verschwundene Züge und große Sprünge
    if prev:
        for cid in ORDER:
            cur = {}
            for p in P[cid]:
                cur.setdefault(_key(p), []).append(p)
            for p in prev["passes"][cid]:
                # nur was bald kommt (nicht, was gerade aus dem Abfragefenster fällt)
                if not now + 60000 <= p["pass"] <= now + 30 * 60000:
                    continue
                same = [q for q in cur.get(_key(p), []) if abs(q["pass"] - p["pass"]) < 20 * 60000]
                if not same:
                    add("verschwunden", cid, p["label"], f"war für {round((p['pass'] - now) / 60000)} Min. angekündigt, "
                        f"ist jetzt weg (Quelle vorher: {p.get('src')})", p["pass"])
                    continue
                q = min(same, key=lambda q: abs(q["pass"] - p["pass"]))
                if abs(q["pass"] - p["pass"]) > 3 * 60000:
                    add("sprung", cid, p["label"], f"Durchfahrt um {round((q['pass'] - p['pass']) / 60000)} Min. verschoben "
                        f"({p.get('src')} → {q.get('src')})", q["pass"])
    return found
