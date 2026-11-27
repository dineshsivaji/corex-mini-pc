# chatgpt_api: FastAPI + Playwright Chromium (headed, on a virtual Xvfb display) + ffmpeg
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb ffmpeg fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

COPY chatgpt_api.py docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

EXPOSE 8090
ENTRYPOINT ["/app/docker-entrypoint.sh"]
