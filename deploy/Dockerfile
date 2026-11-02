# Control plane image: API server, scheduler worker and migrations.
# Build from the repository root:  docker build -f deploy/Dockerfile -t pool-control-plane .
# Hardening for production: pin both base images by digest.

FROM node:22-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json* ./
RUN if [ -f package.json ]; then npm ci --no-audit --no-fund; fi
COPY web/ ./
RUN if [ -f package.json ]; then npm run build; else mkdir -p dist; fi

FROM python:3.12-slim-bookworm AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir "uv>=0.6,<0.7"
WORKDIR /srv
COPY pyproject.toml uv.lock ./
COPY protocol/ protocol/
COPY control-plane/ control-plane/
COPY cli/ cli/
RUN uv sync --frozen --no-dev --package pool-cp --extra s3 && rm -rf /root/.cache
COPY --from=web /src/web/dist /srv/web/dist
RUN useradd --system --uid 10001 --home /srv pool && mkdir -p /var/lib/pool && chown pool /var/lib/pool
USER pool
ENV PATH="/srv/.venv/bin:$PATH" POOL_DATA_DIR=/var/lib/pool
EXPOSE 8000
CMD ["uvicorn", "pool_cp.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--ws-max-size", "1048576"]
