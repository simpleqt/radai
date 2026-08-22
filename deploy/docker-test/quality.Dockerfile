# syntax=docker/dockerfile:1.7

ARG BASE_BACKEND_IMAGE
FROM ${BASE_BACKEND_IMAGE}

USER root

ENV HOME=/tmp \
    PATH=/app/.venv/bin:${PATH} \
    PYTHONPATH=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Declare the per-candidate SHA here; git itself is already installed in the
# backend base image, so the quality image no longer needs an apt layer.
ARG SOURCE_SHA

LABEL org.opencontainers.image.title="RADAI test quality gate" \
      org.opencontainers.image.revision="${SOURCE_SHA}"

COPY pyproject.toml uv.lock /quality-meta/
# The quality container executes source directly. Avoid re-compiling the
# inherited runtime environment, which can exceed the build container's file
# descriptor budget on a cold dependency cache.
RUN cd /quality-meta \
    && UV_COMPILE_BYTECODE=0 \
       UV_PROJECT_ENVIRONMENT=/app/.venv \
       /usr/local/bin/uv sync --frozen --no-install-project --extra test

COPY . /workspace
WORKDIR /workspace

RUN test "$(cat /workspace/.source-sha)" = "${SOURCE_SHA}" \
    && mkdir -p /workspace/backend/data \
    && git init --quiet \
    && git config user.name "RADAI Quality Gate" \
    && git config user.email "quality-gate@localhost" \
    && git config --system --add safe.directory /workspace \
    && git add -A \
    && git add --force .source-sha \
    && git commit --quiet -m "immutable quality-gate fixture" \
    && git remote add origin /workspace \
    && find /workspace -type d -exec chmod a+rx {} + \
    && find /workspace -type f -exec chmod a+r {} + \
    && test -x /workspace/deploy/docker-test/auto-update.sh \
    && test -x /workspace/deploy/production/promote-tested.sh

USER 10001:10001

ENTRYPOINT ["/app/.venv/bin/python", "/workspace/deploy/docker-test/run_quality_gate.py"]
