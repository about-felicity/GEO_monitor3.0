# GEO 品牌推荐诊断系统

这是当前生产主项目。系统接收客户的品牌推荐诊断，使用豆包、腾讯元宝、文心一言、千问、DeepSeek、Kimi 六个平台采集多轮真实回答和信源，计算品牌推荐概率、排名与竞品，并提供总管理员、下级管理员、客户报告和付费用户每日监控面板。

生产地址：

- 客户诊断：`https://www.ifbcy.com/geo/`
- 管理员中心：`https://www.ifbcy.com/geo/admin`
- 任务管理：`https://www.ifbcy.com/geo/tasks`
- 健康检查：`https://www.ifbcy.com/geo/api/health`
- 百一电子案例：`https://www.ifbcy.com/geo/bydz/`

## 1. 部署架构

系统由服务器和办公室 Windows Worker 两部分组成，不能只部署其中一边。

```text
客户浏览器
  -> Nginx /geo/
  -> React/Vinext 前端
  -> Python API
  -> SQLite 持久化任务队列
  -> Windows Worker 主动通过 HTTPS 领取任务
  -> 六模型采集、正文清洗、信源校验和本地分析
  -> 幂等回传每一轮结果
  -> 服务器生成诊断报告和每日监控面板
```

服务器不运行浏览器、不安装模拟器，也不保存模型账号 Cookie。Windows Worker 只主动访问服务器的 HTTPS 地址，不需要公网 IP、端口映射或开放入站端口。

任务队列保证：全局单活动任务、任务内模型并行、单模型轮次串行、心跳续租、断点续跑和幂等回传。即时诊断会抢占付费用户每日监控；诊断完成后，每日监控从已经保存的轮次继续。

## 2. 哪些代码部署到服务器

服务器生产目录为 `/opt/geo-monitor`，主要部署下列内容：

| 文件或目录 | 服务器职责 |
| --- | --- |
| `doubao_dashboard_server.py` | HTTP API、登录认证、管理员接口、报告路由和健康接口 |
| `monitor_core/remote_tasks.py` | SQLite 队列、租约、心跳、任务抢占、权限、概率和报告聚合 |
| `monitor_core/quality.py` | 回答主题一致性、正文质量和品牌识别规则 |
| `yuanbao_monitor/dashboard/` | 客户诊断、诊断报告、管理员和付费监控前端 |
| `model_plugins/` | 六模型名称、顺序及公共配置 |
| `web_collectors/config.py` | 服务端使用的模型目录配置 |
| `deploy/hybrid/` | 当前生产 Dockerfile、Compose、入口和环境变量模板 |
| `tools/deploy_enterprise_zero_downtime.py` | 8300/8301、8876/8877 蓝绿零停机发布 |
| `tools/deploy_enterprise_update.py` | 原地更新备用脚本，非首选 |

服务器运行时数据位于 `deploy/hybrid/runtime/`，包含队列数据库和报告数据。该目录、Docker 卷和历史报告禁止删除或提交 Git。

### 服务器环境

- 推荐系统：Ubuntu 22.04 或 24.04 x86_64。
- 必需软件：Docker Engine、Docker Compose 插件、Nginx、有效 HTTPS 证书。
- 前端构建镜像：Node.js 22 Bookworm。
- API 运行镜像：Node.js 22 Bookworm + Debian Python 3。
- 当前 Compose 限额：1 CPU、640 MiB 内存。
- 容器只绑定 `127.0.0.1:8300 -> 3000` 和 `127.0.0.1:8876 -> 8765`，公网只开放 Nginx 的 80/443。
- 时区：`Asia/Shanghai`。

生产密钥写入 `deploy/hybrid/server.env`，参照 `server.env.example`。至少配置任务 API Token、Worker Token、总管理员账号、管理员密码哈希和会话密钥。真实值不得写入源码、README、日志或 Git。

详细服务端步骤见 [`deploy/hybrid/README.md`](deploy/hybrid/README.md)。

## 3. 哪些代码只在 Windows Worker 运行

