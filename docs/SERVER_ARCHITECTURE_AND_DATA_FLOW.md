# 服务器架构、任务协议与数据流

本文是保持现有服务器不变时，Windows 抓取/分析 Worker 必须遵守的接口合同。真实实现以 `monitor_core/remote_tasks.py` 和 `doubao_dashboard_server.py` 为最终依据。

## 1. 总体边界

```text
客户浏览器
  POST /geo/api/diagnosis
        |
        v
服务器 Nginx -> Python API -> SQLite 任务队列
                                 ^       |
                  HTTPS 出站轮询 |       | 任务租约
                                 |       v
                       Windows Worker
                抓取 -> 本地原始落盘 -> 本地分析
                                 |
                         幂等上传报告记录
                                 v
                      SQLite 结果/客户报告
```

服务器负责接收诊断参数、生成随机报告路径、排队与租约、任务控制、保存事件/在线状态/报告记录，以及实时生成客户报告。Windows Worker 负责模型登录、新会话提问、正文和信源采集、本地原始落盘、本地分析、幂等回传和响应暂停/取消。

## 2. 身份与密钥

| 用途 | 请求头 | 服务端变量 |
| --- | --- | --- |
| Worker API | `Authorization: Bearer <token>` | `MONITOR_WORKER_TOKEN` |
| 管理控制台 API | `X-Monitor-Task-Token: <token>` | `MONITOR_TASK_API_TOKEN`（也兼容 Worker Token） |

Worker Token 只放在 Windows 用户环境变量或本机受限配置中，绝不写入源码、日志、截图或 Git。任务领取返回的 `lease_token` 是短期任务租约，只保存在进程内存。

## 3. 客户创建任务

`POST /api/diagnosis` 无需管理密钥，正文：

```json
{
  "brand_name": "外星人",
  "product_name": "外星人电解质水",
  "question": "推荐一款大量出汗后适合喝的电解质饮料"
}
```

服务端为公开诊断派发豆包、腾讯元宝、文心一言、千问和 DeepSeek 五个真实采集模型，轮数由总管理员服务设置决定，当前默认 3 轮。报告中的 Kimi 暂时使用 DeepSeek 镜像数据；Kimi 真实采集器仍可用于显式任务和付费监控。接口返回随机 32 位十六进制 `report_key`、`/geo/<report_key>` 路径和任务对象。同一 IP 五秒内重复提交会被拒绝。报告不使用品牌名或产品名作为路径。

## 4. Worker 轮询与租约

### 4.1 领取任务

`POST /api/worker/claim`

```json
{
  "worker_id": "windows-office-01",
  "readiness": {
    "doubao": {"ready": true, "message": "已登录"},
    "yuanbao": {"ready": true, "message": "已登录"},
    "wenxin": {"ready": true, "message": "已登录"}
  }
}
```

无任务时返回 `task: null`。成功时任务含 `id`、`lease_token`、`models`、`questions`、`rounds`、`question_mode`、品牌/产品/报告 key，以及 `completed_rounds` 和 `model_progress`。默认租约 900 秒。服务器同一时间只允许一个活动任务；一个任务内部是否并行三个模型由 Windows Worker 决定。

### 4.2 心跳

`POST /api/worker/tasks/<task_id>/heartbeat`

```json
{
  "lease_token": "领取任务时返回的租约",
  "model": "doubao",
  "question": "当前问题",
  "message": "豆包第 2/3 轮正在采集信源"
}
```

响应含 `cancel_requested` 和 `pause_requested`。等待长回答时也必须续租，不能只在轮次开始时发心跳。

### 4.3 回传一轮结果

`POST /api/worker/tasks/<task_id>/result`，最大请求约 8 MiB：

```json
{
  "lease_token": "...",
  "model_id": "doubao",
  "request_id": "task-确定性哈希",
  "record": {
    "collector_model": "doubao",
    "model_id": "doubao",
    "round": 1,
    "question": "...",
    "reply": "完整回答正文",
    "web_body": "完整回答正文",
    "sources": [{"title": "来源标题", "url": "https://example.com/article"}],
    "body_capture_complete": true,
    "body_capture_origin": "new-worker-dom",
    "expected_source_count": 1,
    "source_capture_complete": true,
    "source_capture_origins": {"dom": 1, "network": 0},
    "page_url": "模型会话地址",
    "capture_mode": "windows_browser",
    "started_at": "ISO-8601",
    "finished_at": "ISO-8601",
    "products": [],
    "brands": [],
    "analysis": {
      "mode": "local_chrome_extension",
      "engine": "windows_worker",
      "recommended": false,
      "rank": null,
      "matched_terms": [],
      "analyzed_at": "ISO-8601"
    }
  }
}
```

`request_id` 必须由 `task_id + model_id + 轮次 + 问题` 确定性生成，网络重试使用同一个值。服务端客户报告依赖正文、信源、完整性和分析字段，因此当前协议会在服务器 SQLite 中保存报告副本。Windows 本地 JSONL 是原始事实来源。若未来要求正文/信源绝不离开 Windows，必须新增仅上传汇总值的协议并同步改造服务端，不能只改 Worker。

服务端现有报告代码用 `analysis.mode == "local_chrome_extension"` 判断分析是否完整。新 Windows Worker 为保持服务器不变，暂时保留这个兼容值，并用 `analysis.engine == "windows_worker"` 标识真实执行引擎；以后升级服务端协议时再更名。

### 4.4 结束

`POST /api/worker/tasks/<task_id>/finish`

```json
{"lease_token": "...", "status": "completed"}
```

状态只允许 `completed`、`failed`、`cancelled`、`paused`；失败时可增加 `error`。暂停应先安全落盘当前轮，再结束；取消后不再开始下一轮。

## 5. 状态机与恢复

```text
queued -> running -> completed
             |  |-> failed
             |  |-> cancelled
             |  `-> paused -> queued -> running
             `-- 租约过期 -> queued（等待重新领取）
```

- Worker 重启后重新 claim，并依据 `completed_rounds` 跳过已上传轮次。
- 本地先写 JSONL/SQLite，再上传服务器。
- 上传超时后不能重新提问，只能用相同 `request_id` 重传落盘结果。
- 验证码、登录失效或页面结构变化必须记录为错误并暂停/失败，不得伪造成功。

## 6. 管理接口

均需 `X-Monitor-Task-Token`：

- `GET /api/tasks?limit=100`、`GET /api/tasks/<id>`、`GET /api/tasks/<id>/results`
- `POST /api/tasks/<id>/pause|resume|cancel|rerun|clear|delete`
- `POST /api/tasks/<id>/results/<request_id>/delete`
- `POST /api/tasks/cleanup`
- `GET /api/workers`

诊断任务只在报告数据库保存一次，不额外写服务器 JSONL。

## 7. 服务端数据表

| 表 | 作用 |
| --- | --- |
| `remote_tasks` | 参数、状态、进度、租约、客户报告 key |
| `remote_task_events` | 面板实时日志 |
| `remote_task_results` | 每轮幂等记录及 `record_json` |
| `remote_workers` | Worker 心跳、在线状态、模型 readiness |
| `remote_login_requests` | 旧登录工作流兼容数据 |

## 8. 验证顺序

先检查 `/geo/api/health` 和公开页面；再创建 1 模型 × 1 轮管理任务验证领取、心跳、落盘、分析、回传和完成；只有在明确授权真实测试时，才执行一次完整公开诊断，核对五个真实采集模型、Kimi 镜像字段、报告概率和逐轮证据。
