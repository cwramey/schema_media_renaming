#!/bin/sh
# Tell Schema to import one or more paths (generic webhook). POSIX sh; uses curl, else GNU wget.
#
#   schema-notify.sh [-k movie|series|anime] [-c CATEGORY] [-l LABEL] PATH [PATH...]
#
# Environment:
#   SCHEMA_URL           default http://schema:8420
#   SCHEMA_API_KEY       the key (Schema -> Settings -> Download clients), or
#   SCHEMA_API_KEY_FILE  a file holding it ("KEY" or "X-Api-Key: KEY"); default: schema-api-key
#                        next to this script
#
# The key never goes on a command line (where ps and /proc/*/cmdline show it): curl reads it from
# stdin, GNU wget from a private temporary wgetrc. Redirects are not followed.
# Paths are sent as Schema-side paths after Settings -> Download clients -> path mappings apply.
# Exit status: 0 queued, 1 refused or unreachable, 2 usage error.

set -u

SCHEMA_URL=${SCHEMA_URL:-http://schema:8420}
SCHEMA_API_KEY=${SCHEMA_API_KEY:-}
SCHEMA_API_KEY_FILE=${SCHEMA_API_KEY_FILE:-}
kind=
category=
label=

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

fail() {
    echo "schema-notify: $*" >&2
    exit 1
}

while getopts "k:c:l:h" opt; do
    case $opt in
        k) kind=$OPTARG ;;
        c) category=$OPTARG ;;
        l) label=$OPTARG ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))
[ $# -ge 1 ] || usage

key=$SCHEMA_API_KEY
if [ -z "$key" ]; then
    file=$SCHEMA_API_KEY_FILE
    [ -n "$file" ] || file="$(dirname "$0")/schema-api-key"
    if [ -r "$file" ]; then
        IFS= read -r key < "$file" || [ -n "$key" ]
        key=$(printf '%s' "$key" | tr -d '\r')
        key=${key#[Xx]-[Aa]pi-[Kk]ey:}
        key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
    elif [ -n "$SCHEMA_API_KEY_FILE" ]; then
        fail "cannot read SCHEMA_API_KEY_FILE ($SCHEMA_API_KEY_FILE)"
    fi
fi
[ -n "$key" ] || fail "set SCHEMA_API_KEY or SCHEMA_API_KEY_FILE (Schema -> Settings -> Download clients)"
case $key in
    *[!A-Za-z0-9._~+/=_-]*) fail "the API key contains unexpected characters; copy it again from Schema" ;;
esac

# One path per line; the generic webhook splits the "paths" field on newlines.
nl='
'
cr=$(printf '\r')
paths=
for p in "$@"; do
    case $p in
        *"$nl"*|*"$cr"*) fail "a path contains a line break: $p" ;;
    esac
    if [ -z "$paths" ]; then paths=$p; else paths=$(printf '%s\n%s' "$paths" "$p"); fi
done

urlencode() {
    # Percent-encode every byte: always valid form encoding, safe for UTF-8 names and newlines.
    printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n' | sed 's/../%&/g'
}

endpoint="${SCHEMA_URL%/}/api/webhooks/generic"

if command -v curl >/dev/null 2>&1; then
    set -- -fsS --proto =http,https -X POST "$endpoint" --data-urlencode "paths=$paths"
    [ -n "$kind" ] && set -- "$@" --data-urlencode "kind=$kind"
    [ -n "$category" ] && set -- "$@" --data-urlencode "category=$category"
    [ -n "$label" ] && set -- "$@" --data-urlencode "label=$label"
    # "--config -": the header comes in on stdin, not on the command line.
    if response=$(printf 'header = "X-Api-Key: %s"\n' "$key" | curl --config - "$@" 2>&1); then
        echo "schema-notify: queued: $response"
        exit 0
    fi
elif command -v wget >/dev/null 2>&1 && wget --version 2>/dev/null | head -n 1 | grep -q 'GNU Wget'; then
    body="paths=$(urlencode "$paths")"
    [ -n "$kind" ] && body="$body&kind=$(urlencode "$kind")"
    [ -n "$category" ] && body="$body&category=$(urlencode "$category")"
    [ -n "$label" ] && body="$body&label=$(urlencode "$label")"
    rc=$(umask 077 && mktemp "${TMPDIR:-/tmp}/schema-notify.XXXXXX") || fail "cannot create a temporary file"
    trap 'rm -f "$rc"' EXIT
    trap 'exit 1' HUP INT TERM
    printf 'header = X-Api-Key: %s\nmax_redirect = 0\n' "$key" > "$rc"
    if response=$(WGETRC=$rc wget -qO- --header="Content-Type: application/x-www-form-urlencoded" \
        --post-data="$body" "$endpoint" 2>&1); then
        echo "schema-notify: queued: $response"
        exit 0
    fi
else
    fail "needs curl or GNU wget (BusyBox wget can only send the key on its command line)"
fi

fail "Schema refused or was unreachable at $endpoint: $response"
