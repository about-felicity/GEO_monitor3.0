FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/monitor \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev build-essential wget ca-certificates \
      fonts-noto-cjk gosu \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/chrome.deb \
    && apt-get install -y /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

WORKDIR /app
COPY requirements ./requirements
RUN pip install --no-cache-dir -r requirements/web.txt

COPY yuanbao_monitor/dashboard/package.json ./yuanbao_monitor/dashboard/package.json
RUN npm install --prefix yuanbao_monitor/dashboard --no-audit --no-fund

COPY . .
RUN npm run build --prefix yuanbao_monitor/dashboard \
    && chmod +x deploy/server/entrypoint.sh \
    && useradd --create-home --uid 10001 monitor \
    && mkdir -p runtime config \
    && chown -R monitor:monitor /app /opt/venv

ENTRYPOINT ["/app/deploy/server/entrypoint.sh"]
