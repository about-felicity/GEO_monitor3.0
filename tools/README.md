# 工具目录

这里全部是按需手动运行的工具，不会被日常监控服务自动启动。

- `analysis/`：报表、偏好分析、模型训练与评估。
- `maintenance/`：数据审计、迁移、回填、去重和修复。运行前应先备份数据。
- `development/`：压测和实验脚本，不用于生产启动。

请在项目根目录使用模块方式运行 Python 工具，例如：

```powershell
python -m tools.analysis.analyze_doubao_sources
python -m tools.maintenance.audit_brand_mention_integrity
```

这种调用方式会稳定地使用项目根目录中的正式模块和数据路径。
