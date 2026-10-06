// Rechnung wie in der Familie-App (RailCrossing.swift), erweitert auf drei Übergänge
const CROSSINGS = [
  {id: "elchinger", name: "Elchinger Straße", short: "Elchinger", at: "an der Elchinger Straße", lat: 48.42915, lon: 10.11561, half: false},
  {id: "wald",      name: "Waldstraße",       short: "Wald",      at: "an der Waldstraße",       lat: 48.43161, lon: 10.12236, half: false},
  {id: "fichten",   name: "Fichtenstraße",    short: "Fichten",   at: "an der Fichtenstraße",    lat: 48.43225, lon: 10.12885, half: true},
];
const CFG = {
  minLat: 48.405, minLon: 10.06, maxLat: 48.45, maxLon: 10.17,
  passDistance: 60, mapDistance: 400,
  railModes: new Set(["HIGHSPEED_RAIL","LONG_DISTANCE","NIGHT_RAIL","REGIONAL_FAST_RAIL","REGIONAL_RAIL","RAIL","SUBURBAN"]),
  longModes: new Set(["HIGHSPEED_RAIL","LONG_DISTANCE","NIGHT_RAIL"]),
  station: "Nersingen",
};
// Zugdaten nur über den eigenen Server (Zwischenspeicher, keine Besucherdaten an Transitous).
// Direkt bei Transitous nur beim Ausprobieren als lokale Datei.
const SOURCES = location.protocol === "file:" ? ["https://api.transitous.org/api/v1/map/trips"] : ["/api/trips"];

let passes = {}, segments = [], loaded = null, failed = false;
let stationLon = 10.1196;

