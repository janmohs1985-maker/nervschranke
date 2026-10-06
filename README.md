# Nervschranke

Live-Schätzung, ob die Bahnschranken in Nersingen (Elchinger Straße, Waldstraße, Fichtenstraße) gerade zu sind.
Läuft unter https://nervschranke.de und https://bahn.mohs.es.

- `index.html` – die Seite (Ampel-Kacheln, Schleichweg-Tipp, Gleisband, Zeitstrahl, Zugliste, Karte, „Ich steh dran“-Melden).
- `core.js` – Rechnung (Zugdaten von Transitous, wann welche Schranke zu ist), gemeinsam für Seite und Admin.
- `admin.html` / `login.html` – Admin-Bereich unter `/admin`: an der Schranke messen, Auswertung, Schätzwerte anpassen.
- `app.py` – kleiner Dienst (FastAPI): liefert die Seiten aus, speichert Transitous-Antworten 30 s zwischen (`/api/trips`),
  nimmt Meldungen an (`/api/report`, `/api/live`), Schätzwerte (`/api/config`) und Admin-Schnittstellen (`/api/admin/…`).
  Daten in `/data` (SQLite). Admin-Passwort aus `ADMIN_PASSWORD`.
- `.github/workflows/build.yml` – baut bei jedem Push auf `main` das Abbild `ghcr.io/janmohs1985-maker/nervschranke`.
- `docker-compose.yml` – so läuft es auf dem Server (`/home/mohs/docker/bahn`) hinter Traefik.

Zum Ausprobieren: `index.html?plus=5` zeigt den Stand in 5 Minuten.

## Meldungen

- Besucher: 1 Meldung pro Gerät und Minute, höchstens 5 pro Anschluss. Eine Meldung zählt erst, wenn genug
  verschiedene Geräte (Standard 2) innerhalb von 90 s dasselbe melden. Bestätigte Meldungen gelten 3 Minuten
  und gehen auf der Seite vor die Schätzung.
- Admin-Meldungen zählen sofort.
- Auswertung: je Übergang und Fall (Durchfahrt, Abfahrt in Nersingen, Ankunft in Nersingen) der Median der
  gemessenen Zeiten; ab 3 Werten gibt es einen Vorschlag. Gespeichert gilt er sofort für alle Besucher.

## Server

Neue Versionen holt der Server selbst, alle 5 Minuten (`/etc/cron.d/nervschranke`):

```
*/5 * * * * root cd /home/mohs/docker/bahn && docker compose pull -q && docker compose up -d
```

Das Abbild ist privat; der Server ist dafür einmal mit `docker login ghcr.io` angemeldet (Token mit `read:packages`).
Das Admin-Passwort steht nur in `/home/mohs/docker/bahn/admin.env`.
