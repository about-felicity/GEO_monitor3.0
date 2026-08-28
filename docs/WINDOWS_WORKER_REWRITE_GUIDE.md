# Windows 抓取与分析 Worker 重写指南

目标是在另一台 Windows 电脑重新实现浏览器抓取，不依赖 `geo_chrome_extension`，同时保持现有服务器、客户页面和报告协议不变。

## 1. 可复用与必须重写

可直接复用：

- `windows_worker_sdk/`：服务器协议、任务调度、幂等、断点和本地落盘骨架；
- `monitor_core/quality.py`：正文质量检查思路；
- `monitor_core/remote_tasks.py`：服务器合同的权威实现；
- `monitor_core/analytics.py`、`monitor_core/ingestion.py`：字段归一化和统计规则；
- `save_doubao_refs.py`、`monitor_core/product_analysis.py`：现有产品分析思路。

必须在 Windows 重写：登录态管理、每轮新会话、输入与发送、完成判断、正文/全部信源抓取、验证画面检测与人工接管，以及本地品牌/产品分析。不得引用 `geo_chrome_extension/service-worker.js` 作为运行依赖；它只保留为历史参考。

## 2. 环境

推荐 Python 3.12 x64，不复制 Mac `.venv`：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

协议 SDK 只使用标准库。浏览器框架由新实现自行选择并固定版本；如评估 Scrapling，应在 Windows 虚拟环境安装发布版，不复制仓库内 `Scrapling/` 或 Mac 浏览器缓存。

```powershell
[Environment]::SetEnvironmentVariable('GEO_SERVER_URL', 'https://www.ifbcy.com/geo', 'User')
[Environment]::SetEnvironmentVariable('GEO_WORKER_TOKEN', '实际密钥', 'User')
[Environment]::SetEnvironmentVariable('GEO_WORKER_ID', 'windows-office-01', 'User')
```

Token、DeepSeek Key 和登录信息不能提交 Git。

## 3. Collector 合同

新模块暴露 `create_collector(model_id)`，返回对象实现：

- `check_ready() -> dict`：只检测，不弹窗、不抢焦点；
- `collect_new_conversation(question, round_number, progress) -> CapturedAnswer`：每轮新会话；
- `close()`：释放资源。

硬性验收：

1. 每个模型固定一个后台浏览器上下文或窗口，不随轮次无限创建标签页；
2. 每轮在模型内部新建会话，不向旧会话续问；
3. 正文为空、仍在生成、问题未发送、落入验证码时不能标记成功；
4. `expected_source_count` 是页面/网络可观察的期望数，信源按最终 URL 去重；
5. 正文和信源完整性字段只能依据可验证条件置真；
6. 不自动破解验证码；检测到验证时暂停并提供人工处理入口，恢复后重做当前轮；
7. 后台运行，不调用会激活窗口或抢系统焦点的调试 API。

## 4. 本地分析合同

分析模块暴露 `create_analyzer()`，对象实现：

```python
analyze(model_id, question, brand_name, product_name, captured) -> AnalysisResult
```

至少返回推荐布尔值、排名、匹配词、产品、品牌和可审计详情。DeepSeek Key 只在 Windows 本机使用。分析失败不能丢弃正文，也不能再次向模型提问；应保留本地结果并重试分析。

## 5. 本地数据

默认位于 `windows_worker_sdk/data/`，已被 Git 忽略：

```text
data/
  results/<task_id>.jsonl    每轮原始+分析记录，先写后传
  errors/<task_id>.jsonl     可恢复错误与状态摘要
  state/                     可选游标/重传队列
```

截图、HTML、HAR 等中间产物放任务专属临时目录，使用后立即删除且不得写出项目目录。需要保留排错材料时必须由操作者明确选择。

## 6. 接入步骤

1. 拉取 `codex/windows-worker-handoff`。
2. 创建 Windows 虚拟环境并设置服务器、Worker Token 和 Worker ID。
3. 复制 `collector_template.py` 到新包，实现三模型 Collector。
4. 实现本地 Analyzer。
5. 设置 `GEO_COLLECTOR_FACTORY=your_package.collectors:create_collector`。
6. 设置 `GEO_ANALYZER_FACTORY=your_package.analysis:create_analyzer`。
7. 运行 `python -m windows_worker_sdk --once` 做认证和空队列测试。
8. 在控制台创建 1 轮任务，核对正文、信源、完整性和分析。
9. 完成 3 模型 × 8 轮测试后，再配置 Windows 任务计划程序常驻启动。

## 7. 验收清单

- Worker 20 秒内显示在线，readiness 与真实登录状态一致；
- 三模型并行，每个模型内部串行；每轮全新会话；
- 刷新客户页/控制台不影响 Worker；
- 暂停安全落盘，恢复跳过已完成轮；
- 网络中断只重传，不重复提问；
- 24/24 完成，正文、信源、分析完整数均可见；
- 报告指标可逐轮追溯；
- Token、Cookie、API Key、profile 和原始数据均未进入 Git。

