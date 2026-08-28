# 代码与服务地图

## 1. 线上必须文件

| 文件/目录 | 服务职责 | Windows Worker 是否依赖 |
| --- | --- | --- |
| `doubao_dashboard_server.py` | API、静态报告、任务管理页、认证和路由 | 只依赖其 HTTP 合同 |
| `monitor_core/remote_tasks.py` | SQLite 队列、租约、心跳、结果、报告聚合 | 是，作为协议权威参考 |
| `doubao_env_loader.py` | 服务端环境配置加载 | 否 |
| `deploy/hybrid/` | 服务器 Docker 构建、Compose、入口 | 否 |
| `yuanbao_monitor/dashboard/` | 客户诊断页和报告面板 | 否 |

服务器不需要 Chrome、Cookie、Scrapling、Playwright、模型登录或 DeepSeek Key。

## 2. 当前 Mac 采集代码

| 文件/目录 | 作用 | Windows 重写决策 |
| --- | --- | --- |
| `geo_chrome_extension/` | 旧三模型 Chrome 插件 | 不作为新 Worker 依赖 |
| `web_collectors/remote_worker.py` | 旧 Python Worker 与协议参考 | 仅参考协议/字段 |
| `web_collectors/browser.py` | Cookie 隐身浏览器管理 | 重新实现 |
| `web_collectors/collector.py` | 页面输入、正文、信源采集 | 重新实现 |
| `web_collectors/scrapling_yuanbao.py` | 元宝 Scrapling 试验 | 可评估，不直接迁移 |
| `web_collectors/loop.py` | 本地 JSONL、状态、数据库同步 | SDK 已重写关键部分 |
| `monitor_core/cdp_chat.py` | CDP 页面自动化辅助 | 不作为 Windows 必选项 |

## 3. 数据与分析代码

| 文件 | 作用 | 迁移建议 |
| --- | --- | --- |
| `monitor_core/quality.py` | 回答质量判断 | 可复用规则 |
| `monitor_core/ingestion.py` | 回传记录归一化 | 保持字段兼容 |
| `monitor_core/analytics.py` | 模型、品牌、信源和覆盖率统计 | 服务器报告/长期分析参考 |
| `monitor_core/product_analysis.py` | 批量产品分析辅助 | 可迁移到 Windows |
| `save_doubao_refs.py` | 产品/品牌分析与已有 AI 调用 | 拆出纯本地 Analyzer |
| `product_ai_worker.py` | 异步产品分析 Worker | 若 Windows 需要独立队列可复用思路 |
| `monitor_core/database.py` | 可选 PostgreSQL 存储 | Windows 本地可不启用 |

## 4. 新 Windows 代码

| 文件 | 作用 |
| --- | --- |
| `windows_worker_sdk/protocol.py` | HTTPS Worker API 客户端 |
| `windows_worker_sdk/contracts.py` | Collector/Analyzer 数据合同与完整性校验 |
| `windows_worker_sdk/runner.py` | 并行模型调度、断点、本地落盘、幂等上传 |
| `windows_worker_sdk/collector_template.py` | 新抓取和分析实现模板 |
| `windows_worker_sdk/__main__.py` | 常驻 Worker 命令入口 |
| `windows_worker_sdk/.env.example` | 不含密钥的配置模板 |

## 5. 运行时目录

| 位置 | 所有者 | 内容 | Git |
| --- | --- | --- | --- |
| 服务器 `deploy/hybrid/runtime/` | 服务器 | SQLite 队列、日志、报告缓存 | 禁止 |
| Mac `runtime/` | 旧 Worker | 虚拟环境、结果、状态、浏览器临时资料 | 禁止 |
| Windows `windows_worker_sdk/data/` | 新 Worker | 本地原始结果、错误、重传状态 | 禁止 |

## 6. 典型调用链

```text
DiagnosisStart.tsx
  -> POST /api/diagnosis
  -> RemoteTaskQueue.create_public_diagnosis
  -> SQLite(remote_tasks)

Windows WorkerRunner.poll
  -> ServerClient.claim
  -> Collector.collect_new_conversation
  -> Analyzer.analyze
  -> LocalSpool.append
  -> ServerClient.submit_result
  -> RemoteTaskQueue.accept_result
  -> SQLite(remote_task_results)

DiagnosisDashboard.tsx
  -> GET /api/diagnosis/<report_key>
  -> RemoteTaskQueue.diagnosis_report
  -> 逐模型/逐轮正文、信源、完整性、推荐率
```

## 7. 修改边界

- 只改 Windows 抓取实现：遵守 `contracts.py`，服务器无需变化。
- 改回传字段或隐私边界：必须同时改 `remote_tasks.py`、API 文档、报告前端和回归测试。
- 改队列状态：必须覆盖暂停、恢复、取消、租约过期和幂等重传测试。
- 改客户报告：必须保留逐轮正文、全部信源和完整性可追溯性。

