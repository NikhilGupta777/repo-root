# Python slim + FFmpeg + yt-dlp
FROM python:3.11-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Security (non-root)
RUN useradd -m appuser
WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App
COPY . .

# Amplify passes a PORT env; default to 8080 if not set
ENV HOST=0.0.0.0 \
    PORT=8080 \
    # tuneables for your app (safe defaults)
    KEEP_HOURS=24 \
    MAX_TRIM_SECONDS=3600 \
    MAX_CONCURRENT_JOBS=2

# Expose the container port
EXPOSE 8080

# Gunicorn: 2 workers, gthread for FFmpeg I/O, no hard timeout (FFmpeg controls duration)
CMD ["gunicorn", "-w", "2", "-k", "gthread", "--threads", "8", "--timeout", "0", "-b", "0.0.0.0:${PORT}", "app:app"]
