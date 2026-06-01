# Single image for the whole platform: every service, worker, and the migrate
# job run from this image with a different command (the monorepo "one image,
# many CMDs" topology). Python 3.11 — the pinned deps (e.g. pydantic 2.10.3)
# ship 3.11 wheels.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the compose healthchecks against /health.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so the layer is cached across app-code changes.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code + migrations (the migrate job runs `alembic upgrade head`).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Default command (root health app); compose overrides this per service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
