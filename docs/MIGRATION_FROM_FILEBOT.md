# Migrating from FileBot (jlesage/filebot) to Schema

This guide moves a setup built on the `jlesage/filebot` container (FileBot's Swing desktop over noVNC
on `:5800`, plus the AMC script watching a folder) to Schema on `:8420`. Both run side by side
during a shadow week, so nothing is switched off until Schema has proven it makes the same
decisions.

Where this guide talks about the jlesage container's own wrapper or environment variables, that's
general knowledge. **Check your own compose file or Unraid template for the exact variable names
you used.**

---

## 1. What changes, at a glance

| | FileBot (jlesage container) | Schema |
|---|---|---|
| UI | Java Swing desktop streamed over noVNC (`:5800`) | Web app on `:8420`, works on a phone |
| Automation | AMC script on a watch folder; AMC is a signed bundle downloaded from `api.filebot.net` | Built-in watch folders, SABnzbd/qBittorrent webhooks, media-server refresh; no remote code |
| Preview | AMC renames immediately (fire and forget) | Every job, automated ones too, shows *original → parsed → proposed path*; low-confidence items wait in **Review** |
| License | Required for every real rename, and even for `--action test` in a headless container; verified online | None. Nothing to renew, nothing that can fail at 3 a.m. |
| Formats | Groovy scripts with full JVM, file and network access | FileBot-compatible expression subset in a sandbox (no file or network access), with live validation |
| History | `history.xml` in the app folder | Operation journal in SQLite with per-operation **undo** from the UI |
| Media servers | Plex/Jellyfin/Emby/Kodi refresh lives in the AMC script | Built-in Plex/Jellyfin/Emby refresh of only the touched folders (Kodi: planned) |
| Data folder | FileBot's app folder (license, history, cache) | `/config`: `schema.db`, cache, trash, `secret.key` |

## 2. Volume mapping

Schema uses the same container paths as jlesage/filebot, so you can reuse the host folders for
media.

| Container path | jlesage/filebot | Schema | Reuse the host folder? |
|---|---|---|---|
| `/config` | FileBot's settings, license file, `history.xml`, cache | `schema.db`, `cache.sqlite3`, `trash/`, `secret.key` | **No.** Give Schema its own folder (e.g. `/mnt/user/appdata/schema`). Keep FileBot's folder as a backup. |
| `/storage` | Media visible in the FileBot UI | Media Schema may read/write (add it to **Allowed roots**) | Yes, same host path |
| `/watch` | AMC input folder | Optional watch folder | Yes, same host path |
| `/output` | AMC output folder (library) | Optional library root | Yes, same host path |
| Port | `5800` (web VNC), `5900` (VNC) | `8420` (web UI, API, webhooks) | Different ports, so both can run at once |

User and permissions carry over:

| jlesage | Schema |
|---|---|
| `USER_ID` / `GROUP_ID` | `PUID` / `PGID`, **or keep `USER_ID` / `GROUP_ID`**: Schema reads them when `PUID`/`PGID` aren't set |
| `UMASK` | `UMASK` (same meaning) |
| `TZ` | `TZ` |
| supplementary groups | `SUP_GROUP_IDS` (comma separated) |

Schema chowns only `/config` (and only entries that need it); `/storage`, `/watch` and `/output`
keep their ownership.

## 3. Running side by side (shadow mode)

1. **Start Schema next to FileBot** with the same `/storage`, `/watch` and `/output` mounts (see
   `docker-compose.yml` or `deploy/unraid/schema.xml`). For a hard guarantee during the shadow
   week, mount the media read-only: `-v /mnt/user:/storage:ro`.
2. In Schema, set **Settings → Default mode = test**. Test mode plans, matches, renders and
   validates every item but never touches a file.
3. Let Schema see the same new downloads as FileBot:
   * **Webhooks (recommended):** add the Schema hook to SABnzbd/qBittorrent (§6). They fire while
     the download is still in place, before AMC moves it away.
   * **Watch folder:** watching the same `/watch` as AMC works only if Schema sees files before AMC
     moves them. Schema waits for a file to be stable (default 30 s); AMC usually runs on its own
     interval. If files disappear before Schema reports them, lower the folder's settle time or
     lengthen AMC's polling interval (the variable name is in your jlesage compose).
