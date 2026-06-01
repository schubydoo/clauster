# syntax=docker/dockerfile:1
#
# Multi-arch (linux/amd64, linux/arm64) image for clauster, per spec §"Docker
# image": python:3.14-slim-trixie base, non-root, PUID/PGID, healthcheck, JSON logs.
#
# clauster spawns `claude remote-control` bridges, so the claude CLI is NOT
# baked in — provide it at runtime (mount it onto PATH, or build a derived image
# that installs it) along with ~/.claude credentials and your projects dir.

# ----- builder: resolve the locked deps into a self-contained venv -----------
FROM python:3.14-slim-trixie@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS builder

# renovate: datasource=docker depName=ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.11.18@sha256:78bc42400d77b0678ba95765305c826652ed5431f399257271dda681d0318f03 /uv /uvx /bin/

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
FROM python:3.14-slim-trixie@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS runtime

# apt upgrade pulls any Debian security fixes published since the base image was
# built (defense-in-depth on rebuilds). git: provisioning (create --git-init /
# clone). gosu + passwd: PUID/PGID remap then privilege-drop in the entrypoint.
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
    # makes clauster REQUIRE auth — set CLAUSTER_AUTH_PASSWORD_HASH or it exits.
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
