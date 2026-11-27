"""``/api/fs/list``: server-side file browser confined to ``allowed_roots`` (SPEC §11).

Schema's own data folder (``/config``) is never listed, whatever the roots say. Entries are
``lstat``-ed: a symlink is shown (with its target's size and type) only when it resolves inside the
allowed roots, so the browser can't be used to learn about files elsewhere; sockets, FIFOs and
devices are hidden.
"""

from __future__ import annotations

import os
import stat as stat_mod
from datetime import UTC, datetime

from fastapi import APIRouter

from schema_app.api_models import FsEntry, FsListing
from schema_app.deps import CtxDep
from schema_app.services import paths as paths_mod
from schema_app.services.jobs import ApiError

router = APIRouter()

MAX_ENTRIES = 5000


def _entry_stat(entry: os.DirEntry[str], roots: list[str]) -> os.stat_result | None:
    """The stat to show for ``entry``: its own (lstat), or for a symlink its target's when the target
    is inside the allowed roots. None hides the entry."""
    st = entry.stat(follow_symlinks=False)
    if stat_mod.S_ISLNK(st.st_mode):
        if not paths_mod.is_within(entry.path, roots):
            return None
        st = os.stat(entry.path)
    if not (stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISREG(st.st_mode)):
        return None
    return st


@router.get("/fs/list", response_model=FsListing)
def fs_list(ctx: CtxDep, path: str | None = None) -> FsListing:
    settings = ctx.store.get_app()
    roots = list(settings.allowed_roots)
    videos = {ext.lower().lstrip(".") for ext in settings.video_extensions}
    if not path:
        if not roots:
            return FsListing(path="", parent=None, entries=[], roots=[])
        path = roots[0]  # the UI starts browsing at the first allowed root
        if not os.path.isdir(path):
            return FsListing(path=path, parent=None, entries=[], roots=roots)
    if not os.path.isabs(path):
        raise ApiError(400, "path must be absolute")
    target = os.path.normpath(path)
    if paths_mod.is_protected_path(target):
        raise ApiError(403, "Schema's own data folder can't be browsed")
    if not paths_mod.is_within(target, roots):
        raise ApiError(403, "path is outside the allowed roots")
    if not os.path.exists(target):
        raise ApiError(404, "path not found")
    if not os.path.isdir(target):
        raise ApiError(400, "path is not a directory")
    # Entries that are the data folder itself (listing its parent) are left out.
    real_target = os.path.realpath(target)
    hidden = set(paths_mod.protected_paths())
    entries: list[FsEntry] = []
    try:
        with os.scandir(target) as listing:
            for entry in listing:
                if entry.name.startswith("."):
                    continue
                if hidden and os.path.normcase(os.path.join(real_target, entry.name)) in hidden:
                    continue
                try:
                    st = _entry_stat(entry, roots)
                except OSError:
                    continue
                if st is None:
                    continue
                is_dir = stat_mod.S_ISDIR(st.st_mode)
                ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
                entries.append(
                    FsEntry(
                        name=entry.name,
                        path=os.path.join(target, entry.name),
                        is_dir=is_dir,
                        size=None if is_dir else st.st_size,
                        modified=datetime.fromtimestamp(st.st_mtime, tz=UTC),
                        is_video=(not is_dir) and ext in videos,
                    )
                )
                if len(entries) >= MAX_ENTRIES:
                    break
    except PermissionError as exc:
        raise ApiError(403, f"permission denied: {target}") from exc
    entries.sort(key=lambda e: (not e.is_dir, e.name.casefold()))
    parent_dir = os.path.dirname(target)
    parent = parent_dir if parent_dir != target and paths_mod.is_within(parent_dir, roots) else None
    return FsListing(path=target, parent=parent, entries=entries, roots=roots)
