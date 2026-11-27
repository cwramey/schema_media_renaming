"""Settings backup and restore (``GET /api/settings/backup``, ``POST /api/settings/restore``).

A backup holds the app settings, provider settings, watch folders and the user's own formats
(builtin presets are re-seeded by every install). Secrets are ``SECRET_MASK`` unless the backup was
made with ``include_secrets``; restoring a masked secret keeps the value already stored.
Everything is validated before anything is written, so a bad file changes nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update

from schema_app.api_models import (
    SECRET_MASK,
    AppSettings,
    BackupFormat,
    BackupProviders,
    ProvidersIn,
    SettingsBackup,
    WatchFolderIn,
)
from schema_app.core.format import FormatSyntaxError, compile_format
from schema_app.db import Database, utcnow
from schema_app.models import Format, WatchFolder
from schema_app.services import paths as paths_mod
from schema_app.services.jobs import ApiError
from schema_app.services.settings_store import (
    CREDENTIAL_FIELDS,
    SECRET_CREDENTIALS,
    SettingsError,
    SettingsStore,
    same_server_url,
    validate_roots,
)


def build_backup(db: Database, store: SettingsStore, *, include_secrets: bool) -> SettingsBackup:
    app = store.get_app() if include_secrets else store.get_app_masked()
    app = app.model_copy(update={"webhook_api_key": store.get_api_key() if include_secrets else SECRET_MASK})
    stored = store.stored_providers()
    credentials: dict[str, str | int | None] = {}
    for name, value in stored.credentials.model_dump().items():
        if name in SECRET_CREDENTIALS and not include_secrets:
            credentials[name] = SECRET_MASK if value else None
        else:
            credentials[name] = value
    with db.session() as session:
        folders = [
            WatchFolderIn(path=row.path, enabled=row.enabled, recursive=row.recursive, kind=row.kind or None,  # type: ignore[arg-type]
                          auto_execute=row.auto_execute, settle_seconds=row.settle_seconds, format_id=row.format_id,
                          dest_root=row.dest_root, mode=row.mode or None)  # type: ignore[arg-type]  # fmt: skip
            for row in session.scalars(select(WatchFolder).order_by(WatchFolder.id))
        ]
        formats = [
            BackupFormat(id=row.id, name=row.name, kind=row.kind, expression=row.expression,  # type: ignore[arg-type]
                         is_default=row.is_default)  # fmt: skip
            for row in session.scalars(select(Format).where(Format.builtin.is_(False)).order_by(Format.id))
        ]
    return SettingsBackup(
        created_at=utcnow(),
        app=app,
        providers=BackupProviders(priority=stored.priority, disabled=stored.disabled, language=stored.language,
                                  credentials=credentials),  # fmt: skip
        watch_folders=folders,
        formats=formats,
    )


@dataclass
class RestorePlan:
    """A validated backup, ready to write."""

    app: AppSettings | None = None
    api_key: str | None = None
    providers: ProvidersIn | None = None
    formats: list[BackupFormat] = field(default_factory=list)
    folders: list[tuple[str, WatchFolderIn]] = field(default_factory=list)  # (normalized path, body)
    notes: list[str] = field(default_factory=list)  # things the user must redo (missing tokens)


def plan_restore(store: SettingsStore, backup: SettingsBackup) -> RestorePlan:
    """Validate ``backup`` against this install (raises ``ApiError(400)``); writes nothing."""
    plan = RestorePlan()
    for fmt in backup.formats:
        try:
            compile_format(fmt.expression)
        except FormatSyntaxError as exc:
            raise ApiError(400, f"format {fmt.name!r}: {exc.message} (at {exc.start})") from exc
        plan.formats.append(fmt)
    if backup.app is not None:
        stored = {server.id: server for server in store.get_app().media_servers}
        servers = []
        for server in backup.app.media_servers:
            if server.token == SECRET_MASK:
                kept = stored.get(server.id)
                if kept is None or not same_server_url(kept.url, server.url):
                    # The backup didn't include it and this install doesn't know it: keep the server,
                    # without a token, and say so.
                    server = server.model_copy(update={"token": ""})
                    plan.notes.append(f"Re-enter the token for media server {server.name!r}")
            servers.append(server)
        plan.app = backup.app.model_copy(update={"media_servers": servers})
        key = backup.app.webhook_api_key
        if key and key != SECRET_MASK:
            if len(key.strip()) < 16:
                raise ApiError(400, "webhook API key is too short")
            plan.api_key = key
    if backup.providers is not None:
        creds = backup.providers.credentials
        if creds is not None:
            unknown = sorted(set(creds) - set(CREDENTIAL_FIELDS))
            if unknown:
                raise ApiError(400, f"unknown credential {unknown[0]!r}")
            version = creds.get("anidb_client_version")
            if version not in (None, "", SECRET_MASK):
                try:
                    int(version)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    raise ApiError(400, "anidb_client_version must be an integer") from exc
        plan.providers = ProvidersIn(
            priority=backup.providers.priority,
            disabled=backup.providers.disabled,
            language=backup.providers.language,
            credentials={k: None if v is None else str(v) for k, v in creds.items()} if creds is not None else None,
        )
        if plan.providers.language is not None and not plan.providers.language.strip():
            raise ApiError(400, "language must not be empty")
    if plan.app is not None:
        # The same root rules as saving settings, checked before anything is written: no "/", nothing
        # that is, holds or sits inside Schema's data folder.
        try:
            validate_roots(plan.app)
        except SettingsError as exc:
            raise ApiError(400, str(exc)) from exc
    roots = (plan.app or store.get_app()).allowed_roots
    for body in backup.watch_folders:
        path = body.path.strip()
        if not path or not os.path.isabs(path):
            raise ApiError(400, f"watch folder path must be absolute: {body.path!r}")
        path = os.path.normpath(path)
        if problem := paths_mod.root_problem(path):
            raise ApiError(400, f"watch folder {path} can't be used: {problem}")
        if not paths_mod.is_within(path, roots):
            raise ApiError(400, f"watch folder is outside the allowed roots: {path}")
        if body.dest_root and (
            not os.path.isabs(body.dest_root)
            or paths_mod.root_problem(body.dest_root)
            or not paths_mod.is_within(body.dest_root, roots)
        ):
            raise ApiError(400, f"watch folder destination is not an allowed absolute path: {body.dest_root}")
        plan.folders.append((path, body))
    return plan


def write_restore(db: Database, store: SettingsStore, plan: RestorePlan) -> dict[str, Any]:
    """Blocking: apply a validated plan. Returns the counts for ``RestoreOut``."""
    if plan.app is not None:
        try:
            store.put_app(plan.app)
        except SettingsError as exc:
            raise ApiError(400, str(exc)) from exc
        if plan.api_key:
            store.set_api_key(plan.api_key)
    if plan.providers is not None:
        try:
            store.put_providers(plan.providers)
        except SettingsError as exc:
            raise ApiError(400, str(exc)) from exc
    format_ids: dict[int, int] = {}
    with db.session() as session:
        for fmt in plan.formats:
            row = session.scalar(
                select(Format).where(Format.builtin.is_(False), Format.name == fmt.name.strip(),
                                     Format.kind == fmt.kind.value).order_by(Format.id)  # fmt: skip
            )
            if row is None:
                row = Format(name=fmt.name.strip(), kind=fmt.kind.value, expression=fmt.expression)
                session.add(row)
            row.expression = fmt.expression
            row.is_default = fmt.is_default
            row.updated_at = utcnow()
            session.flush()
            if fmt.is_default:
                session.execute(update(Format).where(Format.kind == row.kind, Format.is_default.is_(True),
                                                     Format.id != row.id).values(is_default=False))  # fmt: skip
            if fmt.id is not None:
                format_ids[fmt.id] = row.id
        existing = {os.path.normcase(os.path.normpath(r.path)): r for r in session.scalars(select(WatchFolder))}
        for path, body in plan.folders:
            format_id = body.format_id
            if format_id is not None:
                format_id = format_ids.get(format_id, format_id if session.get(Format, format_id) else None)
            row = existing.get(os.path.normcase(path))
            if row is None:
                row = WatchFolder(path=path)
                session.add(row)
                existing[os.path.normcase(path)] = row
            row.enabled = body.enabled
            row.recursive = body.recursive
            row.kind = body.kind.value if body.kind else None
            row.auto_execute = body.auto_execute
            row.settle_seconds = body.settle_seconds  # restored as saved; never silently rewritten
            row.format_id = format_id
            row.dest_root = body.dest_root
            row.mode = body.mode.value if body.mode else None
    return {"app": plan.app is not None, "providers": plan.providers is not None,
            "watch_folders": len(plan.folders), "formats": len(plan.formats)}  # fmt: skip