| 文件或目录 | 本地职责 |
| --- | --- |
| `windows_worker_sdk/protocol.py` | Worker HTTPS 协议、鉴权和连接健康状态 |
| `windows_worker_sdk/runner.py` | 领取任务、六模型并行、轮次调度、心跳、断点和幂等上传 |
| `windows_worker_sdk/contracts.py` | 回答、信源及本地分析的数据合同 |
| `windows_enterprise_worker/collectors.py` | 六个平台的生产采集入口和完整性校验 |
| `windows_enterprise_worker/analyzer.py` | 本地品牌推荐、排名、竞品和证据分析 |
| `windows_enterprise_worker/supervisor.py` | 内存、模拟器、App 进程和浏览器资源监管 |
| `windows_enterprise_worker/yuanbao_burst.py` | 元宝模拟器提问与网页正文回收 |
| `scripts/run_windows_enterprise_worker.ps1` | 常驻 Worker 启动、日志和默认并发配置 |
| `scripts/install_windows_enterprise_worker.ps1` | 注册开机登录后自动运行的计划任务 |
| `scripts/watch_windows_enterprise_worker.ps1` | Worker 心跳、进程、夸克页面和主机资源守护 |
| `runtime/worker_spool/` | 未确认上传结果的本地安全暂存，禁止删除 |
| `runtime/health/` | Worker、连接、模拟器和 Watchdog 健康状态 |

当前企业 Worker 还依赖工作区中的两个相邻目录：

- `../DouBao_Monitor_v2.0/`：豆包模拟器流水线、元宝浏览器资料和文心网页采集器。
- `../kuake/extension/`：千问在夸克浏览器中的生产扩展。

因此迁移办公室电脑时，不能只复制 `GEO_monitor3.0-main/`；必须一并迁移上述依赖，或先把依赖正式收拢进本仓库。

## 4. 六个平台实际采集方式

| 模型 | 当前执行方式 | 必要条件 |
| --- | --- | --- |
| 豆包 | MEmu 内 App 发起问题，配套浏览器流水线回收正文和信源；诊断最多三轮一组，结果逐轮入库 | 豆包模拟器在线、ADB 可用、App 已登录 |
| 腾讯元宝 | MEmu 内 App 发起问题，Chrome/CDP 回收当前轮完整正文和信源；严格一轮一轮采集 | 元宝模拟器在线、ADB 可用、账号已登录、本地 Chrome 可用 |
| 文心一言 | Windows 本机网页直采，可批量建立独立会话 | `../DouBao_Monitor_v2.0/wenxin_monitor/` 可用，账号已登录 |
| 千问 | 夸克浏览器打开 Qwen 的 `/quarkchat` 页面，通过 `../kuake/extension/` 和本机 `8765` 接收器采集 | 夸克浏览器、扩展、登录态、页面和扩展版本均就绪 |
| DeepSeek | Chrome 持久化 profile 的网页端无头采集 | Chrome 已安装，`runtime/web_profiles/deepseek/` 中账号有效 |
| Kimi | 仓库内 `kimi_chrome_extension/` 驱动专用 Chrome；每轮新建独立会话，等待正文稳定并采集真实信源，失败可续跑 | Chrome 已安装，9227 端口可用，`runtime/web_profiles/kimi-extension/` 中账号有效 |

不要把千问改成普通千问首页；当前生产采集目标是夸克浏览器中的 `https://www.qianwen.com/quarkchat`。

当前公开即时诊断会派发六个平台的独立采集任务。Kimi 由 Chrome 插件直接采集，
不再使用 DeepSeek 镜像数据，并作为第六个独立平台参与总体概率、排名、竞品和
信源统计。历史上未派发 Kimi 的旧报告仍保留原镜像口径，避免上线后改写历史结果。

## 5. Windows Worker 环境要求

### 操作系统与硬件

- Windows 10/11 x64，保持用户处于已登录桌面会话。
- BIOS/UEFI 开启 CPU 虚拟化，Windows 能正常运行两个 MEmu 实例。
- 建议至少 6 核 CPU、16 GB 内存和 30 GB 可用磁盘；六模型并发及双模拟器长期运行建议 24 GB 或更多内存。
- Worker 在内存占用达到 88% 或可用内存低于 12% 时会暂停启动新的重型子进程，等待资源恢复，防止任务因内存耗尽失败。
- 系统不能自动休眠；长期任务期间保持网络、电源和桌面登录状态。

### 软件版本

