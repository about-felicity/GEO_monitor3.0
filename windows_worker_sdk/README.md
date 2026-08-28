# Windows Worker SDK

这是现有 GEO 服务端协议的可运行骨架，不包含任何浏览器抓取或验证码绕过逻辑，也不依赖旧 Chrome 插件。

需要在 Windows 单独实现两个工厂：

- `GEO_COLLECTOR_FACTORY`：`model_id -> Collector`
- `GEO_ANALYZER_FACTORY`：`() -> Analyzer`

SDK 提供 HTTPS 认证、领取、租约心跳、三个模型并行、每模型轮次串行、断点跳过、先本地 JSONL 后幂等上传、暂停/取消和完成回传。

完整接入说明见 `docs/WINDOWS_WORKER_REWRITE_GUIDE.md`，协议字段见 `docs/SERVER_ARCHITECTURE_AND_DATA_FLOW.md`。

