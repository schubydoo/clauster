# syntax=docker/dockerfile:1
#
# Multi-arch (linux/amd64, linux/arm64) image for clauster, per spec §"Docker
# image": python:3.14-slim-trixie base, non-root, PUID/PGID, healthcheck, JSON logs.
#
# clauster spawns `claude remote-control` bridges, so the claude CLI is NOT
# baked in — provide it at runtime (mount it onto PATH, or build a derived image
# that installs it) along with ~/.claude credentials and your projects dir.

# ----- builder: resolve the locked deps into a self-contained venv -----------
FROM python:3.14-slim-trixie@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Deps first (cached unless the lockfile/manifest change), then the project.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ----- runtime ---------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS runtime

# apt upgrade pulls Debian security fixes published since the base image was
# built. NB: this layer is keyed on the FROM digest, so the CI build cache
# (cache-from/to: gha) only re-runs it when the base digest above changes —
# bumping that pin (Renovate, or manually) is what refreshes OS CVEs; a rebuild
# at the same digest reuses the cached layer. git: provisioning (create
# --git-init / clone). gosu + passwd: PUID/PGID remap + privilege-drop in entry.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git gosu passwd \
    && rm -rf /var/lib/apt/lists/*

# Default identity; remappable to the host's PUID/PGID at runtime.
RUN groupadd -g 1000 clauster \
    && useradd -u 1000 -g 1000 -d /config -s /usr/sbin/nologin clauster

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # Bind all interfaces (a container is useless on loopback). host!=loopback
    # makes clauster REQUIRE enforced auth — set CLAUSTER_AUTH_ENABLED=true +
    # CLAUSTER_AUTH_PASSWORD_REQUIRED=true + CLAUSTER_AUTH_PASSWORD_HASH (or
    # reverse-proxy trust), or it exits on start. See README "Docker".
    CLAUSTER_HOST=0.0.0.0 \
    CLAUSTER_PORT=7621 \
    CLAUSTER_LOG_FORMAT=json \
    CLAUSTER_HOME=/config \
    PUID=1000 \
    PGID=1000

WORKDIR /app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# /config: clauster.yml + state_dir. /projects: the projects_root to manage.
VOLUME ["/config", "/projects"]
EXPOSE 7621

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7621/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["clauster", "run"]
