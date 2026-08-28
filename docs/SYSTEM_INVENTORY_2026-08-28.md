# GEO 系统现状盘点（2026-08-28）

本文件记录迁移 Windows Worker 前的只读盘点结果。盘点没有修改线上服务器。

## 1. 线上服务器

### 对外入口

- 用户入口：`https://www.ifbcy.com/geo/`
- 任务控制台：`https://www.ifbcy.com/geo/tasks`
- API 前缀：`https://www.ifbcy.com/geo/api/`
- Nginx 对外监听 80/443；80/443 之外的面板端口没有开放到公网。

### 运行服务

| 层 | 当前服务 | 作用 |
| --- | --- | --- |
| 入口 | `nginx.service` | TLS、路径路由、反向代理 |
| 容器 | Docker / containerd | 承载面板与任务 API |
| 应用 | `hybrid-panel-1` | React 静态面板、Python API、SQLite 队列 |
| 运维 | `sshd.service`、`crond.service` | 远程维护、Certbot 每日续签 |

`hybrid-panel-1` 使用 `unless-stopped` 重启策略，限制为 640 MiB 内存和 1 CPU。主机回环端口映射如下：

- `127.0.0.1:8300 -> container:3000`：前端页面
- `127.0.0.1:8876 -> container:8765`：任务与报告 API

Nginx 路由：

- `/geo/api/* -> 127.0.0.1:8876/api/*`
- `/geo/tasks -> 127.0.0.1:8876/tasks`
- `/geo/* -> 127.0.0.1:8300/*`

### 服务器文件职责

服务器项目根目录为 `/opt/geo-monitor`。容器只读挂载下列源文件：

- `doubao_dashboard_server.py`：HTTP API、任务控制台、报告接口
- `doubao_env_loader.py`：服务端环境变量读取
- `monitor_core/remote_tasks.py`：队列、租约、结果、报告状态机
- `yuanbao_monitor/dashboard/dist/`：构建后的客户前端
- `yuanbao_monitor/dashboard/scripts/verify-production-assets.mjs`：启动前静态资源校验
- `deploy/hybrid/entrypoint.sh`：容器入口

唯一读写挂载是 `/opt/geo-monitor/deploy/hybrid/runtime -> /app/runtime`。其中 `remote_tasks.sqlite3` 是任务、日志、Worker 心跳、结果和报告的当前事实来源。盘点时数据库约 1.2 MiB，包含 1 个任务、7 个结果、20 条事件、2 个 Worker 心跳和 5 个登录请求；数量只代表盘点瞬间。

服务器目录不是 Git 工作副本，因此后续发布仍应从受控 Git 分支构建部署包，而不是在线上直接编辑。

### 风险

盘点时根磁盘 40 GiB 已使用 99%，仅余约 691 MiB。这不会由本次迁移自动清理，但在下一次构建镜像、写日志或数据库增长前必须单独安排容量清理/扩容，并先确认可删除对象，禁止直接执行宽范围删除。

## 2. 当前 Mac 本机

### 项目内资源

| 路径 | 大小（盘点时） | 说明 |
| --- | ---: | --- |
| `.venv/` | 37 MiB | 基础 Python 3.14.4 环境 |
| `runtime/worker-venv/` | 362 MiB | 昨日建立的抓取 Worker 环境 |
| `Scrapling/` | 16 MiB | 本地上游源码检出，包含自己的 `.git`，不纳入本仓库 |
| `geo_chrome_extension/` | 112 KiB | 旧 Chrome 插件方案，仅供历史参考 |
| `yuanbao_monitor/dashboard/node_modules/` | 715 MiB | 前端依赖树 |

`runtime/worker-venv` 已安装的关键包：`scrapling==0.4.15`、`patchright==1.62.1`、`playwright==1.62.0`、`requests==2.34.2`、`lxml==6.1.2`。

### 全局资源核查

- 全局 Python：3.14.4；Node.js：24.19.0；npm：11.17.0。
- npm 全局包只有 npm 和 corepack。
- 没有发现全局/用户级 Scrapling、Patchright、Playwright、OpenAI、Pandas 等新增包。
- Playwright 浏览器缓存约 1.1 GiB，但目录时间为 2026-08-13，不是昨日安装。

结论：昨日抓取相关资源在项目的 `runtime/worker-venv` 和本地 `Scrapling/` 中，不是系统全局安装。Windows 机器应重新创建虚拟环境，不应复制 Mac 虚拟环境或浏览器二进制缓存。

### 当前进程

盘点时没有项目 Worker、面板或分析进程在运行。发现一个 2026-08-25 启动的 `python -m http.server 8765 --directory /tmp`，它与本项目无关，本次未停止。

## 3. 不应迁移或提交的内容

- `config/remote_worker.env`、`server.env`、API Key、Worker Token、Cookie。
- `runtime/` 中的数据库、JSONL、日志、浏览器资料和临时文件。
- `.venv/`、`runtime/worker-venv/`、`node_modules/`。
- Mac 的 Playwright 缓存。
- `Scrapling/` 上游源码检出；Windows 端按版本安装依赖即可。
- `.DS_Store` 和任何截图、DOM dump、验证码中间产物。

