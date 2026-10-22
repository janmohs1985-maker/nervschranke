"""Labor: kleines Lernmodell für die Schranken – nur zum Ausprobieren im Admin-Bereich, die Seite nutzt es NICHT.

Zwei Teile:
1. Schließzeiten: aus den genauen Messungen (Jan voll, bestätigte Besucher schwächer) lernt eine Ridge-Regression,
   wie viele Sekunden die Schranke vor bzw. nach der Durchfahrt zu ist – abhängig von Übergang, Fall
   (durch/ab/an), Fern-/Regionalzug, Richtung, Tageszeit, Wochenende und Verspätung. Geprüft wird mit
   „eins weglassen“ (jede Messung wird vorhergesagt, ohne dass das Modell sie kannte) gegen die heutige Regel.
2. Güterzüge: wann sie laut Meldungen kommen (je Stunde und Wochentag/Wochenende, geglättet).

Bewusst ohne numpy & Co.: die Datenmengen sind klein, ein paar Zeilen lineare Algebra reichen.
"""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
CROSSINGS = ("elchinger", "wald", "fichten")
CASES = ("durch", "ab", "an")
LAMBDA = 1.5          # Ridge: je mehr, desto vorsichtiger (wenige Daten → stark Richtung Mittelwert)
PRIOR = 4.0           # wie die heutige Regel: der manuelle Wert zählt wie 4 Messungen


def features(crossing, case, long, westbound, ts, delay):
    """Merkmale einer Durchfahrt (alle als Zahlen, 1 = trifft zu)."""
    t = datetime.fromtimestamp(ts, TZ)
    h = t.hour + t.minute / 60
    return [
        1.0,                                          # Grundwert
        1.0 if crossing == "wald" else 0.0,
        1.0 if crossing == "fichten" else 0.0,
        1.0 if case == "ab" else 0.0,
        1.0 if case == "an" else 0.0,
        1.0 if long else 0.0,
        1.0 if westbound else 0.0,
        math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
        1.0 if t.weekday() >= 5 else 0.0,
        max(-5.0, min(15.0, float(delay or 0))) / 5,  # Verspätung in 5-Min.-Schritten, begrenzt
    ]


FEATURE_NAMES = ["Grundwert", "Waldstraße", "Fichtenstraße", "fährt in Nersingen ab", "hält danach in Nersingen",
                 "Fernzug", "Richtung Ulm", "Tageszeit (sin)", "Tageszeit (cos)", "Wochenende", "Verspätung je 5 Min."]


