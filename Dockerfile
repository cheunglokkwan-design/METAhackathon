# Hardened production Dockerfile for Margin (FastAPI + PostgreSQL client + Web App)
# Works on Hugging Face Spaces (port 7860), Render, Railway, Fly.io, or Docker Compose.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Create a non-root system user for container security best practices
RUN groupadd --system margin && useradd --system --gid margin --create-home margin

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/postgres/init.sql docker/postgres/init.sql
COPY server server
COPY web web

RUN mkdir -p /app/.margin_data && chown -R margin:margin /app

USER margin

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os, urllib.request; port = os.getenv('PORT', '7860'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3)" || exit 1

CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers"]
