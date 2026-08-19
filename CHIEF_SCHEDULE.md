# 本机小时轮错峰（Asia/Shanghai）

**钟（第一阶段）**：`/Users/pojian/chief-clock/` · launchd 日历 · 只观察、写日志、退出。不拉起 agent。

| 窗 | 每小时 | 谁执行 | 钟的 job |
|---|---|---|---|
| Director 分身 | :10 | 本地窗口（手） | `director_status` |
| Julius 分身 | :25 | 本地窗口（手） | `julius_status` |
| Chief 仓分身 | :40 | 本地窗口（手） | `control_status` |
| Chief 组合观察 | :50 | 钟写 `status/portfolio.md`；主窗只在打开时读 | `chief_review` |

飞书通道：`run_feishu_control.py` 约每 60 秒扫 `.workbuddy`，不是这口钟。

Cursor 对话里的 `sleep` **不再当 daemon**。主窗不是守夜人。
