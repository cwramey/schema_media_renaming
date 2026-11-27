# Deploying Schema

Schema ships as **one container running one process**. Uvicorn serves the API, the WebSocket and the
built web app on port **8420**. State lives in SQLite under `/config`. There is no Redis, no worker
container and no license server.

| | |
|---|---|
| Image | built from this repository (`Dockerfile`), e.g. `schema-media:latest` |
| Port | `8420` (web UI, API, webhooks, WebSocket `/api/ws`) |
| Volumes | `/config` (state) · `/storage` (downloads **and** media, one mount) · optional `/watch` and `/output` |
| User | non-root `user: "PUID:PGID"` with a read-only image (recommended), or root mode with `PUID`/`PGID` (or jlesage-style `USER_ID`/`GROUP_ID`); `UMASK`, `TZ` |
| Health | `HEALTHCHECK` calls `GET /api/health` every 30 s and needs `"status": "ok"` |

Moving from the `jlesage/filebot` container? Read [MIGRATION_FROM_FILEBOT.md](MIGRATION_FROM_FILEBOT.md)
as well.

---

## 1. Quick start with Docker Compose

On the server (for example the Unraid box at `192.168.1.20`):

```bash
git clone <this repository> schema && cd schema
# edit docker-compose.yml: PUID/PGID, SCHEMA_ALLOWED_HOSTS, the /storage mount (§3)
docker compose up -d --build
docker compose logs -f schema          # wait for "Uvicorn running on http://0.0.0.0:8420"
```

Open `http://192.168.1.20:8420`. The first visit asks you to create the admin account, using the
one-time setup code from the log (`docker compose logs schema | grep "setup code"`), so nobody else
on the network can claim a fresh instance first.

Without Compose:

```bash
docker build -t schema-media:latest .
docker run -d --name schema --restart unless-stopped -p 8420:8420 \
  --security-opt no-new-privileges --cap-drop ALL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETGID --cap-add SETUID \
  --memory 1536m --cpus 2 --pids-limit 256 --stop-timeout 120 \
  -e PUID=99 -e PGID=100 -e UMASK=022 -e TZ=Europe/Berlin \
  -e SCHEMA_ALLOWED_HOSTS=tower.local,192.168.1.20 \
  -v /mnt/user/appdata/schema:/config \
  -v /mnt/user/data:/storage \
  schema-media:latest
```

That is root mode (§3): the entrypoint uses the five capabilities to create the `PUID` user and then
drops them. Non-root mode needs none of them and runs with a read-only image; see §3.

The image builds in three stages: `node:22-alpine` runs `npm ci && npm run build` for the web app
(the lockfile is required), a `python:3.12-slim` stage installs the backend into a virtualenv, and
the slim runtime adds `mediainfo`, `tini` and `gosu`. Python dependencies are installed in a layer
that depends only on `backend/pyproject.toml`, so a code change rebuilds in seconds; that layer is
re-resolved when `pyproject.toml` or the base image changes, or when you pass
`--build-arg PY_DEPS_REFRESH=$(date +%F)`. Building needs internet access for npm and PyPI; running
doesn't, except for the metadata providers you enable.

## 2. First-run setup

1. **Create the admin account** (`/setup`).
2. **Settings → General → Allowed roots:** every folder Schema may read or write, e.g. `/storage`,
   `/watch`, `/output`. Schema refuses any path outside these, in the file browser, jobs, watch
   folders, webhooks and every file operation (checked again at execution time, with symlinks
   resolved).
3. **Library roots** per kind (where renamed files go), e.g. `/storage/media/Movies`,
   `/storage/media/TV`, `/storage/media/Anime`. Formats are relative to these.
4. **Default mode and conflict policy.** Start with mode **test** (a dry run that touches nothing)
   until the previews look right.
5. **Settings → Providers:** TMDb / TheTVDB / AniDB / Trakt keys (TVmaze needs none). Enter them
   here rather than as environment variables, which anyone with `docker inspect` can read (§6).
6. **Settings → Automation:** copy the webhook API key for download clients (§7) and add path
   mappings.
