# chenyiwei 回复检索

本仓库用于 GitHub Pages 静态发布。

- 入口文件：`index.html`
- 内容范围：会计视野论坛 CPA业务探讨版，chenyiwei 回复检索
- 自动更新：GitHub Actions 每小时刷新最近 3 天公开答疑并自动提交到 `main`
- 公开地址：<https://patrickusgaap-sudo.github.io/chenyiwei-reply-search/>

刷新遇到 HTTP 524 等临时网关错误、限流或连接故障时，每页最多请求 4 次，
重试前分别等待 5、10、20 秒；连接超时为 10 秒，单次请求最多 60 秒。
持续失败或接口返回格式异常时，任务仍报错，并保留原有 `index.html`。
Actions 每次刷新前运行回归测试；本地可执行 `python -m unittest discover -s tests -v`。
