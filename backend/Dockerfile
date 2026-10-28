# Build from repository root:
#   docker build -f backend/Dockerfile -t platform-backend .
FROM python:3.12-slim@sha256:6b25cbe6577518889d553cdd5a5da97d259348d8f6e45b527dd731caaba27a07

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# The container sandbox provider talks to a container engine over its socket,
# which needs a client binary as well as the socket itself. The static CLI is
# used rather than the distro `docker.io` package because that one drags in a
# daemon this image will never run. Drop this layer if you set
# SANDBOX_PROVIDER=subprocess or point CONTAINER_ENGINE at something else.
# The checksums pin the exact archive for DOCKER_CLI_VERSION; update all three
# together (download.docker.com publishes no checksum file for static builds,
# so compute them with `curl -fsSL <url> | sha256sum`).
ARG DOCKER_CLI_VERSION=27.3.1
ARG DOCKER_CLI_SHA256_X86_64=9b4f6fe406e50f9085ee474c451e2bb5adb119a03591f467922d3b4e2ddf31d3
ARG DOCKER_CLI_SHA256_AARCH64=4da6a6c7502b7ab561675a5ff5ac192d9b49d76d0b8847cf17ade246122279f4
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
        x86_64) sha="$DOCKER_CLI_SHA256_X86_64" ;; \
        aarch64) sha="$DOCKER_CLI_SHA256_AARCH64" ;; \
        *) echo "no Docker CLI checksum for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    echo "${sha}  /tmp/docker.tgz" | sha256sum -c -; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    install -m 0755 /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker.tgz /tmp/docker; \
    docker --version

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

COPY backend/requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy the application as a directory rather than enumerating modules. The old
# per-file list had to be edited for every new module and silently broke the
# build when one was removed; .dockerignore already excludes tests, caches and
# local state.
COPY backend/ /app/

COPY backend/docker-entrypoint.sh /docker-entrypoint.sh

# Skills data is copied to a seed location inside the image.
# The entrypoint initialises the /src/data volume from this seed on first run
# so that runtime-published skills (written to /src/data at runtime) survive
# container rebuilds.
COPY src/data/skills /app/skills-seed/skills
COPY src/data/skills-acl.yaml /app/skills-seed/skills-acl.yaml

RUN chmod +x /docker-entrypoint.sh \
    && mkdir -p /data /app/generated /src/data/skills /sandbox-workspaces \
    && chown -R appuser:appuser /data /app/generated /src/data /app/skills-seed /sandbox-workspaces

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HUB_DATABASE_PATH=/data/hub.db

ARG BUILD_COMMIT=unknown
ENV BUILD_COMMIT=${BUILD_COMMIT}

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