7. **Watch folders** (optional): add `/watch` (or any folder inside the allowed roots).

To try it without real media, start a throwaway container with `-e SCHEMA_DEMO=1`. It builds a
fictional library under `/config/demo` and matches it offline.

## 3. Volumes, users and permissions

| Container path | Purpose | Ownership |
|---|---|---|
| `/config` | `schema.db` (SQLite, WAL), `cache.sqlite3` (provider HTTP cache), `secret.key` (cookie signing, mode 0600), `trash/` (replaced files), `demo/` (demo mode only) | root mode: chowned to `PUID:PGID` on start (only entries that need it); non-root mode: must already belong to the user |
| `/storage` | Your downloads **and** your media, in one mount (below) | **never chowned**; Schema needs read/write where it renames |
| `/watch` | Optional watch folder (jlesage AMC input) | never chowned |
| `/output` | Optional library root (jlesage AMC output) | never chowned |

### One mount for downloads and media, and never appdata

Mount **one** folder that contains both your downloads and your library, for example a `data` share
laid out like this (the [TRaSH guides](https://trash-guides.info/File-and-Folder-Structure/) layout;
keeping `downloads/` inside the media share, as in `deploy/ugreen/docker-compose.yml`, works the
same way):

```
/mnt/user/data             -> /storage            (UGREEN: /volume1/data or /volume1/media)
├── downloads/             <- download clients write here
└── media/
    ├── Movies/  TV/  Anime/   <- library roots
/mnt/user/appdata/schema   -> /config             (separate, never inside /storage)
```

**Why one mount.** Linux can rename or hardlink a file only within a single mount, and every bind
mount is its own mount, even when two of them come from the same disk or share. With
`/volume1/downloads:/storage/downloads` and `/volume1/media:/storage/media`, `rename()` fails with
`EXDEV` and every move becomes copy, fsync, CRC read-back and delete: on an HDD RAID a 40 GB file
takes minutes instead of milliseconds, needs its space twice meanwhile, and competes with Jellyfin
for the disks. Hardlink mode fails outright. With one mount over the common parent, a move on the
same filesystem is an instant rename and hardlinks work. The same applies to `/watch` and
`/output`: they are separate mounts, so prefer a watch folder or library root under `/storage`.

Download clients should see the same paths: mount the same parent (or just its `downloads/` folder
at `/storage/downloads`) in their containers, and no path mappings are needed.

**Why never appdata.** Do not mount a folder that contains `appdata`, such as all of `/mnt/user` or
`/volume1`. Schema reads, renames, moves and trashes files under its allowed roots, and `appdata`
holds Schema's own `secret.key` (whoever reads it can forge an admin session) and `schema.db`
(password hash, webhook API key, provider keys, media-server tokens), plus every other container's
configuration and secrets. One allowed root set too wide, one bad match or one stolen session would
then reach all of it. `docker exec schema ls /storage` should list your data folders and nothing
else.

### Non-root mode (recommended)

The container runs as your user from the start: no root, no capabilities, and a read-only
image. Schema can write only to `/config`, your media mounts and `/tmp`.

```yaml
services:
  schema:
    user: "1000:1000"           # uid:gid that own your media (`id <user>` on the host)
    group_add: ["1001"]         # optional extra groups, e.g. one that owns a share
    read_only: true
    tmpfs:
      - /tmp:size=256m          # in RAM, counts towards mem_limit
    security_opt:
      - no-new-privileges:true
    cap_drop: [ALL]             # and no cap_add
```

(`docker run`: `--user 1000:1000 --group-add 1001 --read-only --tmpfs /tmp:size=256m --cap-drop ALL
--security-opt no-new-privileges`.)

* Give the appdata folder to that user **before** the first start, e.g.
  `mkdir -p /volume1/docker/schema && chown -R 1000:1000 /volume1/docker/schema`. Docker creates a
  missing folder as root, and a non-root container can't fix that: it exits with code 73 and says so.
  Switching an existing root-mode install needs nothing else when `user:` is set to its old
  `PUID:PGID`: root mode already made `/config` belong to them.
* `PUID`, `PGID` and `SUP_GROUP_IDS` are ignored (the log notes a `PUID` that differs from the
  actual uid); `group_add` replaces `SUP_GROUP_IDS`.
* gid 0 among the container's groups is refused like in root mode.
* `deploy/ugreen/docker-compose.yml` uses this mode.

### Root mode (`PUID` / `PGID`)

Without `user:`, the container starts as root, creates a `schema` user with `PUID`/`PGID`, makes
`/config` belong to it and drops to it with `gosu` before Schema starts. The entrypoint needs the
capabilities `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID` for that (`cap_drop: [ALL]`
plus exactly these in `cap_add`) and a writable root filesystem, because it edits `/etc/passwd`.
Use it when non-root mode can't work: an appdata folder you can't chown on the host (NFS
`root_squash`, a share owned by another user) or a Docker UI that can't set the user.

* `PUID` / `PGID`: the user Schema runs as. Use the owner of your media (Unraid: `99`/`100`).
  `USER_ID` / `GROUP_ID` are accepted when `PUID`/`PGID` are not set, so a jlesage environment block
  works unchanged.
* `SUP_GROUP_IDS`: extra groups, comma separated (e.g. a group that owns a share).
* **Root is refused.** `PUID=0`, `PGID=0` or a `0` in `SUP_GROUP_IDS` stops the container (exit 64):
  as root, Schema could change any file on every mounted share regardless of its permissions. Set
  `SCHEMA_ALLOW_ROOT=1` to override that if you really must.
* The ownership fix only touches entries in `/config` not yet owned by `PUID:PGID`, stays on that
  filesystem and never follows symlinks.

### UMASK (both modes)

`UMASK` is the mask for files and folders Schema creates, 1 to 4 octal digits (anything else stops
the container with exit 64). `022` (default): only the owner can write them. `002`: the group too,
for shares that rely on group write. Masks that leave new files writable by everyone (`000`, `001`,
`004`, `005`) log a warning: with `000` any user or container that can reach the share can change
your renamed media. Moved files keep their permissions; copies get the umask.

### Where moved-aside files go (trash)

With conflict policy **overwrite** (or **upgrade** when the new file wins), the existing file is
**moved, never deleted**:

* to `/config/trash/<date>/<time>-<id>/` when `/config` is on the same filesystem as the file;
* otherwise to `<allowed root>/.schema-trash/<date>/...` on the media's own disk, so a 40 GB movie
  isn't copied into the app-data volume. If that isn't possible either, it goes to
  `<folder>/.schema-trash/...` next to the file.

Every trash folder has a `schema-trash.json` manifest (original → trashed path). Undo restores from
it. Schema never empties the trash: delete old dated folders yourself once you're sure, for example
`find /mnt/user -path '*/.schema-trash/*' -type d -mtime +30 -prune -exec rm -rf {} +`. Plex,
Jellyfin and Emby ignore dot-folders.

### Safety guarantees of file operations

* Copies (and cross-filesystem moves) are written to `.<name>.schema-tmp` in the destination
  folder, fsynced, verified (size, plus CRC32 for cross-filesystem moves) and then renamed into
  place. A crash never leaves a half-written file under the final name.
* Renames never overwrite an existing file silently. Conflicts follow the policy (skip, overwrite,
  upgrade, index, fail). A destination that already holds the identical file (same inode or
  byte-identical content) counts as done.
* Each item is all-or-nothing: if a sidecar (subtitle, `.nfo` ...) or the symlink of `keeplink`
  fails, the steps already done are rolled back.
* Hardlinks across filesystems fail with a clear message instead of silently copying.
* Undo refuses when the renamed file changed since, or when its original location is occupied.

## 4. Unraid

1. Build the image on the server once: `docker build -t schema-media:latest /path/to/schema`.
2. Copy `deploy/unraid/schema.xml` to `/boot/config/plugins/dockerMan/templates-user/my-schema.xml`.
3. **Docker → Add Container → Template: Schema**. Defaults: `/mnt/user/appdata/schema` → `/config`,
   `/mnt/user/data` → `/storage`, PUID `99`, PGID `100`, UMASK `022`, and fill in *Allowed hosts*.
   *Extra Parameters* are preset to root mode with only the entrypoint's capabilities and the same
   limits as the compose files: `--security-opt no-new-privileges --cap-drop ALL --cap-add CHOWN
   --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add SETGID --cap-add SETUID --pids-limit 256
   --memory 1536m --cpus 2 --stop-timeout 120`.
4. If you push the image to a registry, change *Repository* in the template.

Each top-level folder of `/mnt/user` is its own share. Keep downloads and media in **one** share
(`data/downloads`, `data/media`) and map only that (§3); the template used to map all of `/mnt/user`,
which also exposed `appdata`, `system`, `domains` and `isos`. When you change the mapping, update
Settings → Allowed roots, library roots, watch folders and path mappings to the new paths; Undo of
operations done before the change refers to the old paths.

**Non-root mode on Unraid:** `chown -R 99:100 /mnt/user/appdata/schema` (root mode already did this
for an existing install), then in *Extra Parameters* replace the five `--cap-add` flags with
`--user 99:100 --read-only --tmpfs /tmp:size=256m` (plus `--group-add <gid>` for extra groups).
PUID, PGID and SUP_GROUP_IDS are ignored then.

Hardlink mode on Unraid works only within one disk or pool. Through the `/mnt/user` FUSE share,
hardlinks between shares can fail; Schema then reports it instead of copying.

## 5. Environment reference

| Variable | Default (container) | Meaning |
|---|---|---|
| `SCHEMA_DATA_DIR` | `/config` | DB, cache, trash, secret key (`./data` outside Docker) |
| `SCHEMA_HOST` / `SCHEMA_PORT` | `0.0.0.0` / `8420` | Bind address and port |
| `SCHEMA_STATIC_DIR` | `/app/web` | Built web app (`../frontend/dist` outside Docker) |
| `SCHEMA_SECRET_KEY` | generated → `/config/secret.key` | Cookie signing key; set it only to share sessions across instances |
| `SCHEMA_AUTH` | `required` | `disabled` removes the login (trusted LANs only; a warning banner shows) |
| `SCHEMA_SETUP_TOKEN` | generated, printed to the log | One-time code required to create the first admin account |
| `SCHEMA_ALLOWED_HOSTS` | unset (any) | Comma-separated host names/IPs the server answers to; blocks DNS-rebinding attacks. Set it (every template has it); loopback is always allowed, so the healthcheck keeps working |
| `SCHEMA_ALLOWED_ORIGINS` | unset | Extra browser origins allowed to open the WebSocket (when a proxy rewrites `Host`) |
| `SCHEMA_API_DOCS` | `0` | `1` serves the OpenAPI docs at `/api/docs` |
| `SCHEMA_DEMO` | `0` | `1` = offline fixtures provider and a demo library under `<data_dir>/demo` |
| `SCHEMA_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `TMDB_API_KEY`, `TVDB_API_KEY`, `TVDB_PIN`, `ANIDB_CLIENT`, `ANIDB_CLIENT_VERSION`, `TRAKT_CLIENT_ID` | unset | Provider credentials; when set they override what's saved in the UI. Prefer the UI: environment values show in `docker inspect` (§6) |
| `PUID` / `PGID` (`USER_ID` / `GROUP_ID`) | `1000` / `1000` | Root mode: container user and group; `0` is refused. Ignored in non-root mode |
| `SUP_GROUP_IDS` | unset | Root mode: extra groups, comma separated; `0` is refused |
| `SCHEMA_ALLOW_ROOT` | unset | `1` allows uid/gid 0 (`PUID`, `PGID`, `SUP_GROUP_IDS`, or the groups of a non-root `user:`). Not recommended |
| `UMASK` | `022` | File creation mask, 1-4 octal digits; world-writable masks log a warning |
| `TZ` | `UTC` | Time zone for logs and dated trash folders |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Uvicorn: proxies whose `X-Forwarded-*` headers are trusted (see §6); also makes login throttling per client instead of per proxy |

## 6. HTTPS, exposure and reverse proxy

### Why plain `http://<nas-ip>:8420` isn't enough

* **Everything is cleartext.** Your password at login, the session cookie on every request, and
  the keys and tokens you type into Settings cross the network unencrypted. Anything that can see
  LAN traffic (shared Wi-Fi, a compromised IoT device) can take over the session.
* **Cookies ignore ports.** The browser sends Schema's session cookie for `192.168.1.20` to every
  app on that address: Jellyfin on `:8096`, the download client, the NAS's own admin UI. Any of
  them can log it. SameSite doesn't separate them either, because a site is scheme plus host, not
  port: a script running in any of those apps is same-site with Schema.

The fix is to give Schema **its own host name with TLS**, e.g. `https://schema.home.example.com`.
Its cookie then stays with that name and is marked `Secure`. Serve it at the root of the name (a
sub-path such as `/schema/` is not supported) and forward WebSockets for `/api/ws` (live updates).

### Settings that go with it

* `SCHEMA_ALLOWED_HOSTS=schema.home.example.com` (add any LAN name or IP you still use). Requests
  for other names are refused with *Invalid host header*.
* `FORWARDED_ALLOW_IPS`: the address the proxy connects from, as the container sees it: the proxy
  container's IP on a shared network, or the Docker bridge gateway (usually `172.17.0.1`;
  `docker network inspect bridge`) for a proxy running on the host, Tailscale Serve included. Then
  Schema sees `https`, marks its cookie `Secure` and throttles logins per client.