// ?plus=5 zeigt den Stand in 5 Minuten (zum Ausprobieren)
const OFFSET = (+new URLSearchParams(location.search).get("plus") || 0) * 60000;
const nowD = () => new Date(Date.now() + OFFSET);
const $ = id => document.getElementById(id);
const hm = d => d.toLocaleTimeString("de-DE", {hour: "2-digit", minute: "2-digit", timeZone: "Europe/Berlin"});
const mins = ms => Math.max(1, Math.ceil(ms / 60000));
const mmss = ms => { const s = Math.max(0, Math.round(ms / 1000)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

function decodePolyline(s) {
  let i = 0, lat = 0, lon = 0; const out = [];
  const next = () => { let r = 0, sh = 0, b; do { b = s.charCodeAt(i++) - 63; r |= (b & 31) << sh; sh += 5; } while (b >= 32 && i < s.length); return (r & 1) ? ~(r >> 1) : (r >> 1); };
  while (i < s.length) { lat += next(); if (i >= s.length) break; lon += next(); out.push([lat / 1e5, lon / 1e5]); }
  return out;
}

function trainName(raw, mode) {
  let s = (raw || "").replace(/\s*\(.*\)/, "").trim();
  const long = ["ICE","IC","EC","EN","NJ","RJ","RJX","TGV","FLX"];
  const first = s.split(" ")[0];
  if (long.includes(first)) return first;
  const m = s.match(/^([A-Za-z]+)(\d+)$/); if (m) s = m[1] + " " + m[2];
  if (!s) return CFG.longModes.has(mode) ? "Fernzug" : "Zug";
  return s;
}
// Zugart für Aussehen und Animation
function kindOf(name, mode) {
  const n = name.split(" ")[0];
  if (n === "ICE" || n === "TGV" || n === "RJX") return "ice";
  if (n === "NJ" || n === "EN" || mode === "NIGHT_RAIL") return "nj";
  if (n === "IC" || n === "EC" || n === "RJ" || n === "FLX" || CFG.longModes.has(mode)) return "ic";
  if (n === "RE" || n === "IRE" || n === "MEX") return "re";
  if (n === "RB" || n === "S") return "rb";
  return "rb";
}


// DB-Echtzeit (vom eigenen Server, /api/realtime): echte Zeiten in Nersingen (RE/RB) und Ulm Hbf (ICE)
let RT = null;
const trainNumber = raw => { const m = (raw || "").match(/\((\d+)\)/) || (raw || "").match(/^[A-Za-z]+\s*(\d{2,6})$/); return m ? m[1] : null; };

// Um wie viele ms ist der Abschnitt laut DB später als laut Transitous – und fällt er aus?
function rtShift(s) {
  const none = {_shift: 0, _cancel: false, _db: false, _pl: null};
  const num = trainNumber(s.trips?.[0]?.routeShortName);
  if (!RT || !RT.ok || !num) return none;
  const from = s.from?.name || "", to = s.to?.name || "";
  // Halt, an dem der Abschnitt beginnt (Abfahrt) bzw. endet (Ankunft) – Nersingen oder Ulm Hbf
  let e = null, dep = true;
  if (from.includes("Nersingen")) e = RT.nersingen?.[num]?.dp;
  else if (to.includes("Nersingen")) { e = RT.nersingen?.[num]?.ar; dep = false; }
  else if (/^Ulm Hauptbahnhof|^Ulm Hbf/.test(from)) e = RT.ulm?.[num]?.dp;
  else if (/^Ulm Hauptbahnhof|^Ulm Hbf/.test(to)) { e = RT.ulm?.[num]?.ar; dep = false; }
  if (!e) return none;
  const sched = Date.parse(dep ? (s.scheduledDeparture || s.departure) : (s.scheduledArrival || s.arrival));
  if (!(Math.abs(e.pt - sched) < 120000)) return none;   // nicht derselbe Halt
  if (e.cancel) return {_shift: 0, _cancel: true, _db: true, _pl: null};
  const tDelay = Date.parse(dep ? s.departure : s.arrival) - sched;
  const shift = (e.ct - e.pt) - tDelay;
  const atHere = from.includes("Nersingen") || to.includes("Nersingen");
  return {_shift: Math.abs(shift) >= 30000 ? shift : 0, _cancel: false, _db: true, _pl: atHere ? (e.pl || null) : null};
}

// Fahrprofil für Abschnitte, die in Nersingen beginnen ("ab") oder enden ("an"): gleichmäßig anfahren
// bzw. bremsen (0,6 m/s²), sonst konstant – so, dass der Zug genau zur Fahrplanzeit ankommt.
// Gleiche Rechnung für Durchfahrtszeit (Schranke) und Animation (Gleisband, Karte).
function profileT1(L, D, a = 0.6) {
  const q = D * D - 2 * L / a;
  return q > 0 ? D - Math.sqrt(q) : null;   // Dauer der Anfahrphase; null = Abschnitt zu kurz → linear
}
function profilePos(tau, L, D, mode) {      // Strecke nach tau Sekunden
  const t1 = profileT1(L, D), a = 0.6;
  if (!mode || t1 == null) return L * Math.min(1, Math.max(0, tau / D));
  const from = x => x <= t1 ? 0.5 * a * x * x : 0.5 * a * t1 * t1 + a * t1 * (x - t1);
  return mode === "ab" ? from(Math.max(0, tau)) : L - from(Math.max(0, D - tau));
}
function profileTime(s, L, D, mode) {       // Sekunden bis zur Strecke s
  const t1 = profileT1(L, D), a = 0.6;
  if (!mode || t1 == null) return D * s / L;
  const inv = x => x <= 0.5 * a * t1 * t1 ? Math.sqrt(2 * x / a) : t1 + (x - 0.5 * a * t1 * t1) / (a * t1);
  return mode === "ab" ? inv(s) : D - inv(L - s);
}

function parse(raw) {
  const P = {}; CROSSINGS.forEach(c => P[c.id] = []);
  const S = [], seenSeg = new Set(), seen = {}; CROSSINGS.forEach(c => seen[c.id] = new Set());
  const stLons = [], stationDep = {};
  for (const s of raw) if (CFG.railModes.has(s.mode)) Object.assign(s, rtShift(s));
  // Abfahrt in Nersingen nie vor der Ankunft desselben Zugs (+30 s Halt) – sonst gäbe es ihn kurz doppelt,
  // z. B. wenn die DB für die Ankunft mehr Verspätung meldet als für die Abfahrt
  const arrHere = {};
  for (const s of raw) if (s._shift != null && s.to?.name?.includes(CFG.station)) arrHere[s.trips?.[0]?.tripId] = Date.parse(s.arrival) + s._shift;
  for (const s of raw) {
    if (s._shift == null || !s.from?.name?.includes(CFG.station)) continue;
    const a = arrHere[s.trips?.[0]?.tripId], d = Date.parse(s.departure) + s._shift;
    if (a && d < a + 30000) s._shift += a + 30000 - d;
  }
  for (const s of raw) {
    if (!CFG.railModes.has(s.mode)) continue;
    if (s.from?.name?.includes(CFG.station) && !s._cancel) {
      const t = s.trips?.[0]?.tripId, d = new Date(Date.parse(s.departure) + s._shift);
      if (t && !isNaN(d) && (!stationDep[t] || d < stationDep[t])) stationDep[t] = d;
    }
  }
  for (const s of raw) {
    if (!CFG.railModes.has(s.mode) || s._cancel) continue;
    if (s.from?.name?.includes(CFG.station)) stLons.push(s.from.lon);
    if (s.to?.name?.includes(CFG.station)) stLons.push(s.to.lon);
    const dep = new Date(Date.parse(s.departure) + s._shift), arr = new Date(Date.parse(s.arrival) + s._shift);
    if (isNaN(dep) || isNaN(arr)) continue;
    const coords = decodePolyline(s.polyline);
    if (coords.length < 2) continue;
    const name = trainName(s.trips?.[0]?.routeShortName, s.mode);
    // Zugnummer für die Beschriftung, z. B. „ICE 598“, „RE 57049“ (Linie RE 9)
    const num = trainNumber(s.trips?.[0]?.routeShortName);
    const label = num ? `${name.split(" ")[0]} ${num}` : name;
    const kind = kindOf(name, s.mode);
    const isLong = CFG.longModes.has(s.mode);
    const trip = s.trips?.[0]?.tripId || Math.random().toString(36);
    const westbound = s.to.lon < s.from.lon;
    const fromHere = s.from.name.includes(CFG.station), toHere = s.to.name.includes(CFG.station);
    const stops = fromHere || toHere;
    let delay = null;
    if ((s.realTime || s._db) && s.scheduledDeparture) delay = Math.round((dep - new Date(s.scheduledDeparture)) / 60000);

    // Länge entlang der Strecke (Meter) – lokale Projektion um Nersingen
    const lat0 = 48.43, lon0 = 10.12, kx = Math.cos(lat0 * Math.PI / 180) * 111320, ky = 110540;
    const pts = coords.map(([la, lo]) => [(lo - lon0) * kx, (la - lat0) * ky]);
    const cum = [0];
    for (let k = 0; k < pts.length - 1; k++) cum.push(cum[k] + Math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]));
    const total = cum[cum.length - 1]; if (!(total > 0)) continue;

    let nearAny = Infinity;
    for (const c of CROSSINGS) {
      const cx = (c.lon - lon0) * kx, cy = (c.lat - lat0) * ky;
      let best = Infinity, along = 0;
      for (let k = 0; k < pts.length - 1; k++) {
        const ax = pts[k][0] - cx, ay = pts[k][1] - cy, dx = pts[k + 1][0] - pts[k][0], dy = pts[k + 1][1] - pts[k][1];
        const l2 = dx * dx + dy * dy, t = l2 > 0 ? Math.min(1, Math.max(0, (-ax * dx - ay * dy) / l2)) : 0;
        const d = Math.hypot(ax + t * dx, ay + t * dy);
        if (d < best) { best = d; along = cum[k] + t * Math.sqrt(l2); }
      }
      nearAny = Math.min(nearAny, best);
      if (best > CFG.passDistance || seen[c.id].has(trip)) continue;
      seen[c.id].add(trip);
      // Durchfahrt: Hält der Zug in Nersingen, fährt er dort an bzw. bremst – dann mit Anfahr-/Bremsweg
      // statt gleichmäßiger Geschwindigkeit rechnen (beobachtet 6.10.2026: RB 15 kam sonst ~1 Min. zu früh).
      const mode = fromHere ? "ab" : toHere ? "an" : null;
      const pass = new Date(+dep + profileTime(along, total, (arr - dep) / 1000, mode) * 1000);
      // gleicher Zug aus einer zweiten Datenquelle (auch unter anderem Namen, z. B. „DRF“ und „WB 962“):
      // zwei verschiedene Züge können nicht innerhalb einer Minute in dieselbe Richtung über den Übergang fahren
      if (P[c.id].some(p => (p.name === name && Math.abs(p.pass - pass) < 120000) ||
                            (p.westbound === westbound && Math.abs(p.pass - pass) < 60000))) continue;
      // Geschätzte Schließzeiten. Hält der Zug in Nersingen und fährt danach über den Übergang,
      // ist die Schranke schon zu, während er am Bahnsteig steht (beobachtet 4.10.2026: ca. 4 Min. vorher).
      // Die Werte je Fall kommen aus MODEL (Server, im Admin-Bereich aus Messungen angepasst).
      const m = MODEL[c.id], kase = fromHere ? "ab" : toHere ? "an" : "durch";
      const before = kase === "durch" ? (isLong ? m.durch_before_long : m.durch_before) : m[kase + "_before"];
      const after = m[kase + "_after"];
      // Woher kommt die Zeit? db = DB Timetables, rt = Echtzeit aus Transitous, plan = nur Fahrplan
      const src = s._db ? "db" : s.realTime ? "rt" : "plan";
      P[c.id].push({id: trip, name, num, label, kind, isLong, westbound, stops, fromHere, case: kase, pass, delay, src,
        closeFrom: new Date(+pass - before * 1000), closeTo: new Date(+pass + after * 1000)});
    }
    if (nearAny <= CFG.mapDistance) {
      const key = name + "|" + Math.floor(dep / 60000) + "|" + westbound;
      // Gleis in Nersingen (laut OSM von Nord nach Süd: 3 Nebengleis am Mittelbahnsteig, 2 Hauptgleis → Ulm,
      // 1 Hauptgleis → Augsburg). DB-Angabe, sonst: halten → Ulm auf 3, → Augsburg auf 1; durchfahren 2 bzw. 1.
      const plat = s._pl || (westbound ? (stops && !isLong ? "3" : "2") : "1");
      const mode = fromHere ? "ab" : toHere ? "an" : null;
      // derselbe Zug aus zwei Datenquellen (z. B. „DRF“ und „WB 962“) nur einmal zeichnen
      // oder gleiche Zugnummer in gleicher Richtung kurz nacheinander (Doppelgänger mit anderem Abschnitt)
      const twin = S.some(x => x.westbound === westbound && ((Math.abs(x.dep - dep) < 60000 && Math.abs(x.arr - arr) < 60000) ||
                                                          (num && x.num === num && x.dep < arr && dep < x.arr)));
      if (!twin && !seenSeg.has(key)) { seenSeg.add(key); S.push({id: key, trip, name, num, label, kind, westbound, dep, arr, coords, cum, plat, mode}); }
    }
  }
  // Hinter dem Bahnhof geht die Schranke frühestens zu, wenn derselbe Zug an der Schranke davor
  // angekündigt wird – sonst wäre z. B. die Waldstraße vor der Elchinger Straße zu, obwohl der Zug
  // Richtung Augsburg erst über die Elchinger Straße kommt.
  // Hält der Zug nach dem Übergang in Nersingen, bleibt die Schranke zu, bis er dort wieder abfährt
  // (beobachtet 6.10.2026 Elchinger, RB 15 → Augsburg: auf erst 2½ Min. nach der Durchfahrt).
  for (const id in P) for (const p of P[id]) {
    if (p.case !== "an" || !stationDep[p.id]) continue;
    p.stationDep = stationDep[p.id];
    const t = new Date(+p.stationDep + MODEL[id].an_after_dep * 1000);
    if (t > p.closeTo) p.closeTo = t;
  }
  const byTrip = {};
  for (const id in P) for (const p of P[id]) (byTrip[p.id] = byTrip[p.id] || []).push(p);
  for (const list of Object.values(byTrip)) {
    list.sort((a, b) => a.pass - b.pass);
    for (let i = 1; i < list.length; i++) {
      if (!list[i].fromHere) continue;
      const earliest = new Date(Math.min(...list.slice(0, i).map(p => +p.closeFrom)));
      if (list[i].closeFrom < earliest) list[i].closeFrom = earliest;
    }
  }
  for (const id in P) P[id].sort((a, b) => a.pass - b.pass);
  if (stLons.length) stationLon = stLons.reduce((a, b) => a + b, 0) / stLons.length;
  return {P, S};
}

