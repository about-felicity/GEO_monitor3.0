# 启动与运维脚本

- `operations/`：根目录快捷方式调用的实际实现，包括统一面板启动、首次部署和防火墙配置。`open_source_debug_browser.bat` 仅在普通 HTTP 无法读取引用文章正文时作为可选浏览器渲染入口。

日常操作优先使用项目根目录中以中文命名的“一键启动”快捷方式；它们提供稳定入口，内部再调用这里的实现。

服务器混合部署时，本机 Worker 使用以下命令管理：

```bash
scripts/operations/start_remote_worker.sh
scripts/operations/stop_remote_worker.sh
tail -f runtime/logs/remote_worker.log
```

Worker 由项目内的 `screen` 会话托管，PID、会话套接字和日志均保存在 `runtime/`，不会在项目外创建运行文件。