* **Publish the port narrowly.** Proxy on the same host: `127.0.0.1:8420:8420`, so plain HTTP is
  reachable only through the proxy. Proxy as a container on the same Docker network: no `ports:`
  at all (download clients on that network keep using `http://schema:8420`). Keeping plain HTTP on
  the LAN: `192.168.1.20:8420:8420` listens on the LAN address only. Published ports bypass host
  firewalls such as `ufw`.
* **Keep provider keys out of environment variables.** `docker inspect schema` shows every
  variable in clear text to anyone who can use Docker on the host (as does the container view in
  Portainer or UGOS), and compose files end up in backups and repositories. Enter the keys in
  Settings → Providers, where they're stored in `/config`, then remove the variables: while set,
  they override what's saved in the UI.

### Tailscale Serve

The least setup: HTTPS with a real certificate, reachable only from your tailnet. On the NAS (with
MagicDNS and HTTPS certificates enabled for the tailnet):

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8420
```

Schema is then at `https://<nas>.<tailnet>.ts.net/`. Publish the port as `127.0.0.1:8420:8420`, set
`SCHEMA_ALLOWED_HOSTS=<nas>.<tailnet>.ts.net` and `FORWARDED_ALLOW_IPS` to the bridge gateway.

**nginx**

```nginx
server {
    listen 443 ssl;
    server_name schema.home.example.com;
    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:8420;   # or http://schema:8420 on a shared Docker network
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1h;   # keep the WebSocket open
    }
}
```

