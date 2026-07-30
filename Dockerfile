# Single-image deployment for the Evidence Monitoring Agent.
#
# One container serves everything on port 80:
#   * nginx  -> serves the built React SPA and reverse-proxies /api/* to uvicorn
#   * uvicorn -> the FastAPI backend on 127.0.0.1:8000
#
# Both processes are supervised by supervisord. This mirrors the local dev
# setup (Vite proxies /api -> backend with the /api prefix stripped) so no
# application code has to change between dev and prod.

# ---------------------------------------------------------------------------
# Stage 1 - build the React frontend into static files.
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /ui

# Install deps first (better layer caching).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# Build the SPA -> /ui/dist
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 - runtime: python backend + nginx, supervised together.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# System packages: nginx (static + proxy), supervisor (process mgmt), curl (healthcheck).
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx supervisor curl \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default \
 # Send nginx logs to the container's stdout/stderr so `docker logs` shows them.
 && ln -sf /dev/stdout /var/log/nginx/access.log \
 && ln -sf /dev/stderr /var/log/nginx/error.log

WORKDIR /app

# Python deps first (better layer caching).
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend source.
COPY backend/ ./

# Built frontend -> nginx web root.
COPY --from=frontend /ui/dist /usr/share/nginx/html

# nginx + supervisor configuration.
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/supervisord.conf /etc/supervisor/conf.d/app.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1/healthz || exit 1

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