// Fertiges Ergebnis vom Server (/api/state) in die Form der Seite bringen (Date statt ms, Fahrweg entpacken)
function fromState(st) {
  const P = {};
  for (const id in st.passes) P[id] = st.passes[id].map(p => ({...p, pass: new Date(p.pass), closeFrom: new Date(p.closeFrom),
    closeTo: new Date(p.closeTo), stationDep: p.stationDep ? new Date(p.stationDep) : undefined}));
  const lat0 = 48.43, lon0 = 10.12, kx = Math.cos(lat0 * Math.PI / 180) * 111320, ky = 110540;
  const S = st.segments.map(s => {
    const coords = decodePolyline(s.poly), cum = [0];
    for (let k = 0; k < coords.length - 1; k++)
      cum.push(cum[k] + Math.hypot((coords[k + 1][1] - coords[k][1]) * kx, (coords[k + 1][0] - coords[k][0]) * ky));
    return {...s, dep: new Date(s.dep), arr: new Date(s.arr), coords, cum};
  });
  if (st.stationLon) stationLon = st.stationLon;
  if (st.model) for (const id in MODEL) Object.assign(MODEL[id], st.model[id] || {});
  if (st.visitors) Object.assign(VISITORS, st.visitors);
  return {P, S};
}

async function refresh() {
  // Normalfall: der Server rechnet (gleiche Werte wie App und Widgets)
  if (location.protocol !== "file:") {
    try {
      const r = await fetch("/api/state", {cache: "no-store"});
      if (r.ok) {
        const {P, S} = fromState(await r.json());
        passes = P; segments = S; loaded = new Date(); failed = false;
        if (window.onData) window.onData(); return;
      }
    } catch (e) { /* unten selbst rechnen */ }
  }
  const now = Math.floor(Date.now() / 60000) * 60000;  // gerundet: Server-Zwischenspeicher greift für alle
  const q = new URLSearchParams({
    min: `${CFG.minLat},${CFG.minLon}`, max: `${CFG.maxLat},${CFG.maxLon}`, zoom: "15",
    startTime: new Date(now - 30 * 60000).toISOString(), endTime: new Date(now + 90 * 60000).toISOString(),
  });
  // DB-Echtzeit parallel holen; fehlt sie, wird wie bisher nur mit Transitous gerechnet
  const rtReq = location.protocol === "file:" ? Promise.resolve(null)
    : fetch("/api/realtime", {cache: "no-store"}).then(r => r.ok ? r.json() : null).catch(() => null);
  for (const base of SOURCES) {
    try {
      const r = await fetch(base + "?" + q, {cache: "no-store"});
      RT = await rtReq;
      if (!r.ok || !(r.headers.get("content-type") || "").includes("json")) continue;
      const {P, S} = parse(await r.json());
      passes = P; segments = S; loaded = new Date(); failed = false;
      if (window.onData) window.onData(); return;
    } catch (e) { /* nächste Quelle */ }
  }
  failed = true; if (window.onData) window.onData();
}