- Python：推荐 Python 3.12 x64；当前生产机已验证 Python 3.14.7。
- Node.js：前端要求 `>=22.13.0`；当前生产机为 Node.js 24.19.0、npm 11.17.0。
- Google Chrome：当前生产机已验证 152.0.7977.83。若使用显式 ChromeDriver，其主版本必须与 Chrome 匹配。
- 夸克浏览器：安装在 `C:\Program Files\Quark\quark.exe`，并允许加载本地扩展。
- MEmu：当前生产机已验证 9.5.6.1。
- ADB：当前使用 MEmu 自带 ADB 1.0.41（31.0.3），默认路径为 `D:\Program Files\Microvirt\MEmu\adb.exe`。

Python 依赖安装：

```powershell
python -m pip install -r requirements/web.txt
```

若使用 PostgreSQL/Redis 分析能力，再安装：

```powershell
python -m pip install -r requirements/database.txt
```

### 当前已验证的模拟器基线

下面是 2026-09-10 对当前生产 Worker 的实机读取结果，不代表只能使用这些 App 小版本；更换版本后必须重新做完整采集测试。

| 用途 | MEmu 实例 | ADB serial | Android | 架构 | App 包名 | 已验证 App 版本 |
| --- | ---: | --- | --- | --- | --- | --- |
| 腾讯元宝 | 0 | `127.0.0.1:21503` | Android 9 / API 28 | x86_64 | `com.tencent.hunyuan.app.chat` | 2.83.0 |
| 豆包 | 1 | `127.0.0.1:21513` | Android 9 / API 28 | x86_64 | `com.larus.nova` | 14.8.0 |

两个实例当前模拟的设备型号为 `ASUS_AI2401_A`。必须确保：

1. 两个实例都已启动，`adb -s <serial> get-state` 返回 `device`。
2. App 已登录且能正常提问，没有验证码、升级弹窗或隐私弹窗遮挡。
3. 不随意改变实例编号、ADB 端口和包名；如需改变，使用 `GEO_YUANBAO_SERIAL`、`GEO_DOUBAO_SERIAL` 和 `GEO_ADB_PATH` 覆盖默认值。
4. 不删除模拟器数据、浏览器 profile 或 `runtime/worker_spool/`。

## 6. 本地端口与进程

这些端口只用于本机进程通信，不应开放到公网：

| 端口 | 用途 |
| ---: | --- |
| 9222 | 元宝 Chrome/CDP 回收通道 |
| 9223 | 夸克浏览器远程调试通道 |
| 9227 | Kimi 插件专用 Chrome/CDP 控制通道 |
| 8765 | 千问扩展本机任务与结果接收器 |
| 9301 | 豆包受管浏览器端口，由资源监管器识别 |

服务器使用的 8300、8301、8876、8877 是蓝绿部署槽位，只绑定服务器回环地址，与办公室 Worker 的本地端口不是一回事。

## 7. Worker 配置与启动

本机需要以下不进入 Git 的文件：

- `config/worker_token.secret`：服务器分配的 Worker Token。
- 工作区根目录 `../ds_apikey.txt`：本地回答分析使用的 DeepSeek API Key。
- `runtime/web_profiles/`：DeepSeek 与 Kimi 插件专用 Chrome 的网页账号 profile。
- 两台模拟器和夸克浏览器各自的本机登录状态。

不要在命令行、截图、日志或 README 中粘贴真实密钥。

前台验证 Worker：

```powershell
cd "C:\path\to\GEO_monitor3.0-main"
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_windows_enterprise_worker.ps1
```