**Caddy**

```caddy
schema.home.example.com {
    tls internal                # Caddy's own CA (trust its root once per device), or a DNS-challenge certificate
    reverse_proxy schema:8420   # same Docker network; 127.0.0.1:8420 when Caddy runs on the host
}
```

Caddy forwards WebSockets and `X-Forwarded-*` automatically. With a public domain you own, Caddy
can get a Let's Encrypt certificate through a DNS challenge without exposing anything to the
internet.

**Traefik** (labels on the `schema` service)

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.schema.rule=Host(`schema.home.example.com`)
  - traefik.http.routers.schema.entrypoints=websecure
  - traefik.http.routers.schema.tls=true
  - traefik.http.routers.schema.tls.certresolver=letsencrypt   # your resolver's name
  - traefik.http.services.schema.loadbalancer.server.port=8420
```

With Traefik on the same Docker network, drop the `ports:` section from Schema entirely.

Webhooks from download clients on the same Docker network can keep using `http://schema:8420`
directly instead of going through the proxy.

## 7. Download clients

Every webhook needs the API key from **Settings → Automation**, sent as the `X-Api-Key` header.
Keep it off command lines (they show up in process lists and logs): read it from a file or an
environment variable, as the hooks below do.

| Client | How | Details |
|---|---|---|
| SABnzbd | post-processing script `deploy/hooks/sabnzbd/schema_notify.py` (also inside the image at `/app/hooks`) | set `SCHEMA_URL` and `SCHEMA_API_KEY` (or `SCHEMA_API_KEY_FILE`) in the SABnzbd container's environment; failed downloads are not sent |
| qBittorrent | *Run external program on torrent finished* → `curl` form POST | exact command line in `deploy/hooks/qbittorrent/README.md` |
| Anything else | `POST /api/webhooks/generic` with `{"path": "..."}` or `{"paths": [...], "kind": "movie", "category": "..."}` | or `deploy/hooks/generic/schema-notify.sh -k movie /storage/downloads/x` |

