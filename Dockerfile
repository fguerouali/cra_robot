FROM python:3.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    APP_DATA=/app/data \
    IN_CONTAINER=1 \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER=/usr/bin/chromedriver \
    SIGNIN_URL=http://{host}:6080/vnc.html?autoconnect=1&resize=scale

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      chromium-driver \
      fonts-dejavu-core \
      fonts-liberation \
      novnc \
      openbox \
      procps \
      tini \
      websockify \
      x11-utils \
      x11vnc \
      xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY paths.py main.py workflow.py signature.py server.py index.html config.json ./
COPY fonts ./fonts
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && useradd --create-home --uid 1000 app \
 && mkdir -p /app/data /tmp/.X11-unix \
 && chmod 1777 /tmp/.X11-unix \
 && chown -R app:app /app /entrypoint.sh

USER app
VOLUME ["/app/data"]
EXPOSE 8765 6080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/state', timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
