# syntax=docker/dockerfile:1.7

FROM python:3.13-slim AS dependencies

ARG UV_VERSION=0.9.17

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN python -m pip install "uv==${UV_VERSION}"

COPY pyproject.toml uv.lock ./

# Match the immutable host release flow. The project itself is intentionally
# not installed because the runtime imports the exact archived backend source.
RUN uv sync --frozen --no-install-project


FROM dependencies AS tokenizer-cache

# TokenCounter(None) uses the GPT-4/cl100k tokenizer, while LightRAG defaults
# to a GPT-4o/o200k tokenizer. Tiktoken otherwise downloads either encoding on
# first use, which is incompatible with the immutable read-only runtime.
ENV TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken

RUN mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && /app/.venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base"); tiktoken.get_encoding("o200k_base")' \
    && test -n "$(find "${TIKTOKEN_CACHE_DIR}" -type f -print -quit)"


FROM dependencies AS hanlp-cache

# The lexical tokenizer (HanLP msra_crf, 389MB with the electra vocab and
# char table) would otherwise download from download.hanlp.com on the first
# tokenization after every container recreation — the cache lives under
# HOME=/tmp which each deploy wipes, so the startup HanLP prewarm stalled for
# minutes on a fresh download per release. Running a real hanlp.load here
# populates every side asset into HANLP_HOME; the stage depends only on the
# dependency layer, so build cache reuses it across candidate SHAs and cold
# containers load offline from the read-only image. Keep the URL in sync with
# backend/rag/tokenization.py DEFAULT_HANLP_MODEL (contract-tested in
# test_quality_gate_contract.py); runtime RAG_HANLP_MODEL overrides fall back
# to downloading exactly as before.
ENV HANLP_HOME=/app/.cache/hanlp

RUN /app/.venv/bin/python -c 'import hanlp; hanlp.load("https://download.hanlp.com/tok/extra/msra_crf_electra_base_20220507_113936.zip", devices=-1)' \
    && test -n "$(find "${HANLP_HOME}" -name model.pt -print -quit)" \
    && test -f "${HANLP_HOME}/utils/char_table_20210602_202632.json"


FROM python:3.13-slim AS runtime

ENV HOME=/tmp \
    PATH=/app/.venv/bin:${PATH} \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken \
    HANLP_HOME=/app/.cache/hanlp \
    SERVER_HOST=0.0.0.0 \
    SERVER_RELOAD=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-noto-cjk \
        libgomp1 \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dependencies /app/.venv /app/.venv
# The quality-gate image extends this immutable backend image. Reuse the exact
# pinned uv binary from the dependency stage instead of downloading it again
# for every candidate SHA.
COPY --from=dependencies /usr/local/bin/uv /usr/local/bin/uv
COPY --from=tokenizer-cache /app/.cache/tiktoken /app/.cache/tiktoken
COPY --from=hanlp-cache /app/.cache/hanlp /app/.cache/hanlp

# Keep git in its own layer so the cached LibreOffice/fonts layer above is not
# invalidated when the package list changes. The quality-gate image reuses this
# backend image and no longer runs its own apt install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY backend/ /app/backend/

# Git archives are extracted by the controller under umask 077. Keep the
# application immutable while allowing the unprivileged runtime UID to import
# the source tree.
RUN find /app/backend -type d -exec chmod 0555 {} + \
    && find /app/backend -type f -exec chmod 0444 {} + \
    && find /app/.cache/tiktoken -type d -exec chmod 0555 {} + \
    && find /app/.cache/tiktoken -type f -exec chmod 0444 {} + \
    && find /app/.cache/hanlp -type d -exec chmod 0555 {} + \
    && find /app/.cache/hanlp -type f -exec chmod 0444 {} +

# The source SHA changes on every candidate build. Declare it only after all
# heavy layers (apt, venv copy, source copy) so classic docker build can reuse
# them across candidate SHAs.
ARG SOURCE_SHA

LABEL org.opencontainers.image.title="RADAI backend test runtime" \
      org.opencontainers.image.revision="${SOURCE_SHA}"

USER 10001:10001

EXPOSE 8082 8083 8086

CMD ["python", "-m", "backend.cli", "run-api", "--service-role", "agent", "--host", "0.0.0.0", "--port", "8082", "--workers", "1"]
