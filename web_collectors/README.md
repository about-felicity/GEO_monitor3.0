# 网页采集器

这里是五模型共用的浏览器采集层。

- `config.py`：网站地址、输入框、回答区、登录提示和站内域名配置
- `browser.py`：Chrome 隐身启动、临时用户目录、内存 Cookie 注入和 CDP 连接
- `collector.py`：网页提问、回答稳定判断、正文与引用提取
- `scrapling_yuanbao.py`：腾讯元宝临时 Scrapling 隐身浏览器、登录检查和循环提问
- `loop.py`：问题排期、断点续跑、JSONL 保存及可选数据库同步
- `login.py`：用可见临时浏览器验证内存 Cookie 是否仍有效
- `questions/`：五个模型的默认问题文件

单模型调试示例：

```powershell
python -m web_collectors.login --model doubao
python -m web_collectors.loop --model doubao --questions-file web_collectors/questions/doubao.txt --rounds-per-question 1 --results runtime/web_results/doubao_results.jsonl --state runtime/web_state/doubao.json
```

隐身会话不复用磁盘登录资料。启动服务前，将浏览器扩展导出的 Cookie JSON
数组放入当前进程的临时环境变量，例如 DeepSeek 使用
`MONITOR_DEEPSEEK_COOKIES_JSON`。另外四个变量分别为
`MONITOR_DOUBAO_COOKIES_JSON`、`MONITOR_YUANBAO_COOKIES_JSON`、
`MONITOR_WENXIN_COOKIES_JSON`、`MONITOR_QUARK_COOKIES_JSON`。不要把真实
Cookie 写入 `.env`、脚本、日志或聊天；进程退出后环境变量随之消失。

每个采集器都为 Chrome 创建 `runtime/web_sessions/` 下的独立临时 profile，
关闭时先结束浏览器进程，再删除 profile。正常完成、面板停止和终止信号都会
进入同一清理路径。

网站改版后，优先只更新 `config.py` 中对应模型的选择器；确有专属交互时再在 `collector.py` 增加小范围适配。
