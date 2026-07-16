# syntax=docker/dockerfile:1
#
# Multi-arch (linux/amd64, linux/arm64) image for clauster: Alpine + musl,
# non-root, PUID/PGID, healthcheck, JSON logs.
#
# clauster spawns `claude remote-control` bridges, so the claude CLI is NOT baked
# in — provide it at runtime (mount it onto PATH, or build a derived image that
# installs it) along with ~/.claude credentials and your projects dir.
#
# OS packages are EXPLICITLY PINNED (no unpinned `upgrade`), each ARG carrying a
# `# renovate:` comment so Renovate tracks it. NB: Alpine keeps only the latest
# version of each package, so a pinned build 404s once a newer -rN publishes,
# until Renovate's (auto-merged) bump lands. Reseed a pin's value with:
#   docker run --rm alpine:3.24.1 sh -c 'apk update >/dev/null && apk policy <pkg>'

# ----- builder: resolve the locked deps into a self-contained venv -----------
FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS builder

# renovate: datasource=repology depName=alpine_3_24/uv versioning=loose
ARG UV_VERSION=0.11.19-r0
# renovate: datasource=repology depName=alpine_3_24/python3 versioning=loose
ARG PYTHON3_VERSION=3.14.5-r0
# System python3 is the interpreter uv builds the venv against; it must exist at
# the same path in runtime (the copied venv points back at /usr/bin/python3). All
# native deps ship musllinux wheels, so uv installs binaries — no compiler needed.
RUN apk add --no-cache uv="${UV_VERSION}" python3="${PYTHON3_VERSION}"

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

# Deps first (cached unless the lockfile/manifest change), then the project.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable --python python3
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --python python3

# ----- runtime ---------------------------------------------------------------
FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS runtime

# python3 — runs the copied venv (same version as builder).
# git — provisioning (create --git-init / clone).
# shadow — groupmod/usermod/useradd/groupadd for the PUID/PGID remap (busybox lacks them).
# su-exec — musl-native privilege-drop in the entrypoint (replaces gosu; no Go CVE surface).
# renovate: datasource=repology depName=alpine_3_24/python3 versioning=loose
ARG PYTHON3_VERSION=3.14.5-r0
# renovate: datasource=repology depName=alpine_3_24/git versioning=loose
ARG GIT_VERSION=2.54.0-r0
# renovate: datasource=repology depName=alpine_3_24/shadow versioning=loose
ARG SHADOW_VERSION=4.18.0-r1
# renovate: datasource=repology depName=alpine_3_24/su-exec versioning=loose
ARG SU_EXEC_VERSION=0.3-r0
RUN apk add --no-cache \
        python3="${PYTHON3_VERSION}" \
        git="${GIT_VERSION}" \
        shadow="${SHADOW_VERSION}" \
        su-exec="${SU_EXEC_VERSION}"

# Default identity; remappable to the host's PUID/PGID at runtime.
RUN groupadd -g 1000 clauster \
    && useradd -u 1000 -g 1000 -d /config -s /sbin/nologin clauster

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
    CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7621/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["clauster", "run"]
