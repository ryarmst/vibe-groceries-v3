FROM python:3.13-slim

# - No .pyc writes at runtime (read-only root filesystem); bytecode is compiled at build time.
# - MALLOC_ARENA_MAX keeps glibc from reserving per-thread arenas, which trims idle memory.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY pyproject.toml constraints.txt ./
COPY src ./src
RUN pip install -c constraints.txt . \
    && python -m compileall -q /usr/local/lib/python3.13/site-packages \
    && rm -rf /app/src /root/.cache \
    && useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown -R app:app /data

USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["vibe-groceries"]
