#!/usr/bin/env python3
"""Container HEALTHCHECK: GET /api/health on the local Schema process. Exit 0 healthy, 1 not.

Healthy means exactly one thing: an HTTP 2xx answer whose JSON body has ``"status": "ok"``. Every
other field (version, commit, the result of a database check ...) may change or be hidden from
anonymous requests, so none of them is relied on; a missing or different ``status`` is unhealthy.
The request goes to the loopback address, which ``SCHEMA_ALLOWED_HOSTS`` always permits.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def health_url() -> str:
    host = os.environ.get("SCHEMA_HOST", "0.0.0.0").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = os.environ.get("SCHEMA_PORT", "8420").strip() or "8420"
    return f"http://{host}:{port}/api/health"


def main() -> int:
    try:
        with urllib.request.urlopen(health_url(), timeout=4) as response:
            body = json.loads(response.read().decode("utf-8") or "null")
    except Exception as exc:  # connection refused, timeout, HTTP 4xx/5xx, not JSON ...
        print(f"unhealthy: {exc}")
        return 1
    status = body.get("status") if isinstance(body, dict) else None
    if not isinstance(status, str) or status.strip().lower() != "ok":
        print(f"unhealthy: status {status!r}")
        return 1
    print("health: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
