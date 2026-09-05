FROM python:3.12-slim

# FFmpeg/ffprobe are required by the scanner and the worker's pre-flight checks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/
COPY alembic.ini .

# Never run as root; the container only needs to read the video volume.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

CMD ["python", "-m", "src.worker"]