* **Path mappings** translate the client's view to Schema's: `/downloads` → `/storage/downloads`.
  Mounting downloads at the same path in every container avoids them.
* **Webhook paths are confined** to the download area: strictly below a watch folder or a
  path-mapping target, inside the allowed roots, and never a library root or inside one (unless the
  download area itself is). Anything else is refused with 403. With downloads mounted at the same
  path everywhere, add an identity mapping (`/storage/downloads` → `/storage/downloads`) or a watch
  folder there.
* **Category → kind**: `tv`, `sonarr` → series; `movies`, `radarr` → movie; `anime` → anime.
  Matching is case-insensitive and also checks parts (`tv/hd`, `tv-sonarr`). Edit the map in
  Settings.
* Webhook jobs auto-execute confident matches with the default mode; low-confidence items wait in
  **Review**.
* Seeding torrents: use `copy`, `hardlink` (same filesystem) or `keeplink`, not `move`.

### Watch folders

A file is picked up once its size and modification time have been stable for the folder's settle
time (default 30 s), it isn't empty, and it doesn't match the ignore patterns (`*.part`, `*.!qB`,
`*.tmp`, `*.crdownload`; hidden files, `@eaDir` and `#recycle` are always ignored). Files that become
ready together (a season pack) form one job. A full rescan every 60 s backs up inotify, which misses
changes made over SMB/NFS by other machines. A folder that disappears (unmounted share) shows as
**missing** and is picked up again when it returns.

