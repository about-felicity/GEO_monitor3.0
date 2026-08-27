# Linux 服务器部署

推荐 Ubuntu 22.04/24.04、4 核 CPU、8 GB 内存、至少 30 GB 磁盘。服务器需先安装 Docker Engine 与 Docker Compose 插件。

## 启动完整系统

```bash
cd deploy/server
cp server.env.example server.env
# 编辑 server.env，至少设置 POSTGRES_PASSWORD
docker compose --env-file server.env up -d --build
```

面板地址为 `http://服务器IP:3000`，后端为 `http://服务器IP:8765`。

## 元宝首次登录

noVNC 只监听服务器本机，先在自己的电脑建立 SSH 隧道：

```bash
ssh -L 6080:127.0.0.1:6080 用户名@服务器IP
```

浏览器打开 `http://127.0.0.1:6080/vnc.html`，随后在服务器执行：

```bash
cd deploy/server
docker compose --env-file server.env exec -u monitor app \
  python -m web_collectors.scrapling_yuanbao --login --timeout 600
```

在 noVNC 中完成腾讯元宝登录。会话保存在项目的 `runtime/web_profiles/yuanbao_scrapling/`，容器重启后继续复用。

登录状态检查：

```bash
docker compose --env-file server.env exec -u monitor app \
  python -m web_collectors.scrapling_yuanbao --check
```

之后可在统一面板启动元宝循环任务，也可以直接运行 `python -m web_collectors.loop --model yuanbao ...`。

## DeepSeek API Key

抓取网页不需要 Key。需要产品推荐识别、品牌归一和产品排名复核时，将 DeepSeek Key 写入 `server.env` 的 `DEEPSEEK_API_KEY`，然后执行：

```bash
docker compose --env-file server.env up -d --force-recreate app
```

不要把 `server.env` 提交版本库，也不要在聊天或日志中粘贴真实 Key。
