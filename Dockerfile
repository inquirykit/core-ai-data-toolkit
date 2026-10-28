# The browser pages (frontend/), built into /api/static
FROM node:24-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Optional job environments (jobs/README.md), each its own Python with its own
# packages, e.g. --build-arg JOB_ENVS=autogluon (several GB: torch, Ray, ...).
ARG JOB_ENVS=""
ENV JOB_ENVS_DIR=/opt/envs \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_NO_CACHE=1 \
    NLTK_DATA=/opt/nltk_data
COPY requirements-*.txt ./
COPY scripts/setup_job_env.sh scripts/
COPY scripts/job_env_hooks/ scripts/job_env_hooks/
RUN if [ -n "$JOB_ENVS" ]; then \
        pip install --no-cache-dir uv \
        && for env in $JOB_ENVS; do scripts/setup_job_env.sh "$env"; done; \
    fi

# Copy application code and the built pages
COPY . .
COPY --from=frontend /api/static ./api/static

# Non-root user; Hugging Face weights are cached under HF_HOME so a volume
# can persist them across container restarts. The setup wizard's settings go
# to ENV_FILE, on its own volume for the same reason.
ENV HOST=0.0.0.0 \
    HF_HOME=/app/.cache/huggingface \
    IMAGE_DIR=/app/data/images \
    ENV_FILE=/app/data/config/settings.env \
    MODEL_SERVERS_FILE=/app/data/config/model_servers.toml \
    MODEL_SERVERS_SAVED_FILE=/app/data/config/model_servers.json \
    ARCADEDB_PATH=/app/data/arcadedb \
    JOBS_DIR=/app/data/jobs \
    JOB_DATA_DIR=/app/data/datasets \
    ENRICHERS_DIR=/app/data/enrichers \
    EVIDENCE_DIR=/app/data/evidence
# glibc keeps freed memory, so RSS only grows as models are swapped; these
# make large blocks go back to the OS when a model is unloaded.
ENV MALLOC_MMAP_THRESHOLD_=65536 \
    MALLOC_TRIM_THRESHOLD_=65536
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/.cache/huggingface /app/data/images /app/data/config /app/data/arcadedb \
             /app/data/jobs /app/data/datasets /app/data/enrichers /app/data/evidence \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
