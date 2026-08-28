# 本地采集 + 服务器面板

此部署不在服务器安装 Chrome，也不运行五模型采集器。服务器只运行 React
面板、任务队列和结果接收 API；办公室电脑主动通过 HTTPS 领取任务。

## 1. 服务端

```bash
cd /opt/geo-monitor/deploy/hybrid
cp server.env.example server.env
openssl rand -hex 32
openssl rand -hex 32
```

把两次生成的值分别写入 `MONITOR_TASK_API_TOKEN` 和
`MONITOR_WORKER_TOKEN`，然后启动：

```bash
docker compose up -d --build
curl -fsS http://127.0.0.1:8876/api/health
```

容器端口只绑定服务器回环地址。Nginx 示例：

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;

    location = /geo {
        return 301 /geo/;
    }

    location /geo/api/ {
        proxy_pass http://127.0.0.1:8876/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        client_max_body_size 10m;
    }

    location = /geo/tasks {
        proxy_pass http://127.0.0.1:8876/tasks;
        proxy_set_header Host $host;
    }

    location /geo/ {
        proxy_pass http://127.0.0.1:8300/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

先执行 `nginx -t`，通过后再 reload。证书继续使用服务器现有的 Certbot
流程。不要向安全组开放 8300、8876。

## 2. 本机 Worker

复制 `config/remote_worker.env.example` 为 `config/remote_worker.env`：

```env
MONITOR_TASK_SERVER=https://panel.example.com
MONITOR_WORKER_TOKEN=服务端的独立Worker密钥
MONITOR_WORKER_ID=office-desktop
```

双击 `启动远程任务采集代理.bat`。本机只发起出站 HTTPS 请求，不需要公网
IP、端口映射或关闭防火墙。五个模型的 Cookie 只通过本机 Worker 的进程
环境临时提供，不保存浏览器 profile。

## 3. 用户操作

用户打开 `https://panel.example.com/tasks`，输入任务访问密钥，选择模型、问题
和轮数后提交。任务默认限制为最多 10 个问题、每题 3 轮；本机 Worker 全局
顺序执行，关机期间任务保持排队。

## 安全边界

- 任务密钥只允许创建、查看和取消任务。
- Worker 密钥才能领取任务、续租和上传结果。
- 任务参数经过模型白名单和次数限制，不能传系统命令。
- 服务器展示模式禁止旧的 `/api/control/*` 接口直接启动采集器。
- 结果用 request ID 幂等入库；重复回传不会重复计数。
