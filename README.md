<div align="center">

<img src="frontend/public/brand/mark-512.png" alt="Schema logo" width="160">

# Schema

**Preview-first media renaming for movies, TV and anime: a web-native FileBot replacement.**

<br>

![Version](https://img.shields.io/badge/version-0.1.0-7c3aed?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![React](https://img.shields.io/badge/react-18-61DAFB?style=for-the-badge&logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/typescript-5.9-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Providers](https://img.shields.io/badge/providers-TMDb%20%7C%20TVDB%20%7C%20AniDB%20%7C%20Trakt%20%7C%20TVmaze-01B4E4?style=for-the-badge)
![Media servers](https://img.shields.io/badge/media%20servers-Plex%20%7C%20Jellyfin%20%7C%20Emby-E5A00D?style=for-the-badge&logo=plex&logoColor=white)

[Quick start](#quick-start) · [Features](#features) · [Download clients](#download-clients) · [Configuration](#configuration) · [Documentation](#documentation) · [Development](#development) · [Known limitations](#known-limitations)

The media renaming app of the **NEXUS** suite

</div>

| Watch | Match | Rename & move | Clean up |
|---|---|---|---|
| Watch folders and SABnzbd / qBittorrent webhooks pick up finished downloads. A file only counts once it and its whole download folder have stopped changing. | Files are identified with TMDb, TheTVDB, AniDB, Trakt and TVmaze, with automatic fallback. Every item gets a confidence score; low-confidence items wait in Review. | Each job previews *original → parsed → proposed path*, then moves, copies or links files atomically, with sidecars, conflict policies and a persistent undo journal. | Empty source folders are removed, download leftovers are held for a day and then deleted, and Plex / Jellyfin / Emby refresh only the folders that changed. |

**Schema is a web-native media organizer and renamer for movies, TV and anime. It replaces FileBot and the `jlesage/filebot` noVNC container: it shows a reviewable preview of every rename, and then moves, copies or links files safely with full undo.**

## Features

- **Preview first** - every job shows *original → parsed → proposed path* with confidence scores. Low-confidence items wait for review, including items from automated jobs.
- **Metadata providers with fallback** - TMDb, TheTVDB, AniDB, Trakt and TVmaze, with per-kind priority chains and recorded fallback reasons.
- **TV and anime logic** - multi-episode files, specials (`S00Exx` and un-numbered OVAs), daily shows, and absolute ↔ seasonal anime numbering through provider absolute order and the Anime-Lists mapping.
- **FileBot-compatible formats** - `{n} - {s00e00} - {t}` and `{ny}/{'Season '+s}/…` work as-is, evaluated by a sandboxed engine with live validation.
- **Automation** - watch folders, SABnzbd / qBittorrent / generic webhooks with path mappings and category → kind hints, and targeted Plex / Jellyfin / Emby refreshes.
- **Waits for the whole download** - a watch-folder file is moved only once its entire download folder has been unchanged for `release_quiet_seconds` (default 120 s) and holds no temp file (`*.part`, `*.aria2`, `*.!qB` …). A movie is never moved while its extras are still downloading, so the download client doesn't re-download a file that vanished mid-torrent.
- **Download-folder cleanup** - after a move job from a watch folder or webhook, once every video in the download folder is in the library (samples and extras skipped), the folder and its leftovers (`.nfo`, `.jpg`, `.txt`, samples, extras) go to a holding folder, `.schema-trash/leftovers/`, on the same disk and are deleted after 24 h. Undo still works in the meantime. Controlled by `clean_release_folders` (on by default).
- **Safe file operations** - atomic moves, conflict policies (skip, overwrite, upgrade, index, fail), sidecars that travel with their video, files restricted to configured roots, and a persistent undo journal. Replaced files are moved to trash, never deleted.
- **Tidy libraries** - a movie already in its own folder has that folder renamed in place; source folders left empty by a move are removed (never a root).
- **One container** - one process serves the API, WebSocket live updates and the web app on port 8420; state lives in SQLite under `/config`. Works on Unraid, behind a reverse proxy, and as a drop-in for the `jlesage/filebot` volume layout.
- **Demo mode** - a fictional sample library matched by an offline fixtures provider, no API keys needed.

## Quick start

### Docker

```bash
docker compose up -d --build        # builds the image from this repository
docker compose logs -f schema       # wait for "Uvicorn running on http://0.0.0.0:8420"
```

Open `http://<server>:8420` and create the admin account with the one-time setup code from the log.
Every push to `main` also publishes `ghcr.io/cwramey/schema-media:latest`. Volumes, Unraid, reverse
proxies, backups and upgrades are covered in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Development (demo mode)

Requirements: Python 3.12+, Node 20+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
SCHEMA_DEMO=1 .venv/Scripts/python -m schema_app  # http://127.0.0.1:8420
```

```bash
cd frontend
npm ci
npm run dev                                         # http://127.0.0.1:5173 (proxies /api to :8420)
```

Demo mode builds a fictional sample library under `backend/data/demo/` and matches it with an
offline fixtures provider, so you need no API keys. It covers multi-episode files, specials, anime
absolute numbering, remakes, multi-version movies and CRC checks.

## Download clients

| Client | How |
|---|---|
| SABnzbd | post-processing script `deploy/hooks/sabnzbd/schema_notify.py` (also in the image at `/app/hooks`); key in SABnzbd's environment (`SCHEMA_API_KEY` or `SCHEMA_API_KEY_FILE`) |
| qBittorrent | *Run external program on torrent finished* → `curl -H @/config/schema-api-key` form POST, see [deploy/hooks/qbittorrent/README.md](deploy/hooks/qbittorrent/README.md) |
| Anything else | `POST /api/webhooks/generic` (or `deploy/hooks/generic/schema-notify.sh`), or a watch folder |

Webhooks need the secret key from **Settings → Download clients**, sent as the `X-Api-Key` header.
Keep it off command lines, where `ps` and the client's logs show it: the hooks read it from a key
file (`X-Api-Key: …`, one line) or an environment variable. Path mappings translate the client's
paths to Schema's, and categories (`tv`, `sonarr`, `movies`, `radarr`, `anime` …) set the media kind.

A webhook job runs on its own, so its paths must lie **inside the download area**: below a watch
folder or the Schema side of a path mapping (same path on both sides is fine), inside the allowed
roots, and never an allowed root, a library root, a folder containing one, or anything in the
library. Other paths get `403`. Details in [docs/DEPLOYMENT.md §7](docs/DEPLOYMENT.md#7-download-clients).

> **rdt-client and other clients that keep seeding or re-checking:** set the client to remove the
> torrent once it is finished. Otherwise it may download the files again after Schema has moved
> them. For torrents you want to keep seeding, use mode `copy`, `hardlink` or `keeplink` instead of
> `move`.

## Configuration

First run: create the admin account, then set **allowed roots** (every folder Schema may touch),
**library roots** per kind, the default mode (start with **test**, a dry run) and provider keys.
Settings live in the UI; environment variables (`SCHEMA_*`, provider keys, `PUID`/`PGID`, `UMASK`,
`TZ`) are listed in [docs/DEPLOYMENT.md §5](docs/DEPLOYMENT.md#5-environment-reference).

Download-folder handling (**Settings → Moving files**):

| Setting | Default | In the UI | What it does |
|---|---|---|---|
| `remove_empty_source_folders` | on | Delete empty download folders afterwards | After a move, removes source folders left completely empty, stopping at any protected folder |
| `clean_release_folders` | on | Clean up download folders afterwards | After a watch-folder or webhook move job, moves a finished download folder and its leftovers to `.schema-trash/leftovers/` and deletes it after 24 h |
| `release_quiet_seconds` | `120` | (settings API only) | Watch folders wait until the whole download folder has been unchanged this long; `0` turns the wait off |

## Documentation

| Document | What's in it |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | Engineering specification: legacy gap analysis, architecture and data flow, stack, API, roadmap |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, Compose, Unraid, reverse proxy |
| [docs/MIGRATION_FROM_FILEBOT.md](docs/MIGRATION_FROM_FILEBOT.md) | Moving from the jlesage/filebot container |

## Development

### Tests

```bash
cd backend && .venv/Scripts/python -m pytest
cd frontend && npm run typecheck && npm run lint && npm test && npm run build
```

### Repository layout

```
backend/   FastAPI app (schema_app/), tests/
frontend/  React + Vite + Tailwind SPA
deploy/    container entrypoint, Unraid template, download-client hooks
docs/      spec, deployment and migration guides
logo/      source brand assets (processed by scripts/brand.py)
```

## Known limitations

- **Clients that re-download** - rdt-client and similar clients must remove the torrent when it finishes, or they may fetch the files again after Schema moves them (see [Download clients](#download-clients)).
- **Download-folder cleanup scope** - only move jobs started by a watch folder or webhook are cleaned up; a folder is kept while it still holds a temp file or a video Schema hasn't moved.
- **Trash is yours to empty** - leftovers are deleted after 24 h, but files replaced by the overwrite / upgrade policies stay in trash until you delete them.
- **Hardlinks** - fail across filesystems (including between Unraid `/mnt/user` shares) with a clear error instead of copying.
- **Reverse proxy** - Schema must be served at the root of a host name; sub-paths such as `/schema/` are not supported.
- **Not in v1** - music and photo renaming, subtitle downloading, artwork / NFO writing, multi-user roles and transcoding.
