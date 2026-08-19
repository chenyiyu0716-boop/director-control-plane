# Status Contract v1

下一阶段（Clock Proof 今日里程碑已完成之后）：**先定协议，不换手。**

三个仓以后无论谁当 agent（Cursor 窗、cursor-agent、临时 WB），都只须遵守本协议。钟继续读落盘文件；Chief 只判 `state`，不修复。

## 字段

```yaml
service: julius | director | control-plane
timestamp: RFC3339          # 本轮写入时刻
last_success: RFC3339 | null  # 上一轮真正做成事的时刻
age_minutes: number           # 可由钟重算；手写则须与 timestamp 一致
state: OK | WARNING | CRITICAL
reason: string                # 给人看的一句原因，禁止空
```

`OK`：本轮按合同完成（或 idle 且诚实）。  
`WARNING`：活着但受阻（等批复、网关、过期未写）。  
`CRITICAL`：不能可信工作（生产不可达、契约文件缺失、连续失败）。

正文（任务细节）仍可写在 `HOURLY_STATUS.md` 后半；**机器只认上述块**（文件头 YAML，或同目录 `STATUS.json`）。v1 二选一即可，三仓须相同。

## 不在 v1

换手 / Agent Runner；hy3；自动重启；Chief 改产品代码。

## 采纳

状态：**DEFINED**，未要求三仓今晚改格式。钟仍用现有 `HOURLY_STATUS.md` mtime 探针。Owner 点名后再让各仓分身改写入。
