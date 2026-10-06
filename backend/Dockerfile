FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 CMD curl -f http://localhost:${PORT:-8000}/api/v1/health || exit 1
# Migration runs here, inside the app's own container/network context, instead
# of Railway's separate Pre-Deploy Command job. The pre-deploy job resolves
# postgres.railway.internal differently and kept failing password auth even
# with a verified-correct credential; a plain runtime connection from this same
# image does not. If it fails, the container exits before uvicorn starts, so a
# bad migration still blocks the deploy exactly as before.
CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
