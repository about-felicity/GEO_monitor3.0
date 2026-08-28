FROM node:22-bookworm-slim AS frontend
ARG VITE_MONITOR_BASE_PATH=/geo
ENV VITE_MONITOR_BASE_PATH=$VITE_MONITOR_BASE_PATH
WORKDIR /build/dashboard
COPY yuanbao_monitor/dashboard/package.json ./package.json
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm install --no-audit --no-fund --registry="$NPM_REGISTRY"
COPY yuanbao_monitor/dashboard/ ./
RUN npm run build

FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 monitor
WORKDIR /app
COPY --chown=monitor:monitor *.py ./
COPY --chown=monitor:monitor doubao_brand_settings.json ./
COPY --chown=monitor:monitor monitor_core ./monitor_core
COPY --chown=monitor:monitor model_plugins ./model_plugins
COPY --chown=monitor:monitor web_collectors ./web_collectors
COPY --chown=monitor:monitor doubao_ref_extension ./doubao_ref_extension
COPY --chown=monitor:monitor config ./config
COPY --from=frontend --chown=monitor:monitor /build/dashboard ./yuanbao_monitor/dashboard
COPY --chown=monitor:monitor --chmod=755 deploy/hybrid/entrypoint.sh /app/entrypoint.sh
RUN mkdir -p /app/runtime && chown monitor:monitor /app/runtime
USER monitor
EXPOSE 3000 8765
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
