"""Application factory: ``create_app()`` wires config, DB, services, workers, routers and the SPA."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from schema_app import __version__, build_commit
from schema_app.api import build_api_router
from schema_app.api.formats import seed_formats
from schema_app.api.spa import mount_spa
from schema_app.auth import SessionSigner, user_count
from schema_app.config import Config, StartupError, ensure_data_dir, get_config, load_secret_key
from schema_app.core import matcher as matcher_mod
from schema_app.core.pipeline import Pipeline, UnavailableMatcher, WatchManager
from schema_app.db import Database
from schema_app.deps import AppContext
from schema_app.http_guard import RequestGuard, build_csp, inline_hashes
from schema_app.services.events import EventBus
from schema_app.services.jobs import ApiError
from schema_app.services.settings_store import SettingsError, SettingsStore

log = logging.getLogger("schema")

# The healthcheck and local tools always reach the server by loopback address.
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"]
_APIKEY_RE = re.compile(r"(?i)([?&]apikey=)[^&\s\"]*")


class _RedactApiKey(logging.Filter):
    """Download-client hooks may pass the webhook key as ?apikey=: keep it out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_APIKEY_RE.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


def _install_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactApiKey) for f in access.filters):
        access.addFilter(_RedactApiKey())


def _prepare_setup_token(ctx: AppContext) -> None:
    """No admin yet: require a one-time setup code, printed to the log (docker logs schema).
    Demo mode too: a demo instance with sign-in on is still reachable by everyone on the network."""
    config = ctx.config
    if not config.auth_enabled:
        return
    with ctx.db.session() as session:
        if user_count(session) > 0:
            return
    ctx.setup_token = config.setup_token or secrets.token_urlsafe(12)
    if config.setup_token:
        log.warning("No admin account yet: create it in the web UI with the code from SCHEMA_SETUP_TOKEN")
    else:
        log.warning("No admin account yet: create it in the web UI with setup code %s", ctx.setup_token)


def _configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger("schema")
    logger.setLevel(numeric)
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


async def startup(config: Config) -> AppContext:
    """Build every service. Split out of the lifespan so tests and tools can reuse it."""
    ensure_data_dir(config)
    _configure_logging(config.log_level)
    secret = load_secret_key(config)
    db = Database(config.db_path)
    await asyncio.to_thread(db.init)
    store = SettingsStore(db, config)
    await asyncio.to_thread(store.seed)
    bus = EventBus(db)
    bus.bind()
    await asyncio.to_thread(seed_formats, db, store)
    ctx = AppContext(config=config, db=db, store=store, bus=bus, signer=SessionSigner(secret))
    await asyncio.to_thread(_prepare_setup_token, ctx)

    if config.demo:
        from schema_app.demo import bootstrap_demo

        await bootstrap_demo(ctx)

    try:
        ctx.matcher = matcher_mod.create_matcher(store.get_providers(), cache_dir=config.data_dir, demo=config.demo)
    except Exception as exc:  # keep serving the UI even if metadata is broken
        log.exception("could not build the metadata matcher")
        ctx.matcher_error = str(exc) or exc.__class__.__name__
        ctx.matcher = UnavailableMatcher(ctx.matcher_error)
        bus.log("error", "provider", f"Metadata matcher unavailable: {ctx.matcher_error}")

    ctx.pipeline = Pipeline(config=config, db=db, store=store, bus=bus, matcher=ctx.matcher)
    await ctx.pipeline.start()
    ctx.watch = WatchManager(db=db, store=store, bus=bus, pipeline=ctx.pipeline)
    await ctx.watch.start()

    if config.demo:
        from schema_app.demo import seed_demo_job

        await seed_demo_job(ctx)

    commit = build_commit()
    label = f"Schema {__version__}" + (f" ({commit[:7]})" if commit else "")
    await bus.alog(
        "info", "system", f"{label} started" + (" (demo mode)" if config.demo else ""),
        auth="disabled" if not config.auth_enabled else "required",
    )
    if not config.auth_enabled:
        await bus.alog("warning", "system", "Authentication is disabled (SCHEMA_AUTH=disabled)")
    return ctx


async def shutdown(ctx: AppContext) -> None:
    if ctx.watch is not None:
        with contextlib.suppress(Exception):
            await ctx.watch.stop()
    if ctx.pipeline is not None:
        with contextlib.suppress(Exception):
            await ctx.pipeline.stop()
    if ctx.matcher is not None:
        with contextlib.suppress(Exception):
            await ctx.matcher.aclose()
    ctx.bus.unbind()
    ctx.db.dispose()


def create_app(settings_override: Config | None = None) -> FastAPI:
    config = settings_override or get_config()
    problem = config.startup_problem()
    if problem:
        log.critical("Refusing to start: %s", problem)
        raise StartupError(problem)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = await startup(config)
        app.state.ctx = ctx
        try:
            yield
        finally:
            await shutdown(ctx)
            app.state.ctx = None

    app = FastAPI(
        title="Schema",
        version=__version__,
        description="Web-native media organizer and renamer",
        lifespan=lifespan,
        docs_url="/api/docs" if config.api_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if config.api_docs else None,
    )
    # add_middleware wraps: the last one added sees the request first.
    # Level 6: nearly level 9's ratio on JSON and JS (322 KB bundle -> ~100 KB) at a fraction of the CPU.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
    script_hashes, style_hashes = inline_hashes(config.static_dir / "index.html")
    app.add_middleware(RequestGuard, state=app.state, csp=build_csp(script_hashes, style_hashes))
    if config.allowed_host_list:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*config.allowed_host_list, *LOOPBACK_HOSTS])
    _install_log_redaction()
    app.state.config = config
    app.state.ctx = None

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(SettingsError)
    async def _settings_error(_request: Request, exc: SettingsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.include_router(build_api_router())
    mount_spa(app, config.static_dir)
    return app
