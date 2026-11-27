#!/usr/bin/env python3
"""SABnzbd post-processing script: tell Schema that a download finished.

Install
-------
1. Copy this file into SABnzbd's script folder (Config -> Folders -> Scripts Folder) and make it
   executable on Linux: ``chmod +x schema_notify.py``.
2. Give the SABnzbd container (or service) these environment variables:
   ``SCHEMA_URL`` (default ``http://schema:8420``) and ``SCHEMA_API_KEY`` - or
   ``SCHEMA_API_KEY_FILE``, the path of a file that holds the key. The key is in Schema under
   Settings -> Download clients. It is never read from this file, so the script can be shared.
3. Pick ``schema_notify.py`` as the script for your categories (Config -> Categories), or as the
   default script.

SABnzbd passes the job through ``SAB_*`` environment variables. Failed downloads (``SAB_PP_STATUS``
other than 0) are not sent. The script prints one status line (SABnzbd shows it in the History)
and exits 0 on success, 1 on failure. Redirects are not followed (they would carry the key to
another host). Only the Python standard library is used.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

# Default only; set SCHEMA_URL in the environment instead of editing this line.
SCHEMA_URL = "http://schema:8420"

TIMEOUT_SECONDS = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Treat a redirect as an error: urllib would resend X-Api-Key to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def read_api_key(env: Mapping[str, str]) -> str:
    """The key from SCHEMA_API_KEY, else from the file named by SCHEMA_API_KEY_FILE ("KEY" or
    "X-Api-Key: KEY"). Empty when neither is set or the file is unreadable."""
    key = (env.get("SCHEMA_API_KEY") or "").strip()
    path = (env.get("SCHEMA_API_KEY_FILE") or "").strip()
    if key or not path:
        return key
    try:
        with open(path, encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return ""
    if line.lower().startswith("x-api-key:"):
        line = line.split(":", 1)[1].strip()
    return line


def build_payload(env: Mapping[str, str], argv: Sequence[str]) -> dict[str, str]:
    """Collect the job details from SAB_* variables, falling back to the positional arguments
    (1 = final folder, 3 = clean job name, 5 = category, 7 = post-processing status)."""

    def arg(index: int) -> str:
        return argv[index] if len(argv) > index else ""

    return {
        "complete_dir": env.get("SAB_COMPLETE_DIR") or arg(1),
        "name": env.get("SAB_FINAL_NAME") or arg(3),
        "category": env.get("SAB_CAT") or arg(5),
        "status": env.get("SAB_PP_STATUS") or arg(7) or "0",
        "nzo_id": env.get("SAB_NZO_ID", ""),
        "fail_msg": env.get("SAB_FAIL_MSG", ""),
    }


def main(
    env: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    opener: Callable[..., Any] | None = None,
) -> int:
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    opener = _OPENER.open if opener is None else opener
    payload = build_payload(env, argv)
    base_url = (env.get("SCHEMA_URL") or SCHEMA_URL).rstrip("/")
    api_key = read_api_key(env)

    if payload["status"].strip() not in ("0", ""):
        reason = payload["fail_msg"] or f"post-processing status {payload['status']}"
        print(f"Schema: not sent, the download failed ({reason})")
        return 0
    if not payload["complete_dir"]:
        print("Schema: no download folder in SAB_COMPLETE_DIR; is this running as a SABnzbd script?")
        return 1
    if not api_key:
        print(
            "Schema: set SCHEMA_API_KEY or SCHEMA_API_KEY_FILE in SABnzbd's environment "
            "(the key is in Schema -> Settings -> Download clients)"
        )
        return 1

    request = urllib.request.Request(
        f"{base_url}/api/webhooks/sabnzbd",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "User-Agent": "schema-sabnzbd-hook",
        },
        method="POST",
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        hint = " (check SCHEMA_API_KEY)" if exc.code in (401, 403) else ""
        if 300 <= exc.code < 400:
            hint = " (a redirect; not followed - point SCHEMA_URL at Schema itself)"
        print(f"Schema: HTTP {exc.code}{hint}{': ' + str(detail) if detail else ''}")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"Schema: could not reach {base_url} ({reason})")
        return 1

    job = body.get("job_id", "?")
    count = len(body.get("paths") or [payload["complete_dir"]])
    print(f"Schema: queued job {job} ({count} path{'s' if count != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