Large trees on Linux may need more inotify watches:
`sysctl fs.inotify.max_user_watches=524288` on the host. If the limit is hit, the folder shows a
note and Schema falls back to rescanning.

## 8. Media servers

**Settings → Media servers → Add**, then **Test**:

| Server | URL | Credential |
|---|---|---|
| Plex | `http://192.168.1.20:32400` | `X-Plex-Token` ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
| Jellyfin | `http://192.168.1.20:8096` | Dashboard → API Keys |
| Emby | `http://192.168.1.20:8096` | Settings → Advanced → API Keys |

After files land, Schema refreshes only the touched folders: a Plex partial scan of the matching
library section, or Jellyfin/Emby `/Library/Media/Updated`. If no Plex section contains the folder
(or Jellyfin/Emby reject the targeted call), it falls back to a full library refresh. Refreshes are
debounced per server (5 s) and de-duplicated by folder.

If the media server sees your files under a different path, add a **path mapping on the server
entry** (Schema path → server path), e.g. `/storage/media` → `/data/media`, or `/storage/media` →
`D:\Media` for a Windows Plex server.

## 9. Backups

Back up `/config`. What's in it:

| File / folder | Back up? | Notes |
|---|---|---|
| `schema.db`, `schema.db-wal`, `schema.db-shm` | **yes** | settings, formats, jobs, operation journal (undo) |
| `secret.key` | **yes** | without it, everyone is logged out (no data loss) |
| `cache.sqlite3` | optional | provider cache; rebuilt automatically |
| `trash/` | your call | replaced files; see §3 |
| `demo/` | no | demo mode only |

SQLite in WAL mode needs a consistent copy. Either stop the container briefly:

