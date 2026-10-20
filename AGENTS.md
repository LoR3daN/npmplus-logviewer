# AGENTS.md

NPMplus · Access log viewer is a FastAPI web app that reads and visualizes the Nginx (NPMplus) access logs of a reverse proxy: a filterable/sortable/paginated table, request summary cards, and a live animated map (Leaflet 2D + globe.gl 3D). It reads the single-line `access_detailed.log` format, one record per request.

## Run

```sh
docker compose up -d          # recommended (reads /opt/npmplus/nginx/logs, port 8899)
# or locally:
pip install -r requirements.txt
uvicorn app.server:app --host 0.0.0.0 --port 8899
```

CI: GitHub Actions `.github/workflows/docker-image.yml` builds and publishes the image to `ghcr.io/lor3dan/npmplus-logviewer` on every push to `main`.

## Repository layout

```
app/
  server.py    FastAPI app: endpoints, prefs persistence, public-IP detection
  parser.py    LogStore: log parsing, incremental scanning, filtering, collapse
  geo.py       ISO 3166-1 alpha-2 -> [lat, lon] country centroids (map beams)
  static/      Vanilla JS/CSS frontend (app.js, map.js, index.html, style.css, favicon.svg)
docs/screenshots/  UI screenshots (sample, sanitized data only)
Dockerfile     python:3.12-slim, uvicorn on 8899
docker-compose.yml
requirements.txt   pinned: fastapi 0.115.6, uvicorn[standard] 0.32.1
```

## Environment variables

`LOG_DIR` (`/logs`), `REFRESH_SECONDS` (`5`), `TZ` (`Europe/Madrid`), `PREF_FILE` (`/state/prefs.json`), `CARDS_HOSTS`, `MY_IP`, `PUBLIC_IP_REFRESH` (`600`), `CARTO_KEY`. See README for details.

## API

`GET /api/stats`, `GET /api/entries`, `GET /api/live`, `GET /api/home`, `GET /api/config`, `GET|POST /api/prefs`, `POST /api/reload`. Full parameter reference in README.

## Conventions

- **Log parsing is the core.** The viewer reads `access_detailed.log*` with one line per request: `[date] host ip response_time "request" status body_bytes bytes_sent "referer" "ua" [COUNTRY]`. `parse_line` uses a single regex; changing the format or regex breaks everything.
- Rotated files are tracked by **inode + size + first-line fingerprint**, with a pending buffer for incomplete lines. NPMplus logrotate uses `copytruncate`, so `scan()` must handle in-place truncation; the dedup key (timestamp, IP, host, method, path, status, body, bytes, time) avoids duplicates when `.1` copies are read.
- Filters, sort, pagination and collapse (`_collapse_runs`, key = host + IP + status) live in `parser.py`; keep them consistent between `/api/entries` and `/api/live`.
- The app has **no built-in login** — if exposed publicly it must sit behind an NPMplus Access List / Basic Auth. Don't add auth logic without discussion.
- Screenshots in `docs/screenshots` must use **sample, sanitized data** (masked IPs, placeholder hostnames) — never real traffic.
- Dependencies are pinned exactly in `requirements.txt`; pin new ones the same way.
- Frontend is vanilla JS (no framework, no bundler). Static assets mount under `/static`.
