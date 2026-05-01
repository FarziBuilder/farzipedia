# FarziPedia — production container
# Slim Python base. Browser interaction with YouTube happens on Bright
# Data's hosted Chromium (Scraping Browser), so we don't need a local
# Chromium install. Kept ffmpeg in case we re-introduce frame extraction
# from a downloaded file later.

FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/jobs

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
