# Cloud app only. The automation engine never runs in a container -- it runs on
# your PC, where your browser sessions live.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# The cloud app needs none of Playwright's browser tooling.
COPY requirements-cloud.txt .
RUN pip install --no-cache-dir -r requirements-cloud.txt

# The whole package, not just the cloud half: /agent.zip serves the agent
# source so a fresh PC can install without the repo, PyPI or a public mirror.
COPY src/jobauto                  src/jobauto
COPY config/preferences.yaml      config/preferences.yaml

EXPOSE 8000

# Respect WEB_CONCURRENCY -- Render sets it from the instance CPU count, and
# hardcoding a higher number just wastes memory on a free instance.
CMD gunicorn "jobauto.cloud.app:get_app()" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${WEB_CONCURRENCY:-2}" --threads 4 --timeout 60 --access-logfile -