4. Each day, compare: open the Schema jobs and check each item's **proposed path** against where
   FileBot actually put the file (your library, or FileBot's history). Fix differences with
   format tweaks, provider priority or per-item overrides, until a week of jobs matches.
5. Then cut over (§9).

## 4. AMC options → Schema settings

The AMC script isn't part of the analyzed artifacts, so this maps the commonly documented AMC
options. Look up the ones your setup passes (usually in the container's AMC custom options).

| AMC / FileBot CLI option | Schema equivalent |
|---|---|
| `--action move\|copy\|keeplink\|symlink\|hardlink\|test` | Settings → **Default mode** (same names), per job, per watch folder |
| `--action clone` / `duplicate` | Not in v1. Use `hardlink` (same filesystem) or `copy` |
| `--conflict skip` (FileBot's default) | Conflict policy **skip** (item goes to Review with `destination_exists`) |
| `--conflict replace` / `override` | **overwrite**. The old file goes to the trash, as FileBot does |
| `--conflict auto` | **upgrade** (see §5.2 for the ranking) |
| `--conflict index` | **index** (`Name (1).mkv`, not FileBot's `Name.1.mkv`) |
| `--conflict fail` | **fail** |
| `--output /output` | **Library roots** per kind (movie / series / anime) |
| `--def movieFormat=…` / `seriesFormat=…` / `animeFormat=…` | **Formats**: import each into the Format builder and set it as the default for its kind (§7) |
| `--def ut_dir=… ut_kind=… ut_title=… ut_label=…` (download client hand-off) | Webhooks: `/api/webhooks/sabnzbd`, `/qbittorrent`, `/generic`; category → kind map |
| `--db` / `--def seriesDB/animeDB/movieDB` | Settings → **Providers**: priority order per kind, with automatic fallback |
| `--order Airdate\|DVD\|Absolute` | Per item **episode order** override (aired / dvd / absolute); anime absolute → seasonal mapping is automatic |
| `--lang` | Settings → **Language** |
| `-non-strict` | Settings → **Minimum confidence** (lower it to auto-accept more; items below it wait in Review) |
| `--def excludeList=…` | Not needed. Schema remembers what it reported and its journal shows what ran; files left over stay in Review |
| `--def ignore=<regex>` / `minFileSize` / `minLengthMS` | Watch folder **ignore patterns**; samples and extras are skipped by `skip_samples` / `skip_extras` |
| `--def clean=y` / `deleteAfterExtract` | Not in v1. Schema never deletes files |
| `--def unsorted=y` | Unmatched files stay in place and wait in **Review** |
| `--def plex=host:token` | Settings → **Media servers → Plex** (URL + token); refreshes only the touched folders |
| `--def jellyfin=…` / `emby=host:apikey` | Settings → **Media servers → Jellyfin / Emby** |
| `--def kodi=host` | Not in v1 (planned) |
| `--def artwork=y`, `--apply artwork\|nfo\|url\|cover` | Not in v1; let Plex/Jellyfin/Emby fetch artwork and metadata |
| `--def subtitles=…`, `--apply subtitles\|srt` | Not in v1 (planned). Existing subtitles travel with the video as sidecars |
| `--apply import` (companion files) | Sidecars (`.srt`, `.ass`, `.ssa`, `.sub`/`.idx`, `.sup`, `.vtt`, `.nfo`) travel with their video and keep suffixes such as `.en.forced` |
| `--apply chmod` | `UMASK` |
| `--apply prune` | Not in v1 (no deletions) |
| `--apply refresh` (Synology/QNAP indexers) | Not supported |
| `--exec …` | Not in v1; use the event log/WebSocket, or a webhook-driven tool |
| `--def pushover=…`, `mail=…`, `reportError` | Not in v1; failures show on the dashboard and in Activity |
| License file / license path | Not needed |

## 5. Behavioural differences worth knowing

### 5.1 Rename actions

| Action | FileBot | Schema |
|---|---|---|
| move | NIO move; cross-device becomes copy + delete inside the JDK | Same-device: atomic no-clobber rename. Cross-device: copy to `.<name>.schema-tmp`, fsync, verify size **and CRC32**, rename, then delete the source |
| copy | NIO copy (may reflink) | Temp file + fsync + size check + rename. Never leaves a half-written file under the final name |
| keeplink | move, then symlink at the original location | same, and the symlink is prepared before the move, so a filesystem without symlink support fails before anything moves |
| symlink | relative link when source and destination share a filesystem, else absolute | always an absolute link |
| hardlink | hard link | hard link; across filesystems it fails with a clear message (no silent copy) |
| test | computes destinations; needs a license headless | resolves the destination (allowed roots, conflicts, index names, sidecars), checks the folder is writable, and touches nothing |

Every operation is checked against **allowed roots** at execution time (symlinks resolved), and a
failed step (for example a subtitle that can't move) rolls back the steps before it.

### 5.2 Conflicts

* **Exact copies.** FileBot refuses to process a file when the destination has the same size, an
  mtime within 2 s and identical bytes. Schema treats a byte-identical destination (or a hardlink
  of the same file) as *already done* and leaves both untouched, whatever the policy. It compares
  sizes first and reads the content only when they match; it doesn't require matching mtimes.
* **replace → overwrite.** Both move the old file to a trash folder instead of deleting it.
  Schema's trash is `/config/trash/<date>/…` when that's on the same disk, otherwise
  `<library root>/.schema-trash/…` on the media's own disk (no 40 GB copies into app data). Undo
  restores it.
* **auto → upgrade.** FileBot ranks resolution, then codec (MPEG < AVC < HEVC < AV1), then
  REPACK/PROPER, then pixels × bitrate, then file size. Schema ranks by `QualityInfo.rank()`:
  resolution, then remux, then source (BluRay > WEB-DL > WEBRip > HDTV > DVD …), then HDR, Atmos,
  PROPER/REPACK and bit depth. On a tie the larger file wins, and the result says so. **The codec is
  not ranked**, so an x265 file doesn't beat an x264 file of the same resolution and source.
* **index.** FileBot: `Name.1.ext` … `Name.99.ext`. Schema: `Name (1).ext`, `Name (2).ext` … (Plex
  reads that as the same title; FileBot's style looks like a part number). Sidecars follow the
  indexed name.

### 5.3 Folder watching

FileBot's own folder watch is a single 2-second debounce with **no file-size stability or lock
check**, so a download that is still being written can be processed. Schema waits until a file's
size and mtime have been unchanged for the folder's settle time (default 30 s), skips partial files
(`*.part`, `*.!qB`, `*.crdownload`, `*.tmp` …), on Windows also waits until no other process has the
file open, and batches files that become ready together (a season pack is one job). A full rescan
every 60 s catches changes made over SMB/NFS that inotify can't see. A missing folder (unmounted
share) is reported and retried instead of failing.

### 5.4 History and undo

FileBot's `history.xml` isn't imported in v1 (planned). Schema's journal starts empty. Each
operation stores what's needed to reverse it; **undo** moves files back (or removes the copy or
link), restores anything that was replaced from the trash, and refuses if the file was changed
afterwards or its old location is occupied.

## 6. Download clients

Replace the AMC hand-off with Schema's webhooks (details in `DEPLOYMENT.md` §7):

* **SABnzbd:** copy `deploy/hooks/sabnzbd/schema_notify.py` into SABnzbd's scripts folder, set
  `SCHEMA_URL` and `SCHEMA_API_KEY`, and select it for your categories.
* **qBittorrent:** paste the `curl` command from `deploy/hooks/qbittorrent/README.md` into *Run
  external program on torrent finished*.
* Add **path mappings** if the client sees downloads under a different path than Schema.

## 7. Format compatibility

Paste FileBot formats into **Formats → Import**. The validator renders them against sample files
and flags anything it can't run, pointing at the exact characters, without rejecting the whole
format. Typical results:

| FileBot format feature | Example | In Schema |
|---|---|---|
| Literal text + `{…}` blocks | `{n} - {s00e00} - {t}` | Works as-is |
| Folders with `/` | `{ny}/{'Season '+s}/{n} - {s00e00} - {t}` | Works as-is, relative to the library root |
| Leading output folder / drive | `/output/TV Shows/{n}/…`, `{drive}/Media/{plex.id}` | Drop the leading folder and set it as the **library root** instead; absolute paths are rejected |
| Optional segments | `{' - ' + t}`, `{edition}` | Works: a block whose binding is missing renders empty |
| Groovy operators | `?:`, `? :`, `&&`, `\|\|`, `==`, `+` | Work |
| Fallbacks | `{any{t}{'Episode ' + e}}`, `{allOf{vf}{vc}.join(' ')}` | Work |
| String methods | `.pad(2)`, `.colon(' - ')`, `.upperInitial()`, `.lowerTrail()`, `.replaceAll(/[!?]/, '')`, `.sortName()`, `.ascii()`, `.truncate(n)`, `.replaceTrailingBrackets()` | Work (the catalog in the Format builder lists every supported method) |
| Regex literals and GStrings | `/[!?]+$/`, `"S${s.pad(2)}"` | Work |
| Lists | `{[vf, vc, ac].join(' ')}`, `{tags}` | Work |
| Media-server presets | `{plex}`, `{plex.id}`, `{jellyfin}`, `{emby}`, `{kodi}` | Work, **without** FileBot's top-level `Movies/` / `TV Shows/` folder (library roots replace it) |
| Quality and file bindings | `{vf} {vc} {ac} {af} {channels} {hdr} {group} {edition} {crc32} {fn} {ext}` | Work. Values come from the file name, or from `mediainfo` when probing is enabled |
| Movie `{t}` | FileBot: the tagline | Missing for movies (the block renders empty). Use `{n}` |
| Closures with parameters and Groovy collection methods | `{genres.collect{ it.lower() }}`, `.find{}`, `.each{}` | **Not supported**; rewrite with `.join()`, `.first()`, `any{}{}` |
| Statements and variables | `def x = …; …`, `if/else`, loops, `new …` | **Not supported**; use `cond ? a : b` |
| File, network and script access | `json('http…')`, `csv(…)`, `include(…)`, `OpenAI(…)`, `file.dir`, `{home}`, `{output}` | **Not supported** (sandbox). Reported as unsupported |
| Re-mapping bindings | `{order.dvd.s00e00}`, `{localize.de.n}`, `{db.TheTVDB.id}` | Not in v1; use the per-item episode-order override and provider priority |
| Batch context | `{di}`, `{dc}` | Supported; multi-version files also get an automatic ` - {vf}` suffix when they would collide |

Path safety is stricter than FileBot's: each value is cleaned of characters Windows/SMB can't
store (`:` becomes ` - `), Windows device names (`CON`, `NUL` …) get a trailing `_`, trailing dots
and spaces are trimmed, and each path component is limited to 255 bytes with the extension
preserved.

## 8. Things FileBot does that Schema (v1) doesn't

Artwork/NFO writing, subtitle downloads, xattr metadata tags, Kodi refresh, music and photo
renaming, `clone`/`duplicate` actions, `--exec` hooks, notifications, and deleting clutter or empty
folders. Keep FileBot's container around (stopped) if you rely on any of these, or let your media
server handle artwork and metadata.

## 9. Cut-over checklist

- [ ] Back up FileBot's `/config` folder (license, `history.xml`, settings).
- [ ] Start Schema with its **own** `/config` folder and the same `/storage` (and `/watch`,
      `/output`) mounts; `PUID`/`PGID` (or `USER_ID`/`GROUP_ID`) and `UMASK` as before.
- [ ] Create the admin account; set **Allowed roots** (`/storage`, `/watch`, `/output`).
- [ ] Set **Library roots** per kind from your AMC `--output` plus the format's top-level folder
      (e.g. `/output/Movies`, `/output/TV Shows`, `/output/Anime`).
- [ ] Enter provider keys; set provider priority to match the databases FileBot used (5.3.0
      defaults series *and* anime to TheMovieDB::TV).
- [ ] Import your movie/series/anime formats, fix what the validator flags, set them as defaults.
- [ ] Choose the conflict policy you used with AMC (`--conflict`).
- [ ] Add Plex/Jellyfin/Emby with path mappings, and press **Test**.
- [ ] Add the SABnzbd script / qBittorrent command and the path mappings; finish a test download.
- [ ] **Shadow week:** default mode `test`, media mounted read-only if you like; compare Schema's
      proposed paths with FileBot's results daily until they match.
- [ ] Switch **Default mode** to `move` (or `keeplink`/`hardlink`/`copy` for torrents that keep
      seeding), remount `/storage` read-write, enable auto-execute on watch folders.
- [ ] Stop the jlesage/filebot container (or remove its AMC watch) so two tools never move the
      same files. Keep it stopped, not deleted, for a while.
- [ ] Watch the dashboard and Activity for a few days; use **History → Undo** for anything wrong.
- [ ] When happy, remove the FileBot container and retire `:5800`. Keep the `/config` backup.

**Rolling back** is the reverse: switch Schema's default mode back to `test` (or stop the
container) and start the FileBot container again. Undo in Schema puts files it moved back where
they were.
