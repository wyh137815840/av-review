# library-scan 完整一轮运行报告（2026-11-12 08:40:20 → 18:55:10）

观察方式：不重启进程，只读 `progress.json`；后台 watcher 每 60s 采样，`updated_at` 从 `08:40:20.080124` 变为 `18:55:10.768017` 时判定 pass 边界。
日志：`/workspace/ops/logs/library-round-20261112-120603.jsonl`、`/workspace/ops/logs/library-round-20261112-181259.jsonl`（边界记录）。

## 1. 本轮结果

| 项 | 值 |
| --- | --- |
| pass 窗口 | **10h14m50s**（08:40:20 → 18:55:10） |
| inventory | **2439/2439** 全部核对完 |
| done | 57（**本轮无新增完成**） |
| failed | 11 → **162**（新增 156 条：152 条「退出 1」+ 4 条 incomplete review） |
| submitted | +1：`259LUXU-1006`（08:40:20，ed2k；115 任务已 `finished/下载成功`，待下轮 process） |
| stuck / blocked / backoff | 0 / 无 / 无 |
| 新 pass | 18:55:10 立即开始，inventory_total 2439 → **2336**（失败码在 30min 重试窗口内被排除） |

## 2. 关键根因（P0）：library-scan 进程没有 LLM runtime，导致 ready 阶段全部「退出 1」

pass 末尾 inventory 完成、进入 `_run_ready` 后，152 个 ready 番号在约 1 分钟内全部失败，reason 统一为 `退出 1`（时间戳都记为 pass 起点 08:40:20）。

复现（容器内直接跑）：

```
docker exec moviepilot python3 /config/scripts/av_auto.py process ATID-698
SKIP clean ... / SKIP strm ...
ERROR: 找不到 LLM 运行时文件 /config/scripts/av-auto-fs/llm_runtime.json。
       请用官方入口 av_auto（会临时放入 Magpie caller-keys），不要把手写 Key 写进脚本。
FAIL ATID-698: process 停在非 0（1）。...
```

机制：

- `av_auto process` 的 frames 预检需要 LLM runtime（`cmd_frames` 调 `_load_llm_runtime()`）。
- Unraid 入口 `/mnt/user/appdata/findav/bin/av_auto` 只在 `frames|review|grok-review|intake --frames/--review` 时临时写入 `llm_runtime.json`，并在 **EXIT trap 里 `rm -f`**（第 6–32 行）。
- `library-scan` 是长跑进程，启动时没有 runtime；跑到 10 小时后处理 ready 时，文件早已不存在，于是每个 `process` 都 `_die` → `SystemExit(1)` → `退出 1`。
- 当前 `/config/scripts/av-auto-fs/llm_runtime.json` 确认不存在。

这不是数据坏：`ATID-698` 的 `_offline_layout` 正常（dest_keepers=1、strm 存在），frames 目录为空，只是 process 在预检前就退出了。

**建议修复（P0）**：二选一，优先 A：
- A. library-scan 启动时（或 Unraid 入口对 `library-scan`）就安装 LLM runtime，并保持到进程退出；入口脚本把 `library-scan` 加入 `need_llm=1`。
- B. library-scan 启动时预检 runtime，缺失就立即失败（避免先浪费 10 小时 inventory 再批量失败）。
- 另：156 条失败会在 `fail_retry_sec`（30min）后重试，但如果不修 runtime，下一轮 ready 阶段还会同样失败。

## 3. 异常汇总

### 3.1 外部 per-title 驱动停摆（13:33 → 18:55，约 5h22m，且仍在停）

- 今日 review 原本稳定 3–5 部/10min；最后一部是 **13:33 `XVSR-217`**。
- 之后无 frames/review/process 子进程；reviews 计数停在 **114**、incomplete 11、Emby 595 均不再变化。
- grokbot-vm `http://192.168.1.17:18765/health` 正常，`vision_busy.busy=false`（不是 vision 锁）。
- 外部编排器（grokbot-vm 侧，通过 SSH 调 `/mnt/user/appdata/findav/bin/av_auto`）停止派发；仓库里无对应调度脚本/日志可查。需要到 .17 上排查。

### 3.2 DeepSeek 间歇空响应 → 11 条 incomplete review

`deepseek/deepseek-flash` 返回 HTTP 200 但 `content` 为空（`parsed=None`），`_chat_retry` 3 次仍空 → `complete=false`；`gpt-6-luna` 正常。条目：

```
SSIS-899(10:27) SSNI-647(11:18) SSPD-142(11:43) SSPD-155(11:47)
START-023(12:27) START-331(12:39) START-442(12:45) VRTM-091(13:02)
WAAA-674(13:19) WANZ-823(13:25) WAT-010(13:27)
```

同一部 `SSIS-899` 复测时又能返回完整 JSON（parsed 成功），说明是上游间歇性空响应，不是固定尺寸/prompt 问题。这些条目目前**没有自动重审**，会一直停在 incomplete。

**建议（P1）**：DeepSeek 空响应重试 5 次 + 退避；library-scan/process 对 `complete=false` 增加重审队列。

### 3.3 孤儿 in_progress

- `1KRMV-279`：11-11 12:17 提交（ed2k），一直 `in_progress(bucket=run)`，115 任务列表里已无对应任务，属孤儿状态；建议标记 failed 让下轮重试或人工清理。

### 3.4 资源/性能

- inventory 速率约 **4 番号/分钟（≈15s/番号）**，单 pass 约 10h；瓶颈是每个番号都要查 115 布局（`_offline_layout`）。
- frames 目录 **577** 个、192MB；`shfs /config /media` 使用 11%，`overlay /` 66%，磁盘正常。
- 无 blocked/backoff/quota/磁盘告警。

## 4. 结论

1. 本轮 library-scan **正常跑完 inventory，但在 ready 阶段系统性失败 152 个**，直接原因是长跑进程没有 LLM runtime（P0）。
2. 外部 per-title 编排器在 13:33 后停摆，导致本轮后半段没有新 review/Emby 更新；需在 grokbot-vm 侧排查。
3. DeepSeek 间歇空响应造成 11 条 incomplete review，需加重试与重审机制。
4. 修复 P0 后，把 156 条失败重试（或重启一次带 runtime 的 library-scan），本轮结论才能转化为实际入库。
