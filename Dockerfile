# Stage 1: build the React UI
FROM node:22-alpine AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build

# Stage 2: runtime — Python API serving the built UI on one origin
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen --no-install-project
COPY src/ ./src/
COPY eval/ ./eval/
RUN uv sync --no-dev --frozen
COPY --from=ui-build /ui/dist ./ui/dist
ENV HIPPO_UI_DIST=/app/ui/dist

# Run as a non-root user. Pre-create /app/data and chown /app so a mounted
# named volume inherits appuser ownership and the SQLite db stays writable.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
# --no-sync: the image is already synced at build time; skip uv's runtime
# re-sync so startup is fast and the container needs no build tooling at launch.
CMD ["uv", "run", "--no-sync", "hippo", "serve", "--host", "0.0.0.0"]
