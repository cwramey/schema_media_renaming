#!/bin/sh
# Schema container entrypoint (runs under tini as PID 1). Two ways to run the container:
#
# * Non-root mode (recommended): compose `user: "PUID:PGID"` (docker run --user). The entrypoint
#   writes nothing, so the container also runs with `read_only: true`, a tmpfs on /tmp and
#   `cap_drop: [ALL]` with no cap_add. Supplementary groups come from compose `group_add`.
#   /config must already be writable by that user.
# * Root mode (default): the container starts as root only to create a "schema" user with
#   PUID/PGID (USER_ID/GROUP_ID are accepted for parity with jlesage images), add SUP_GROUP_IDS
#   (comma separated, e.g. a group that owns your media share), make /config owned by it and drop
#   privileges with gosu. Needs a writable root filesystem (/etc/passwd is edited) and the
#   capabilities CHOWN, DAC_OVERRIDE, FOWNER, SETGID and SETUID.
#
# Both modes: UMASK sets the permission mask for files and folders Schema creates (default 022).
# uid 0 and gid 0 (as PUID, PGID, a SUP_GROUP_IDS entry or the --user/group_add ids) are refused
# unless SCHEMA_ALLOW_ROOT=1. /storage, /watch and /output are never chowned: they hold your media
# and keep their ownership.
set -eu

DATA_DIR="${SCHEMA_DATA_DIR:-/config}"

fail() {  # fail <exit code> <message...>
    code=$1
    shift
    echo "schema: $*" >&2
    exit "$code"
}

root_allowed() {
    case "${SCHEMA_ALLOW_ROOT:-}" in
        1 | true | TRUE | yes | YES) return 0 ;;
    esac
    return 1
}

is_id() {  # a decimal user/group id
    case "$1" in
        '' | *[!0-9]*) return 1 ;;
    esac
    return 0
}

strip_zeros() {  # "0000" -> "0", "0099" -> "99" (so "00" can't slip past the root check)
    v=$1
    while [ "${#v}" -gt 1 ] && [ "${v#0}" != "$v" ]; do
        v=${v#0}
    done
    echo "$v"
}

# UMASK: 1-4 octal digits ("022", "0002"). Anything else would make umask fail or mean something else.
UMASK="${UMASK:-022}"
case "$UMASK" in
    '' | *[!0-7]*) fail 64 "UMASK must be an octal mask such as 022 or 002 (got '$UMASK')" ;;
esac
[ "${#UMASK}" -le 4 ] || fail 64 "UMASK must be an octal mask such as 022 or 002 (got '$UMASK')"
case "$UMASK" in
    *[0145]) echo "schema: warning: UMASK=$UMASK makes the files and folders Schema creates writable by everyone; 022 or 002 is safer" >&2 ;;
esac
umask "$UMASK"

# --------------------------------------------------------------------------- non-root mode
if [ "$(id -u)" != "0" ]; then
    if ! root_allowed; then
        for gid in $(id -G); do
            [ "$gid" != "0" ] || fail 64 "refusing to run with the root group (gid 0) in the container's groups ($(id -G)); pick another group in user: / group_add, or set SCHEMA_ALLOW_ROOT=1"
        done
    fi
    if [ -n "${PUID:-}" ] && [ "$(strip_zeros "$PUID")" != "$(id -u)" ]; then
        echo "schema: note: PUID=$PUID is ignored; the container already runs as uid $(id -u) (user:)" >&2
    fi
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    if [ ! -d "$DATA_DIR" ] || [ ! -w "$DATA_DIR" ]; then
        fail 73 "$DATA_DIR is not writable by uid $(id -u) gid $(id -g); give the host folder mounted at $DATA_DIR to that user (chown -R $(id -u):$(id -g) <folder>) or use root mode with PUID/PGID"
    fi
    export HOME="$DATA_DIR"
    exec "$@"
fi

