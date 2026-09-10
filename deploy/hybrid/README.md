# 本地采集 + 服务器面板

此部署不在服务器安装 Chrome、模拟器或模型采集器。服务器只运行 React
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

管理员报告中心还需要配置 `MONITOR_ADMIN_USERNAME`、
`MONITOR_ADMIN_PASSWORD_HASH` 和 `MONITOR_ADMIN_SESSION_SECRET`。密码只能保存为
PBKDF2-SHA256 哈希，且其中的 `$` 在 Compose env 文件里必须写成 `$$`；不能把
明文密码写入 `server.env`。管理员可在 `/geo/admin`
查看全部诊断报告并调整后续任务的默认轮数。

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

当前生产 Worker 使用 `config/worker_token.secret` 保存 Worker Token，并由
`scripts/run_windows_enterprise_worker.ps1` 设置服务器地址、Worker ID、六模型
采集器和本地分析器。该文件、浏览器 profile、模拟器数据和登录状态均不得提交 Git。

如使用兼容 Worker，可复制 `config/remote_worker.env.example` 为
`config/remote_worker.env`：

```env
MONITOR_TASK_SERVER=https://panel.example.com
MONITOR_WORKER_TOKEN=服务端的独立Worker密钥
MONITOR_WORKER_ID=office-desktop
```

本机只发起出站 HTTPS 请求，不需要公网 IP、端口映射或关闭防火墙。当前六模型
企业 Worker 的详细软件、模拟器、Android、端口和登录要求以项目根目录
`README.md` 为准。

## 3. 用户操作

用户通过公开诊断页提交品牌、产品和问题；管理员也可在任务管理页创建受控任务。
本机 Worker 全局只领取一个活动任务，任务内部六模型并行，关机期间任务保持排队。

## 安全边界

- 任务密钥只允许创建、查看和取消任务。
- Worker 密钥才能领取任务、续租和上传结果。
- 任务参数经过模型白名单和次数限制，不能传系统命令。
- 服务器展示模式禁止旧的 `/api/control/*` 接口直接启动采集器。
- 结果用 request ID 幂等入库；重复回传不会重复计数。
