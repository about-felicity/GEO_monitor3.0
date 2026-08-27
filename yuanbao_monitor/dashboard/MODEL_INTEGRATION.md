# 模型接入约定

前端不维护模型注册表；模型目录由后端扫描 `model_plugins/*/plugin.py` 后通过 `/api/models` 返回。

新增网页模型时：

1. 在 `web_collectors/config.py` 增加站点地址和选择器。
2. 在 `web_collectors/questions/` 增加问题文件。
3. 在 `model_plugins/<model-id>/plugin.py` 继承 `WebModelPlugin`，只声明模型元数据和问题文件。
4. 为专属网页结构增加采集测试；通用网页交互继续放在 `web_collectors/collector.py`。

模型插件会自动获得问题管理、启停、JSONL 统计、账号检查、断点续跑和可选 PostgreSQL 同步能力。
