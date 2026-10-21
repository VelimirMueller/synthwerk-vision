# syntax=docker/dockerfile:1.7
# BACKEND=siglip (default, ~1.5 GB of weights) or BACKEND=mobilenet (14 MB).
ARG BACKEND=siglip

FROM python:3.14-slim AS build
ARG BACKEND
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY scripts ./scripts
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev \
 && mkdir -p models && .venv/bin/python scripts/fetch_model.py "$BACKEND" \
 && SYNTHWERK_VISION_BACKEND="$BACKEND" .venv/bin/python scripts/warm_label_cache.py

FROM python:3.14-slim
ARG BACKEND
RUN useradd --system --uid 10001 --home /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
# Git commit shown by /readyz; the build passes --build-arg COMMIT=<sha>.
ARG COMMIT=0000000
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 SYNTHWERK_VISION_UPLOAD_DIR=/data/uploads SYNTHWERK_VISION_DATA_DIR=/data/state \
    SYNTHWERK_VISION_BACKEND=$BACKEND SYNTHWERK_VISION_COMMIT=$COMMIT
RUN mkdir -p /data/uploads /data/state && chown app:app /data/uploads /data/state
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "synthwerk_vision.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