```bash
docker stop schema && tar czf schema-config-$(date +%F).tgz -C /mnt/user/appdata schema && docker start schema
```

or take a live online backup with the SQLite CLI on the host:

```bash
sqlite3 /mnt/user/appdata/schema/schema.db ".backup '/mnt/user/backups/schema-$(date +%F).db'"
```

Restore by stopping the container, putting the files back into `/config`, and starting it again.

## 10. Upgrading

```bash
cd schema
git pull
docker compose build --pull
docker compose up -d
docker compose logs -f schema
```

* Back up `/config` first (§9). The database schema is versioned and upgraded on start.
* To roll back, tag the previous image before building (`docker tag schema-media:latest
  schema-media:previous`), then run that tag with the `/config` backup from before the upgrade.
* Health: `docker inspect --format '{{.State.Health.Status}}' schema` should become `healthy`
  within about 30 seconds.
* **Stop timeout.** Docker gives a container 10 s to exit before killing it. The compose files set
  `stop_grace_period: 2m` (Unraid: `--stop-timeout 120`) so a running copy can finish. Each file is
  copied to a temporary name and renamed into place, so a kill never leaves a half file under the
  final name, but the interrupted item has to be redone.
* **Watchtower** ignores the container's grace period and uses its own `--stop-timeout` (default
  10 s). Run it with `--stop-timeout 120s` (or `WATCHTOWER_TIMEOUT=120s`), otherwise every
  automatic update can cut a job off mid-copy.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Refused: ... is outside the allowed roots` | Add the folder under Settings → Allowed roots. The path must be inside after symlinks are resolved. |
| `Permission denied` on move | The `PUID` user needs write access to the source folder (to remove the original) and the destination. Check `PUID`/`PGID`/`SUP_GROUP_IDS`. |
| `Can't hardlink across filesystems` | Source and destination are on different disks/shares or in different mounts. Use copy/move, or mount the common parent once (§3). |
| Every move is slow (copies instead of renaming) | Downloads and library are separate mounts, even if on one disk. Mount their common parent once (§3). |
| `/config is not writable by uid ...` (exit 73) | Non-root mode: `chown -R <uid>:<gid>` the host folder mounted at `/config`. Root mode: the folder can't be chowned (NFS root_squash, read-only); fix ownership on the host. |
| `refusing to run Schema as root` / `... root group` (exit 64) | `PUID`, `PGID`, `SUP_GROUP_IDS` or the `user:`/`group_add` ids contain 0. Use the ids that own your media; `SCHEMA_ALLOW_ROOT=1` overrides. |
| `UMASK must be an octal mask` (exit 64) | Use 1-4 octal digits, e.g. `022` or `002`. |
| `could not set up the container user` (exit 71) | Root mode with `read_only: true`: the entrypoint can't edit `/etc/passwd`. Use non-root mode (§3) or drop `read_only`. |
| `Invalid host header` (HTTP 400) | Add the name or IP in the address bar to `SCHEMA_ALLOWED_HOSTS`. |
| Watch folder shows *missing* | The path doesn't exist inside the container (mount it, check spelling). It recovers automatically. |
| Media server test says *rejected the token/API key* | Paste a fresh token/key; tokens are never shown in logs. |
| Unhealthy container | `docker logs schema`; `docker exec schema python /usr/local/bin/schema-healthcheck`. |

## 12. Development

```powershell
.\scripts\dev.ps1            # Windows: backend (demo mode) on :8420 + Vite on :5173
```

```bash
scripts/dev.sh               # Linux/macOS/Git Bash
```

Both start `python -m schema_app` from `backend/.venv` with `SCHEMA_DEMO=1` and run `npm run dev`
in `frontend/` (which proxies `/api` and the WebSocket to the backend). Ctrl+C stops both. Pass
`-NoDemo` / `--no-demo` to use `backend/data` without fixtures.

CI (`.github/workflows/ci.yml`) runs ruff and pytest on Linux and Windows, type-checks, lints, tests
and builds the frontend, then builds the Docker image (no push) and smoke-tests it in demo mode.
