FROM node:22-bookworm-slim AS frontend
WORKDIR /build/dashboard
COPY yuanbao_monitor/dashboard/package.json ./package.json
RUN npm install --no-audit --no-fund
COPY yuanbao_monitor/dashboard/ ./
RUN npm run build

FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 monitor
WORKDIR /app
COPY *.py ./
COPY monitor_core ./monitor_core
COPY model_plugins ./model_plugins
COPY web_collectors ./web_collectors
COPY doubao_ref_extension ./doubao_ref_extension
COPY config ./config
COPY --from=frontend /build/dashboard ./yuanbao_monitor/dashboard
COPY deploy/hybrid/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /app/runtime && chown -R monitor:monitor /app
USER monitor
EXPOSE 3000 8765
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
