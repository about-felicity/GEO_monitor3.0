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

## 隐身会话认证

五个模型均不持久化浏览器 profile。启动容器时通过进程环境临时传入相应的
`MONITOR_<模型>_COOKIES_JSON`，变量名和格式见 `web_collectors/README.md`。
不要把 Cookie 写入 `server.env`。例如可在当前 shell 中 `export` 后执行
`docker compose`，Compose 会把已声明变量传入容器；shell 结束后及时 `unset`。

元宝登录状态检查：

```bash
docker compose --env-file server.env exec -u monitor app \
  python -m web_collectors.scrapling_yuanbao --check
```

之后可在统一面板启动循环任务，也可以直接运行 `python -m web_collectors.loop --model yuanbao ...`。

## DeepSeek API Key

抓取网页不需要 Key。需要产品推荐识别、品牌归一和产品排名复核时，将 DeepSeek Key 写入 `server.env` 的 `DEEPSEEK_API_KEY`，然后执行：

```bash
docker compose --env-file server.env up -d --force-recreate app
```

不要把 `server.env` 提交版本库，也不要在聊天或日志中粘贴真实 Key。