def _solve(A, b):
    """Gauß-Elimination für das kleine Gleichungssystem A x = b."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                M[r] = [a - f * bb for a, bb in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge(X, y, w, lam=LAMBDA):
    """gewichtete Ridge-Regression; der Grundwert (Spalte 0) wird nicht bestraft."""
    k = len(X[0])
    A = [[sum(w[i] * X[i][a] * X[i][b] for i in range(len(X))) + (lam if a == b and a > 0 else 0) for b in range(k)] for a in range(k)]
    v = [sum(w[i] * X[i][a] * y[i] for i in range(len(X))) for a in range(k)]
    return _solve(A, v)


def predict(beta, x):
    return sum(b * xi for b, xi in zip(beta, x))


def closure_model(samples):
    """samples: [{"kind": "before"/"after", "crossing", "param", "case", "long", "westbound", "ts", "delay",
    "y" (gemessen), "w" (Gewicht), "rule" (Wert der heutigen Regel ohne diese Messung = Schätzung des Grundwerts)}]
    → je Art Koeffizienten + Fehlervergleich (eins weglassen) Modell vs. Regel."""
    out = {}
    for kind in ("before", "after"):
        S = [s for s in samples if s["kind"] == kind]
        res = {"n": len(S)}
        if len(S) >= 6:
            X = [features(s["crossing"], s["case"], s["long"], s["westbound"], s["ts"], s["delay"]) for s in S]
            y = [s["y"] for s in S]
            w = [s["w"] for s in S]
            beta = ridge(X, y, w)
            err_m, err_r, rows = [], [], []
            for i in range(len(S)):
                b_i = ridge(X[:i] + X[i + 1:], y[:i] + y[i + 1:], w[:i] + w[i + 1:])
                pm = predict(b_i, X[i])
                # heutige Regel ohne diese Messung: Mittel aus manuellem Wert (wie 4 Messungen) und den anderen derselben Gruppe
                others = [S[j]["y"] for j in range(len(S)) if j != i and S[j]["param"] == S[i]["param"] and S[j]["crossing"] == S[i]["crossing"]]
                pr = (sum(others) + PRIOR * S[i]["manual"]) / (len(others) + PRIOR)
                err_m.append(abs(pm - y[i])); err_r.append(abs(pr - y[i]))
                rows.append({"ts": S[i]["ts"], "crossing": S[i]["crossing"], "param": S[i]["param"], "gemessen": round(y[i]),
                             "modell": round(pm), "regel": round(pr), "besucher": S[i]["w"] < 1})
            res.update({"beta": [round(b, 1) for b in beta], "names": FEATURE_NAMES,
                        "mae_model": round(sum(err_m) / len(err_m), 1), "mae_rule": round(sum(err_r) / len(err_r), 1),
                        "rows": rows[-40:]})
        out[kind] = res
    return out


def freight(events, first_ts, now_ts):
    """events: Zeitpunkte gemeldeter Güterzüge (je Übergang zusammengefasst: innerhalb 3 Min. = derselbe Zug).
    → je Stunde (0–23), Werktag/Wochenende: Güterzüge pro Tag (geglättet über die Nachbarstunden)."""
    ev = sorted(events)
    merged = [t for i, t in enumerate(ev) if i == 0 or t - ev[i - 1] > 180]
    days_wd = days_we = 0
    d = datetime.fromtimestamp(first_ts, TZ).date()
    end = datetime.fromtimestamp(now_ts, TZ).date()
    while d <= end:
        if d.weekday() >= 5:
            days_we += 1
        else:
            days_wd += 1
        d = d.fromordinal(d.toordinal() + 1)
    wd, we = [0] * 24, [0] * 24
    for t in merged:
        x = datetime.fromtimestamp(t, TZ)
        (we if x.weekday() >= 5 else wd)[x.hour] += 1
    smooth = lambda a: [round((a[h - 1] * .25 + a[h] * .5 + a[(h + 1) % 24] * .25), 2) for h in range(24)]
    return {"n": len(merged), "tage_werktag": days_wd, "tage_wochenende": days_we,
            "werktag": [round(v / max(1, days_wd), 2) for v in smooth(wd)],
            "wochenende": [round(v / max(1, days_we), 2) for v in smooth(we)],
            "roh_werktag": wd, "roh_wochenende": we}


def upcoming(passes, cfg_model, betas, now_ms):
    """Vergleich für die nächsten 60 Min.: Sperrbeginn/-ende laut Seite vs. laut Modell (nur Anzeige)."""
    out = []
    for c in CROSSINGS:
        for p in passes.get(c, []):
            if not now_ms <= p["pass"] <= now_ms + 3600_000:
                continue
            x = features(c, p["case"], p.get("isLong"), p["westbound"], p["pass"] / 1000, p.get("delay"))
            row = {"crossing": c, "label": p["label"], "westbound": p["westbound"], "case": p["case"], "pass": p["pass"],
                   "seiteZu": p["closeFrom"], "seiteAuf": p["closeTo"]}
            if betas.get("before"):
                row["modellZu"] = p["pass"] - max(20, predict(betas["before"], x)) * 1000
            if betas.get("after") and not (p["case"] == "an" and c != "elchinger"):
                row["modellAuf"] = p["pass"] + max(3, predict(betas["after"], x)) * 1000
            out.append(row)
    return sorted(out, key=lambda r: r["pass"])[:30]
