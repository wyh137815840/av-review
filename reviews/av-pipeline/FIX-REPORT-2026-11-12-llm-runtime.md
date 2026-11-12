# P0/P1 修复报告：library-scan LLM runtime + DeepSeek 重试（2026-11-12 19:30–19:45）

承接 `RUN-REPORT-2026-11-12-library-pass.md`：上一轮 pass 在 ready 阶段 152 个番号统一「退出 1」，根因是长跑的 library-scan 进程没有 LLM runtime。本轮按优先级修复并验证。

## P0：LLM runtime 对长跑进程不可用（已修复并实测）

### 根因
- `av_auto process/frames` 的预检需要 `llm_runtime.json`。
- Unraid 入口只在 `frames/review/grok-review/intake --frames/--review` 时临时写入该文件，并在 **EXIT trap 删除**；`library-scan` 启动时没有 runtime，跑到 10 小时后处理 ready 时文件早已不存在 → 每个 `process` 立即 `_die` → `SystemExit(1)` → `退出 1`。
- 复现：`docker exec ... av_auto.py process ATID-698` → `ERROR: 找不到 LLM 运行时文件 ...llm_runtime.json`。

### 修复
1. **入口脚本** `/mnt/user/appdata/findav/bin/av_auto`（备份 `av_auto.bak-llm-20261112-*`）：
   - 每次调用使用独立 runtime：`av-auto-fs/llm_runtime.<pid>.json`，通过 `-e AV_AUTO_LLM_FILE=...` 传入容器，退出即删（避免并发/长跑互删）；
   - `need_llm` 增加 `process`、`library-scan`；
   - 透传 `AV_AUTO_CORPUS_FILE` 到容器。
2. **library_scan.py**：`cmd_library_scan` 启动即校验 runtime（fail-fast），缺失立即报错，不再先跑 10h inventory。
3. **av_auto.py**：`_chat_retry` 重试 3 → **5**（P1）。
4. 生产备份：`config/scripts/backups/av_auto.py.bak-llm-*`、`library_scan.py.bak-llm-*`；仓库修复 **PR #5 已合并**（av-review main `0dfb3ac`）。

### 部署 + 验证
- 停止旧 library-scan（PID 243642，SIGTERM 未退、最终 kill），清理孤儿后，通过新入口重启：
  - 语料：从 findav DB 重建 `corpus-20261112-193232.tsv`（T=2899 / E=2910 / M=377，mode 600，读后即删）；
  - 新进程 PID **307301**，持有独立 runtime `llm_runtime.1507083.json`（mode 600）；
  - `library-scan: 开始核对 115 上 2373 个番号`，启动正常，无 runtime 报错。
- **P0 端到端实测**：`/mnt/user/appdata/findav/bin/av_auto process ATID-698`
  - 抽帧 8/8、预检 DeepSeek 正常、终审 `deepseek-flash=pass` + `luna=pass`；
  - `AUTO promote ATID-698: /av-auto/ATID-698 → /AV/ATID-698`，strm 写入，Emby `Media/Updated 204; Refresh AV 204`。
  - 说明长跑进程的 ready 处理路径已恢复。
- 当前：library-scan inventory **19/2373**（19:32 起），failed=162、done=57；Emby 597、本地 strm 目录 673、云端 `/AV` 571。

## P1：DeepSeek 间歇空响应与重审（已改善）

- `_chat_retry` 重试提到 5 次（指数退避），提高空响应时的成功率。
- **实测重审**：`av_auto review SSIS-899`（原 incomplete，DeepSeek 空响应）→ 本次 DeepSeek 返回完整 JSON、两家一致 pass，`review.json complete=true`，并自动 promote；Emby 已刷新。
- 剩余 incomplete **10 条**（可同样用 `bin/av_auto review <code>` 重审；library-scan 下一轮也会把它们当 work 重跑）：
  `SSNI-647, SSPD-142, SSPD-155, START-023, START-331, START-442, VRTM-091, WAAA-674, WANZ-823, WAT-010`
- 尚未做：把「重审 incomplete」做成常驻队列（当前依赖下一轮 inventory 或人工触发）。

## 孤儿清理

- `1KRMV-279`（11-11 提交、115 任务已不存在）：重启前已从 `submitted/in_progress` 移除并写入 `failed`（until +30min），下一轮会重新评估/交磁。

## .17 外部编排器

- 仍然停摆：最后一部外部 review 是 **13:33 `XVSR-217`**；之后没有 frames/review/process（我们 19:34/19:35 的手工重审除外）。
- 访问受限：`ssh root@192.168.1.17` 返回 `Permission denied (publickey,password)`；HTTP `:18765` 控制接口需要 `secrets/api.token`（未持有）。**需要你在 .17 上排查编排器**；本机无法修复。

## 待观察 / 下一步

1. 等新 library-scan 跑完 inventory（约 10h），确认 ready 阶段不再批量「退出 1」。
2. 视需要批量重审剩余 10 条 incomplete（每次 `bin/av_auto review <code>`，成本很小）。
3. 外部编排器恢复后观察吞吐；若仍停，需要在 grokbot-vm 侧查进程/定时器。
4. 建议后续把 `library-scan` 的启动纳入一个受管入口（带语料生成 + runtime），避免手工重启依赖。