// Schätzwerte kommen vom Server (/api/config), im Admin-Bereich anpassbar. Ohne Server gelten diese.
const MODEL = {
  elchinger: {durch_before: 120, durch_before_long: 90, durch_after: 30, ab_before: 250, ab_after: 55, an_before: 120, an_after: 45, an_after_dep: 60},
  wald:      {durch_before: 120, durch_before_long: 90, durch_after: 30, ab_before: 250, ab_after: 55, an_before: 120, an_after: 45, an_after_dep: 60},
  fichten:   {durch_before: 80,  durch_before_long: 60, durch_after: 20, ab_before: 250, ab_after: 55, an_before: 80,  an_after: 45, an_after_dep: 60},
};
const VISITORS = {confirm: 2, window: 90, rate: 60, live: true};
async function loadConfig() {
  try {
    const r = await fetch("/api/config", {cache: "no-store"});
    if (!r.ok) return;
    const c = await r.json();
    for (const id in MODEL) Object.assign(MODEL[id], c.crossings?.[id] || {});
    Object.assign(VISITORS, c.visitors || {});
  } catch (e) { /* Standardwerte */ }
}

// Für Meldungen: welcher Zug gehört gerade zu diesem Übergang (der nächste zur Sperrzeit, max. 15 Min.)?
function snapshotFor(id, d) {
  let best = null, bestD = 15 * 60000;
  for (const p of passes[id] || []) {
    const dist = d < p.closeFrom ? p.closeFrom - d : d > p.closeTo ? d - p.closeTo : 0;
    if (dist < bestD) { bestD = dist; best = p; }
  }
  if (!best) return null;
  return {name: best.name, kind: best.kind, case: best.case, westbound: best.westbound, long: best.isLong,
    pass: +best.pass, closeFrom: +best.closeFrom, closeTo: +best.closeTo, delay: best.delay,
    stationDep: best.stationDep ? +best.stationDep : null};
}