注册登录后自动运行的计划任务（管理员 PowerShell）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_windows_enterprise_worker.ps1
```

生产机还应每分钟运行 `scripts/watch_windows_enterprise_worker.ps1`。Watchdog 连续两次发现进程、健康文件或服务器连接异常时会重启 Worker，并负责恢复正确的千问页面和扩展运行时。

常用环境变量：

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `GEO_SERVER_URL` | `https://www.ifbcy.com/geo` | 服务器入口，必须是 HTTPS |
| `GEO_WORKER_ID` | `windows-office-01` | Worker 唯一 ID |
| `GEO_MODEL_CONCURRENCY` | `6` | 一个任务内最多并行模型数 |
| `GEO_SUBPROCESS_CONCURRENCY` | `4` | 本机重型采集子进程上限 |
| `GEO_ANALYSIS_CONCURRENCY` | `3` | 本地分析并发数 |
| `GEO_HEALTH_INTERVAL` | `15` | 模拟器和资源健康采样秒数 |
| `GEO_READINESS_TTL` | `45` | 模型 readiness 缓存秒数 |
| `GEO_ADB_PATH` | MEmu ADB 默认路径 | 自定义 ADB 路径 |
| `GEO_YUANBAO_SERIAL` | `127.0.0.1:21503` | 元宝模拟器 serial |
| `GEO_DOUBAO_SERIAL` | `127.0.0.1:21513` | 豆包模拟器 serial |

## 8. 主要业务模块

| 功能 | 主要代码 |
| --- | --- |
| 客户提交诊断 | `DiagnosisStart.tsx`、`POST /api/diagnosis` |
| 实时诊断进度 | `DiagnosisDashboard.tsx`、Worker heartbeat |
| 客户诊断报告 | `[customer]/page.tsx`、`RemoteTaskQueue.diagnosis_report()` |
| 总管理员/下级管理员 | `DiagnosisAdmin.tsx`、`doubao_dashboard_server.py`、`remote_tasks.py` |
| 报告删除、编辑、撤回 | 管理员 API、报告审计快照和 `DiagnosisAdmin.tsx` |
| 高概率品牌策略 | `remote_tasks.py`，任务创建时快照策略版本 |
| 付费用户每日监控 | `PaidMonitorCenter.tsx`、`PaidCustomerDashboard.tsx`、`remote_tasks.py` |
| 诊断抢占每日监控 | `RemoteTaskQueue.claim()`、`WorkerRunner` 控制心跳 |
| 回答问题/主题一致性 | `monitor_core/quality.py`、`collectors.py` |
| 六模型采集与完整性 | `windows_enterprise_worker/collectors.py` |
| 本地分析 | `windows_enterprise_worker/analyzer.py` |
| Worker 自动恢复 | `supervisor.py`、`watch_windows_enterprise_worker.ps1` |

更细的文件职责见 [`docs/CODEBASE_SERVICE_MAP.md`](docs/CODEBASE_SERVICE_MAP.md)，队列协议见 [`docs/SERVER_ARCHITECTURE_AND_DATA_FLOW.md`](docs/SERVER_ARCHITECTURE_AND_DATA_FLOW.md)。

## 9. 开发验证

后端完整测试：

```powershell
python -m unittest discover -s tests -v
```

队列和 Worker 核心测试：

```powershell
python -m unittest tests.test_remote_tasks tests.test_windows_worker_sdk tests.test_enterprise_worker -v
```

前端生产构建与测试：

```powershell
cd yuanbao_monitor\dashboard
npm ci
npm run build
npm test
```

发布前必须确认：后端测试通过、前端生产构建通过、`/geo/assets/` 校验通过；发布后检查客户入口、管理员入口、健康接口、Worker 心跳、六模型 readiness 和队列是否停滞。

## 10. 生产发布

优先使用零停机脚本：

```powershell
python tools/deploy_enterprise_zero_downtime.py
```

脚本会构建备用槽位、验证健康后切换 Nginx，失败时保留原槽位。不要仅凭退出码判断成功，发布后仍需执行线上健康和 Worker/队列检查。

百一电子案例位于相邻的 `../anli/`，不属于本项目主前端。构建与发布使用：

```powershell
python tools/deploy_bydz_case_panel.py
```

## 11. 数据与安全边界

- 不提交 `runtime/`、SQLite、日志、浏览器 profile、Cookie、Token、API Key 或密码哈希。
- 不删除服务器 Docker 卷、历史报告、本地 Worker spool 或模拟器数据。
- 不绕开持久化队列直接执行客户任务。
- 不伪造模型成功、正文、信源、概率、排名或登录状态。
- 修改概率、排名、正文或信源逻辑时，必须保证六模型和所有报告字段数学一致，并补回归测试。
- 上传失败只重传相同 `request_id`；不得因为网络失败再次提问制造重复数据。
- 服务器和 Worker 时间应保持 NTP 同步；健康判断应允许亚秒级时钟偏差。
