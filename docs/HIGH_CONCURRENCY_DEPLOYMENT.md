# 面板高并发部署

当前服务已具备有界请求池、热点 JSON/gzip 复用、ETag/304、请求合并、过期数据后台刷新、PostgreSQL 连接池，以及可选 Redis 跨实例共享。

## 单实例推荐配置

```powershell
$env:MONITOR_HTTP_WORKERS = "256"
$env:MONITOR_HTTP_MAX_PENDING = "2048"
$env:MONITOR_HTTP_BACKLOG = "1024"
$env:MONITOR_HTTP_CACHE_MAX = "256"
$env:MONITOR_DATABASE_POOL_MIN = "2"
$env:MONITOR_DATABASE_POOL_MAX = "32"
python -u doubao_dashboard_server.py
```

`/api/health` 可查看工作线程、缓存命中、Redis 和 PostgreSQL 连接池状态。

## 多实例与 Redis

所有读取实例必须连接同一个 PostgreSQL。配置 Redis 后，HTTP 热点结果也会在实例间共享：

```powershell
$env:MONITOR_REDIS_URL = "redis://redis-host:6379/0"
```

可在负载均衡器后启动多个读取实例。采集控制接口和内容归档进程应只放在一个主实例；读取副本设置：

```powershell
$env:DOUBAO_CONTENT_WORKER_DISABLED = "1"
$env:MONITOR_DASHBOARD_WARMUP_DISABLED = "1"
```

外层负载均衡需要启用 gzip，并保留 `ETag`、`If-None-Match`、`Cache-Control` 和 `Vary` 请求/响应头。`/api/control/*` 及控制类 POST 请求固定转发到主实例，普通分析 GET 请求可以分发到全部读取实例。健康检查地址使用 `/api/health`。

## 压力测试

完整响应：

```powershell
python -m tools.development.load_test_dashboard --requests 500 --concurrency 100
```

模拟大量用户的后台条件轮询：

```powershell
python -m tools.development.load_test_dashboard --requests 5000 --concurrency 500 --conditional
```

脚本在出现非 200/304 响应或连接错误时返回失败退出码。