const deviceId = (() => {
  try { let d = localStorage.getItem("nsGeraet"); if (!d) { d = Math.random().toString(36).slice(2, 12); localStorage.setItem("nsGeraet", d); } return d; }
  catch (e) { return ""; }
})();

async function postJSON(url, body, method = "POST") {
  const r = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  let data = null; try { data = await r.json(); } catch (e) {}
  return {ok: r.ok, status: r.status, data};
}

// ---------- Zustand ----------
const closedAt = (id, d) => (passes[id] || []).find(p => p.closeFrom <= d && p.closeTo >= d);
const nextAt = (id, d) => (passes[id] || []).find(p => p.closeFrom > d);

function interp(s, d) {
  if (d < s.dep || d > s.arr) return null;
  const total = s.cum[s.cum.length - 1], span = s.arr - s.dep;
  // mit Fahrprofil: am Bahnhof Nersingen sanft bremsen bzw. anfahren
  const target = span > 0 ? profilePos((d - s.dep) / 1000, total, span / 1000, s.mode) : 0;
  for (let k = 0; k < s.coords.length - 1; k++) {
    if (s.cum[k + 1] < target) continue;
    const len = s.cum[k + 1] - s.cum[k], t = len > 0 ? (target - s.cum[k]) / len : 0;
    const a = s.coords[k], b = s.coords[k + 1];
    return {c: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], h: heading(a, b)};
  }
  return null;
}
const heading = (a, b) => Math.atan2((b[1] - a[1]) * Math.cos(a[0] * Math.PI / 180), b[0] - a[0]) * 180 / Math.PI;

function liveTrains(d) {
  const out = [], moving = new Set();
  // je Zug nur ein Abschnitt gleichzeitig (der frühere), damit er nie doppelt erscheint
  for (const s of [...segments].sort((a, b) => a.dep - b.dep)) {
    if (moving.has(s.trip) || (s.num && moving.has("n" + s.num))) continue;
    const p = interp(s, d);
    if (p) { out.push({...s, ...p, halt: false}); moving.add(s.trip); if (s.num) moving.add("n" + s.num); }
  }
  for (const s of segments) {
    if (s.arr > d || moving.has(s.trip)) continue;
    const nexts = segments.filter(x => x.trip === s.trip && x.dep >= s.arr).map(x => +x.dep);
    const until = nexts.length ? Math.min(...nexts) : +s.arr + 90000;
    if (+d > until || d - s.arr >= 600000) continue;
    const n = s.coords.length;
    out.push({...s, c: s.coords[n - 1], h: heading(s.coords[n - 2], s.coords[n - 1]), halt: true, until: nexts.length ? until : null});
    moving.add(s.trip);
  }
  return out;
}
