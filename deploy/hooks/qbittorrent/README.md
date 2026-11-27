# qBittorrent → Schema

qBittorrent can run a command when a torrent finishes. Schema's webhook takes that call and starts a
job for the torrent's files.

## Setup

1. In Schema, open **Settings → Download clients** and copy the secret key.
2. Save the key in a file that qBittorrent can read, as one header line:

   ```
   X-Api-Key: YOUR_API_KEY
   ```

   * Docker: put it in the folder mounted as the container's `/config` and call it
     `schema-api-key`, so qBittorrent sees `/config/schema-api-key`. Make it private:
     `chmod 600 schema-api-key` (owned by the `PUID` the container runs as).
   * Windows: for example `C:\Users\you\AppData\Roaming\qBittorrent\schema-api-key.txt`.

3. In qBittorrent, open **Tools → Options → Downloads**, tick **Run external program → Run on torrent
   finished**, and paste the command line below. Replace `SCHEMA_HOST`.

### Linux / Docker (qBittorrent in a container that has `curl`)

```
curl -fsS -X POST -H @/config/schema-api-key --data-urlencode "content_path=%F" --data-urlencode "name=%N" --data-urlencode "category=%L" --data-urlencode "hash=%I" --data-urlencode "save_path=%D" "http://SCHEMA_HOST:8420/api/webhooks/qbittorrent"
```

### Windows (qBittorrent desktop; `curl.exe` ships with Windows 10 and later)

```
curl.exe -fsS -X POST -H "@C:\Users\you\AppData\Roaming\qBittorrent\schema-api-key.txt" --data-urlencode "content_path=%F" --data-urlencode "name=%N" --data-urlencode "category=%L" --data-urlencode "hash=%I" --data-urlencode "save_path=%D" "http://SCHEMA_HOST:8420/api/webhooks/qbittorrent"
```

`SCHEMA_HOST` is whatever qBittorrent can reach Schema at: the server IP (`192.168.1.20`), or the
service name (`schema`) when both containers share a Docker network.

`-H @file` makes curl read the header from the file (curl 7.55 or newer), so the key is not part of
the command line: it doesn't show up in `ps`, in `/proc/*/cmdline`, in qBittorrent's settings file or
in its **Execution Log**. Don't paste the key into the command itself.

## What the placeholders mean

| Placeholder | Sent as | Meaning |
|---|---|---|
| `%F` | `content_path` | The file (single-file torrent) or root folder (multi-file torrent). Schema imports this. |
| `%N` | `name` | Torrent name, used as the job label. |
| `%L` | `category` | Category, mapped to a media kind through **Settings → Download clients → Download categories** (`tv`, `sonarr`, `tv-sonarr` → series; `movies`, `radarr` → movie; `anime` → anime). |
| `%I` | `hash` | Info hash (logged only). |
| `%D` | `save_path` | Save path, used when `%F` is empty. |

`--data-urlencode` matters: torrent names with spaces, `&` or `#` would break a hand-built query
string.

## Paths seen by qBittorrent vs. Schema

qBittorrent reports paths as *it* sees them. If its container mounts downloads at `/downloads` and
Schema mounts the same folder at `/storage/downloads`, add a path mapping in **Settings → Download
clients → Download app sees a different path**: `/downloads` → `/storage/downloads`. Windows paths
work too: `D:\Torrents` → `/storage/torrents`.

A webhook job runs on its own, so Schema only accepts paths **inside the download area**: below a
watch folder or below the Schema side of a path mapping, and inside the allowed roots. It refuses an
allowed root or library root itself, any folder that contains one, and anything in the library. If
qBittorrent and Schema see the same path, add a watch folder for the download folder, or a mapping
with the same path on both sides (`/media/downloads` → `/media/downloads`).

## Seeding

Torrents that keep seeding must stay where qBittorrent expects them. Use **copy**, **hardlink**
(same filesystem only) or **keeplink** (move, then leave a symlink behind) as the job mode for this
watch/webhook source, not **move**.

## No curl in your qBittorrent image?

`wget` can't URL-encode the fields, so use the generic hook instead:
`deploy/hooks/generic/schema-notify.sh` (POSIX `sh`; uses `curl`, else GNU `wget`). Copy it into the
qBittorrent container's config folder next to the `schema-api-key` file and use
`/config/schema-notify.sh -c "%L" -l "%N" "%F"` with `SCHEMA_URL` set in the container environment.
The script finds `schema-api-key` next to itself (or set `SCHEMA_API_KEY_FILE`, or
`SCHEMA_API_KEY`) and never puts the key on a command line. BusyBox `wget` can't do that, so the
script refuses to run with it: install `curl`.

## Check it

Finish a small torrent and look at **Activity** in Schema (category `webhook`). Errors from the
command appear in qBittorrent's **Execution Log** (View → Log).
