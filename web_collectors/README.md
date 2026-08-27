# 网页采集器

这里是五模型共用的浏览器采集层。

- `config.py`：网站地址、输入框、回答区、登录提示和站内域名配置
- `browser.py`：Chrome 启动、独立持久化用户目录和 CDP 连接
- `collector.py`：网页提问、回答稳定判断、正文与引用提取
- `scrapling_yuanbao.py`：腾讯元宝持久化 Scrapling 隐身浏览器、登录检查和循环提问
- `loop.py`：问题排期、断点续跑、JSONL 保存及可选数据库同步
- `login.py`：首次登录或登录失效时打开可见浏览器
- `questions/`：五个模型的默认问题文件

单模型调试示例：

```powershell
python -m web_collectors.login --model doubao
python -m web_collectors.loop --model doubao --questions-file web_collectors/questions/doubao.txt --rounds-per-question 1 --results runtime/web_results/doubao_results.jsonl --state runtime/web_state/doubao.json
```

网站改版后，优先只更新 `config.py` 中对应模型的选择器；确有专属交互时再在 `collector.py` 增加小范围适配。
