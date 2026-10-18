# syntax=docker/dockerfile:1
#
# Build:  docker build -t bot-farm-silence-tracker .
# Run:    docker run --rm -v bfst-data:/data bot-farm-silence-tracker scan --platform reddit-archive --n 30
#
# The program runs as an unprivileged user (uid 10001) with /data as its home and working
# folder. The cache database therefore lives at /data/.cache/bot-farm-silence-tracker/ and output
# files are written to /data, so both survive in the volume between runs.
#
# Base images are pinned by digest for reproducible builds. To update them, look up the new
# digests with "docker buildx imagetools inspect <image>:<tag>".

FROM ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 AS uv

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first, from the lock file, so this layer is reused when only the code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
COPY config ./config
COPY events.toml ./
RUN uv sync --locked --no-dev --no-editable

# The final image has the virtual environment but neither uv nor the source tree.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN groupadd --system --gid 10001 bfst \
    && useradd --system --uid 10001 --gid bfst --home-dir /data --shell /usr/sbin/nologin bfst \
    && mkdir -p /data/.cache \
    && chown -R bfst:bfst /data
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/data \
    XDG_CACHE_HOME=/data/.cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER bfst
WORKDIR /data
VOLUME /data

ENTRYPOINT ["bot-farm-silence-tracker"]
CMD ["--help"]
