# CPU-only worker image. Build context is the repository root.
#
# Uses the builder's built-in Dockerfile frontend instead of pulling a floating
# `docker/dockerfile` image; every instruction below is supported by the
# BuildKit frontend shipped with current Docker and by Render's builder.
#
# The image deliberately does not contain models, media, credentials or the
# SQLite database. Those live on the mounted data volume at /data.

FROM python:3.13.3-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLIPFORGE_DATA_DIR=/data \
    CLIPFORGE_WORKER_HOST=0.0.0.0 \
    PORT=8788

# Use HTTPS and bounded retries: plain HTTP to deb.debian.org is unreliable on
# some Docker Desktop networks, and a transient mirror failure should not make
# the image unreproducible.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 update \
    && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=5 \
        -o Acquire::http::Timeout=30 \
        ffmpeg \
        fonts-dejavu-core \
        gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Runtime pins first, then the project without resolving dependencies again.
# Keep pip's download cache in a BuildKit mount, not the image layer, so a
# transient package-index timeout can be retried without re-downloading the
# whole set. PIP_NO_CACHE_DIR stays on for every other layer.
COPY apps/worker/requirements.lock ./
RUN --mount=type=cache,id=clipforge-pip,target=/root/.cache/pip \
    PIP_NO_CACHE_DIR=0 python -m pip install --retries 10 --timeout 60 -r requirements.lock

COPY apps/worker/pyproject.toml ./
COPY apps/worker/src ./src
RUN python -m pip install --no-deps .

RUN groupadd --system --gid 1001 clipforge \
    && useradd --system --uid 1001 --gid clipforge --create-home clipforge \
    && mkdir -p /data \
    && chown -R clipforge:clipforge /app /data

COPY --chmod=755 docker/worker-entrypoint.sh /usr/local/bin/worker-entrypoint

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8788')+'/api/health', timeout=4)"

# The container starts as root only so the entrypoint can claim a freshly
# mounted data volume (Render and bind mounts are often root-owned). It execs
# gosu and drops to UID 1001 before uvicorn starts. Do not add `USER clipforge`
# here: that would prevent the chown and make a root-owned mount unwritable.
ENTRYPOINT ["worker-entrypoint"]
CMD ["sh", "-c", "exec uvicorn clipforge.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8788} --proxy-headers --timeout-graceful-shutdown 20"]
