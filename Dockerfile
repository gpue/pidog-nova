# syntax=docker/dockerfile:1.7
#
# pidog-nova runtime image.
#
# Single-stage build with uv: pidog-nova ships a self-contained HTML
# operator page (no Node toolchain needed), so we skip the multi-stage
# pattern other connectors use for their React UIs.
#
FROM python:3.11-slim AS runtime
WORKDIR /app

# uv needs git to fetch the SDK + nova-vda5050 pins from wandelbotsgmbh.
# ca-certificates is needed for the GitHub HTTPS clone.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install the uv binary directly — small, no Python deps required, and
# matches `uv sync` semantics from the repo root.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Some SDK dependencies live in private wandelbotsgmbh repos.  CI passes
# GITHUB_TOKEN as a build-arg; the credential is unset from gitconfig at
# the end of the sync step, but `docker history --no-trunc` will still
# surface the value via the ARG record — only push the produced image to
# a trusted private registry.  TODO: migrate to BuildKit `--mount=type=secret`.
ARG GITHUB_TOKEN
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1

# Install the locked dependency set into /app/.venv.  We copy
# pyproject.toml + uv.lock + README.md + src/ first because hatchling
# needs all four to build the pidog-nova package itself.
# mobile-integration-sdk and nova-vda5050 are public wandelbotsgmbh repos,
# so no auth is needed.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Operator HTML page (mounted at /ui by main.py via StaticFiles).
COPY ui/ ./ui/
# Brand assets (mounted at /static).
COPY static/ ./static/

# Default config-file location for the local-JSON fallback layer.
RUN mkdir -p /data
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    CONFIG_FILE=/data/config.json

EXPOSE 8000

# uvicorn comes from fastapi[standard] (via the SDK).  Launch the
# SDK-built FastAPI app composed in pidog_nova.main.
CMD ["uvicorn", "pidog_nova.main:app", "--host", "0.0.0.0", "--port", "8000"]
