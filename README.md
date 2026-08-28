# 五模型 GEO 网页监控

项目现在只保留网页采集方案：豆包、腾讯元宝、文心、DeepSeek、夸克均通过本机浏览器网页端提问，采集完整回答与外部引用信源。MuMu、逍遥、ADB、Appium、手机端提问、局域网结果回传等链路已移除。

## 当前功能

- 五模型统一问题管理、轮次配置、开始/停止控制
- 每次任务使用独立 Chrome 隐身会话，模型之间互不干扰
- Cookie 只从进程环境读取，不落盘；正式任务默认隐身无头运行
- 自动提交问题、等待回答稳定、抓取正文和引用链接
- 五个模型都使用各自正式聊天网页；豆包额外复用引用面板提取逻辑
- JSONL 原始结果留档、断点续跑、统一统计面板
- 可选 PostgreSQL 同步、产品识别、品牌/信源和内容分析
- 可选混合部署：用户在公网面板创建任务，本机 Worker 主动领取并回传进度与结果
- Chrome 三模型插件可直接复用已登录的豆包、腾讯元宝和文心一言标签页，不导出 Cookie

## 使用方法

1. 首次运行 `一键部署并启动.bat`，安装 Python、Chrome 和前端依赖。
2. 在启动采集器的同一进程环境中临时设置对应模型 Cookie（见 `web_collectors/README.md`）。
3. 打开 `http://127.0.0.1:3000`，在面板中维护各模型问题并启动采集。
4. 以后可直接运行 `一键启动五模型监控.bat`；Cookie 失效时只需更新临时环境变量。

三模型公网诊断优先使用 `geo_chrome_extension/`。在
`chrome://extensions/` 加载该目录后，打开并登录三个模型网页，再从插件弹窗连接服务器。

采集结果保存在 `runtime/web_results/`，运行进度保存在 `runtime/web_state/`。浏览器临时资料只在 `runtime/web_sessions/` 存活，采集器关闭后立即删除；这些目录均不应提交版本库。

## 目录职责

- `web_collectors/`：五模型网页配置、浏览器会话、采集器和统一循环
- `model_plugins/`：五个模型接入统一面板的轻量插件
- `monitor_core/`：调度、标准化、数据库、统计与质量校验
- `yuanbao_monitor/dashboard/`：统一 React 管理面板
- `doubao_ref_extension/`：豆包回答和引用信源解析逻辑
- `geo_chrome_extension/`：豆包、腾讯元宝、文心一言统一 Chrome 采集插件
- `scripts/operations/`：部署、启动及局域网面板配置
- `requirements/`：网页采集与分析依赖
- `tests/`：后端、采集协议和前端回归测试
- `runtime/`：运行时结果、浏览器资料、日志和状态（自动生成）

## 可选数据库

不配置数据库也能采集并在面板查看 JSONL 统计。若需要跨模型产品分析和长期数据库查询，请设置用户环境变量 `MONITOR_DATABASE_URL`；采集完成后会自动同步 PostgreSQL，并启动产品分析工作进程。

更细的采集模块说明见 `web_collectors/README.md`。

## Windows Worker 迁移

迁移到另一台 Windows 电脑时，服务器保持不变，新抓取逻辑不得依赖旧 Chrome
插件。协议骨架位于 `windows_worker_sdk/`，迁移前请依次阅读：

- `docs/SYSTEM_INVENTORY_2026-08-28.md`
- `docs/SERVER_ARCHITECTURE_AND_DATA_FLOW.md`
- `docs/WINDOWS_WORKER_REWRITE_GUIDE.md`
- `docs/CODEBASE_SERVICE_MAP.md`

Linux/Docker 服务器部署方法见 `deploy/server/README.md`。腾讯元宝在服务器方案中使用 Scrapling 临时隐身浏览器。

低配置服务器推荐使用“本地采集 + 服务器面板”模式，见
`deploy/hybrid/README.md`。服务器不运行浏览器；本机双击
`启动远程任务采集代理.bat` 后主动领取用户任务。
