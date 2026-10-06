# Nervschranke

Live-Schätzung, ob die Bahnschranken in Nersingen (Elchinger Straße, Waldstraße, Fichtenstraße) gerade zu sind.
Läuft unter https://nervschranke.de und https://bahn.mohs.es.

- `index.html` – die ganze Seite. Zugdaten von Transitous, Rechnung wie in der Familie-App (`RailCrossing.swift`).
- `nginx.conf` – liefert die Seite aus und speichert die Transitous-Antworten 30 s zwischen (`/api/trips`).
- `Dockerfile` – nginx mit beiden Dateien.
- `.github/workflows/build.yml` – baut bei jedem Push auf `main` das Abbild `ghcr.io/janmohs1985-maker/nervschranke`.
- `docker-compose.yml` – so läuft es auf dem Server (`/home/mohs/docker/bahn`) hinter Traefik.

Zum Ausprobieren: `index.html?plus=5` zeigt den Stand in 5 Minuten.

## Server

Neue Versionen holt der Server selbst, alle 5 Minuten (`/etc/cron.d/nervschranke`):

```
*/5 * * * * root cd /home/mohs/docker/bahn && docker compose pull -q && docker compose up -d
```

Das Abbild ist privat; der Server ist dafür einmal mit `docker login ghcr.io` angemeldet (Token mit `read:packages`).
