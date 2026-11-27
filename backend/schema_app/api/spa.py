"""Serve the built SPA from ``SCHEMA_STATIC_DIR`` with history-API fallback (SPEC §5)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response

log = logging.getLogger("schema.spa")


def mount_spa(app: FastAPI, static_dir: Path) -> bool:
    """Register a catch-all GET/HEAD route (after the API routes). Returns False if there's no build."""
    root = Path(static_dir).resolve()
    index = root / "index.html"
    if not index.is_file():
        log.info("SPA not found at %s; serving the API only", root)
        return False

    # HEAD too: uptime monitors and reverse proxies probe "/" with it.
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(full_path: str, request: Request) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "not found")
        if full_path.endswith(".map"):
            raise HTTPException(404, "not found")  # source maps stay private even if a build has them
        if full_path:
            candidate = (root / full_path).resolve()
            inside = os.path.commonpath([str(candidate), str(root)]) == str(root)
            if inside and candidate.is_file():
                headers = {}
                if full_path.startswith("assets/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return FileResponse(candidate, headers=headers)
            if "." in full_path.rsplit("/", 1)[-1] and not full_path.endswith(".html"):
                raise HTTPException(404, "not found")  # a missing asset, not an app route
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return True
