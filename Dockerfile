# syntax=docker/dockerfile:1
#
# Schema: one image, one process. Uvicorn serves the API, the WebSocket and the built SPA on :8420.
#
#   docker build -t schema-media:latest .
#   docker run -d -p 8420:8420 -v /mnt/user/appdata/schema:/config -v /mnt/user/data:/storage schema-media:latest
#
# Stages: (1) node builds frontend/dist, (2) a throwaway python stage installs the backend into a
# virtualenv, (3) the slim runtime gets the venv, the SPA, mediainfo, tini and gosu.
#
# The image runs either as root that drops to PUID/PGID (default), or entirely as a non-root user
# with a read-only root filesystem (user: "PUID:PGID", read_only: true, tmpfs /tmp, cap_drop ALL).
# Nothing at runtime writes outside /config, the media mounts and /tmp. See docs/DEPLOYMENT.md §3.

ARG NODE_VERSION=22
ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------- 1. frontend
FROM node:${NODE_VERSION}-alpine AS frontend
WORKDIR /src/frontend
# The lockfile is required: npm ci installs exactly what it lists (and fails if it's out of date).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && test -f dist/index.html

# --------------------------------------------------------------------------- 2. backend
FROM python:${PYTHON_VERSION}-slim AS backend
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
WORKDIR /src/backend
# Dependencies first, in a layer that depends only on pyproject.toml, so a change to the backend's
# code reuses it instead of downloading every package again. The layer is rebuilt when
# pyproject.toml or the base image changes; bump PY_DEPS_REFRESH (e.g. --build-arg
# PY_DEPS_REFRESH=$(date +%F)) to pick up new dependency releases without either. The build backend
# (hatchling) goes into this stage's system Python, not into the venv that ships.
COPY backend/pyproject.toml ./
ARG PY_DEPS_REFRESH=""
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb')); open('/tmp/requirements.txt', 'w').write('\n'.join(p['project']['dependencies'])); open('/tmp/build-requirements.txt', 'w').write('\n'.join(p['build-system']['requires']))" \
 && /opt/venv/bin/pip install -r /tmp/requirements.txt \
 && pip install -r /tmp/build-requirements.txt
# Then the package itself, without touching the dependencies installed above.
COPY backend/schema_app ./schema_app
RUN pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/wheels . \
 && /opt/venv/bin/pip install --no-deps /tmp/wheels/*.whl \
 && /opt/venv/bin/pip check \
 && /opt/venv/bin/python -c "import schema_app; print('schema_app', schema_app.__version__)"

# --------------------------------------------------------------------------- 3. runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="Schema" \
      org.opencontainers.image.description="Web-native media organizer and renamer (FileBot replacement)" \
      org.opencontainers.image.licenses="Proprietary"

RUN apt-get update \
 && apt-get install -y --no-install-recommends mediainfo tini gosu tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && gosu nobody true

COPY --from=backend /opt/venv /opt/venv
COPY --from=frontend /src/frontend/dist /app/web
COPY deploy/docker/entrypoint.sh /usr/local/bin/schema-entrypoint
COPY deploy/docker/healthcheck.py /usr/local/bin/schema-healthcheck
# The download-client hooks ship inside the image so they can be copied out: docker cp schema:/app/hooks .
COPY deploy/hooks /app/hooks
RUN chmod 0755 /usr/local/bin/schema-entrypoint /usr/local/bin/schema-healthcheck /app/hooks/sabnzbd/schema_notify.py \
               /app/hooks/generic/schema-notify.sh \
 && sed -i 's/\r$//' /usr/local/bin/schema-entrypoint /app/hooks/generic/schema-notify.sh /app/hooks/sabnzbd/schema_notify.py \
 && mkdir -p /config /storage /watch /output

# PYTHONDONTWRITEBYTECODE keeps Python from writing .pyc files next to the code, which a read-only
# root filesystem would refuse anyway (the venv's own .pyc files are compiled at install time).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    SCHEMA_DATA_DIR=/config \
    SCHEMA_STATIC_DIR=/app/web \
    SCHEMA_HOST=0.0.0.0 \
    SCHEMA_PORT=8420 \
    UMASK=022 \
    TZ=UTC
# PUID/PGID are deliberately not set here: the entrypoint defaults them to 1000 and honours the
# jlesage-style USER_ID/GROUP_ID when PUID/PGID are absent.

VOLUME ["/config"]
EXPOSE 8420

# The git commit the image was built from (CI passes --build-arg SCHEMA_COMMIT=<sha>); shown in
# Settings → About and returned by /api/health. Declared last so it never busts the layer cache.
ARG SCHEMA_COMMIT=""
ENV SCHEMA_COMMIT=${SCHEMA_COMMIT}
LABEL org.opencontainers.image.revision=${SCHEMA_COMMIT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "/usr/local/bin/schema-healthcheck"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/schema-entrypoint"]
CMD ["python", "-m", "schema_app"]
