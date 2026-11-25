# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/ledger.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Install the exact, tested dependency set (includes pytest/httpx so the test
# suite can also run inside the image if desired).
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application + tests + scaling check.
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
COPY pytest.ini ./pytest.ini
COPY README.md ./README.md

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=4s --start-period=5s --retries=10 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# Single worker: the process-wide write lock serializes submissions, while
# SQLite BEGIN IMMEDIATE + primary-key uniqueness remain the durable backstop.
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT} --workers 1"]
