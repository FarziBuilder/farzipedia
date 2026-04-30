# FarziPedia — production container
# Slim Python base + ffmpeg + the app. Listens on $PORT (Render/Fly inject this).

FROM python:3.11-slim

# System deps: ffmpeg for frame extraction, ca-certificates for TLS,
# tini for proper signal handling under PID 1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so they cache between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

# Ensure the jobs directory exists at container start (writable scratch space)
RUN mkdir -p /app/jobs

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# tini cleanly forwards SIGTERM to uvicorn so graceful shutdowns work
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