# --------------------------------------------------------------------------- root mode
PUID="${PUID:-${USER_ID:-1000}}"
PGID="${PGID:-${GROUP_ID:-1000}}"
SUP_GROUP_IDS="${SUP_GROUP_IDS:-}"

if ! is_id "$PUID" || ! is_id "$PGID"; then
    fail 64 "PUID and PGID must be numbers (got PUID=$PUID PGID=$PGID)"
fi
PUID=$(strip_zeros "$PUID")
PGID=$(strip_zeros "$PGID")

# Validate every supplementary group before touching anything.
sup_gids=""
if [ -n "$SUP_GROUP_IDS" ]; then
    old_ifs=$IFS
    IFS=,
    for gid in $SUP_GROUP_IDS; do
        gid=$(echo "$gid" | tr -d ' \t')
        [ -n "$gid" ] || continue
        is_id "$gid" || fail 64 "SUP_GROUP_IDS must be group ids separated by commas (got '$SUP_GROUP_IDS')"
        gid=$(strip_zeros "$gid")
        if [ "$gid" = "0" ] && ! root_allowed; then
            fail 64 "refusing to add the root group (gid 0) from SUP_GROUP_IDS; use the group that owns your media, or set SCHEMA_ALLOW_ROOT=1"
        fi
        sup_gids="$sup_gids $gid"
    done
    IFS=$old_ifs
fi

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    if ! root_allowed; then
        fail 64 "refusing to run Schema as root (PUID=$PUID PGID=$PGID): it could then change any file on your mounted shares. Set PUID/PGID to the user that owns your media ('id <user>' on the host), or set SCHEMA_ALLOW_ROOT=1 to accept the risk"
    fi
    if [ "$PUID" = "0" ]; then
        echo "schema: warning: running as root (PUID=0, SCHEMA_ALLOW_ROOT=1); files Schema creates will belong to root" >&2
        mkdir -p "$DATA_DIR"
        export HOME="$DATA_DIR"
        exec "$@"
    fi
fi

setup() {  # run a user/group command; explain the usual cause when it fails
    "$@" || fail 71 "could not set up the container user ('$1' failed). With a read-only root filesystem (read_only: true / --read-only) use non-root mode instead: user: \"$PUID:$PGID\" (docs/DEPLOYMENT.md)"
}

# Group for PGID: reuse whatever group already has that id, otherwise create "schema".
group_name="$(getent group "$PGID" | cut -d: -f1 || true)"
if [ -z "$group_name" ]; then
    setup groupadd -o -g "$PGID" schema
    group_name=schema
fi

# User "schema" with PUID (created or adjusted on every start, so changing PUID just works).
if getent passwd schema >/dev/null 2>&1; then
    setup usermod -o -u "$PUID" -g "$PGID" schema >/dev/null
else
    setup useradd -o -u "$PUID" -g "$PGID" -d "$DATA_DIR" -M -s /usr/sbin/nologin schema
fi

for gid in $sup_gids; do
    name="$(getent group "$gid" | cut -d: -f1 || true)"
    if [ -z "$name" ]; then
        name="schema-$gid"
        setup groupadd -o -g "$gid" "$name"
    fi
    setup usermod -a -G "$name" schema
done

# /config: create it and fix ownership of anything the user can't own yet (cheap on restarts).
# This runs as root on a folder the unprivileged user could write to before, so it must never act
# on a symlink's target: find doesn't follow links (-P), symlinks are skipped, and chown -h would
# change a link itself rather than its target even if a file were swapped for one meanwhile.
mkdir -p "$DATA_DIR"
find "$DATA_DIR" -xdev ! -type l \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} + 2>/dev/null || \
    echo "schema: could not change ownership of everything in $DATA_DIR (read-only or NFS root_squash?)" >&2

if ! gosu schema test -w "$DATA_DIR"; then
    fail 73 "$DATA_DIR is not writable by uid $PUID; check the volume's permissions"
fi

export HOME="$DATA_DIR"
exec gosu schema "$@"
