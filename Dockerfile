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

# Job environments (jobs/README.md), each its own Python with its own packages,
# are installed from /admin (the setup-env job) into /opt/envs, a volume of its
# own, with the Python 3.13 uv downloads for them, which their interpreters
# link to. Nothing is baked into the image.
ENV JOB_ENVS_DIR=/opt/envs \
    UV_PYTHON_INSTALL_DIR=/opt/envs/.python \
    UV_NO_CACHE=1

# Copy application code and the built pages
COPY . .
COPY --from=frontend /api/static ./api/static

# Non-root user; Hugging Face weights are cached under HF_HOME so a volume
# can persist them across container restarts. Everything the service keeps is
# in DATA_DIR, one volume: the setup wizard's settings (ENV_FILE) and the model
# servers file go there too.
ENV HOST=0.0.0.0 \
    HF_HOME=/app/.cache/huggingface \
    DATA_DIR=/app/data \
    ENV_FILE=/app/data/settings.env \
    MODEL_SERVERS_FILE=/app/data/model_servers.toml
# glibc keeps freed memory, so RSS only grows as models are swapped; these
# make large blocks go back to the OS when a model is unloaded.
ENV MALLOC_MMAP_THRESHOLD_=65536 \
    MALLOC_TRIM_THRESHOLD_=65536
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/.cache/huggingface /app/data/images /opt/envs \
    && chown -R appuser:appuser /app /opt/envs
USER appuser
# The database's full-text extension, in the image: the service turns off DuckDB's downloads once it is open.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL fts')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "main.py"]
