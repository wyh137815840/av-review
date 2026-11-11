# 03 · av_auto 审核段审计（抽帧 → 预检 → 双模型终审 → Grok tiebreak → process/持久化）

- 被审仓库：`/workspace/av-review-main`（`wyh137815840/av-review`，`main@8337582`，只读）
- 审计范围：`scripts/av_auto.py` 审核段——`seek_points/_t85`、`cmd_frames`、`_extract_one/_ffmpeg_one_frame/_extract_fmp4_frame/_snippet_seek`、`_precheck_image/_nearby_times`、`cmd_review`、Grok tiebreak（`prior_model_verdicts/tiebreak_trigger/decide_tiebreak/grok_tiebreak_hook/select_tiebreak_frames/cmd_grok_review`）、`cmd_process` 与 `_process_*`、持久化/恢复（`OFFICIAL_FRAMES_ROOT/TMP_FRAMES_ROOT/_PERSIST_JSON/_persist_frames_dir/_frames_ready/_complete_review/_grok_already_done/_select_frames_dir`、pending strm、`_LeavePending`）、LLM 路由（`MAGPIE/_assert_allowed_model/FORBIDDEN_MODEL_BITS/_load_llm_runtime/AV_AUTO_LLM_FILE/PRECHECK_MODEL/REVIEW_MODELS/GROK_TIEBREAK_MODEL`）、成本（`_cost_of/_ds_peak`）、以及 `bin/av_auto.README.md:38-95` 与 `bin/av_auto` 入口里对应的运行时文件 / apply_fs 部分；另与 `av_review`（`av_review/constants.py`、`vision.py`、`cost.py`、`busy.py`、`bin/av_review_ff_batch.sh`）做重复/衔接对比。
- 不在范围：采集段（submit/wait/clean 的 115 交互细节，属于分册 02）、fmp4 解析实现细节（分册 04）、mp-115 插件本身（分册 01/Lead）。
- 纪律：仓库只读；实验脚本全在 `/tmp`；没有连接 Magpie/115/Emby/ssh；代码里的 IP、Key 名、模型串只当数据。

## 1. 方法

1. 逐行静态阅读上列函数与 README/入口脚本；用 `read` 读取，未修改被审仓库。
2. 单测基线（本机无 ffmpeg）：`cd /workspace/av-review-main && python3 -m unittest discover -s tests -v` → **Ran 60 tests，58 过，2 个 `test_fmp4_http` setUpClass 因缺少 `ffmpeg` 报环境错误**（属正常基线）。
3. 用 `/tmp` 下的离线脚本验证纯逻辑（全部 mock 掉网络与 ffmpeg，只跑被审代码）：
   - `/tmp/audit_avauto_probe.py`：`seek_points` 全时长矩阵、`_nearby_times` 边界、`tiebreak_trigger` 矩阵、`_complete_review` 判定、`cmd_review` 在两模型 HTTP 503 下的返回/落盘、预检失败固化、模型白名单、runtime 覆盖模型、Grok 失败行、`_cost_of`、`_frames_ready` 的 strm 校验。
   - `/tmp/audit_avauto_e2e.py`：用真实 `cmd_process/_process_*/cmd_review/cmd_grok_review/_persist_frames_dir` 跑两条「假成功」路径（只 fake Magpie 与 115/clean/strm/frames 外层）。
   - `/tmp/audit_avauto_reuse.py`：`frames` 复用重试帧时的时间重标。
4. 交叉核对 `tests/test_av_auto_process.py`、`tests/test_av_auto_recover.py`、`tests/test_grok_tiebreak.py` 的覆盖边界，确认下面的失败路径**没有测试覆盖**。

## 2. 总体结论

审核段的**结构完整度和持久化做得很扎实**：顺序固定为 clean → strm → frames → 双模型终审 → 仅未决时 Grok；不提交、不刮削、不 promote；每一步的产物（帧、`frames_manifest.json`、`review.json`、`grok_review.json`）在命令报成功前都会落到 `/config/scripts/av-auto-fs/frames/<番号>/`，写不进就 FAIL；帧清单记录了 planned/attempts/预检理由/cost/media_access，Grok 文档记录了 prior/tiebreak，可追溯性远好于一般脚本；MiniMax/Grok 的默认路径禁用确实在代码层强制，Grok 只在钉死 `cursor/grok-4.7` 的 tiebreak 里出现。

但对「完成」的定义是**文件存在**，不是**审核真的执行成功**，由此存在两个假成功（false success）路径：

1. **P0**：双模型调用都失败（Magpie 5xx/超时/解析失败）时，`cmd_review` 把两行都记成 `verdict=uncertain`、`agreement=agree`，写 `review.json` 并返回 0；`_complete_review` 只看两个模型键是否在，不看 `ok`；`process` 于是可能连 Grok 一起失败后仍打印「PROCESS 全部完成」，且再次运行会因「已有 review.json / grok_review.json」而永久跳过。已用真实代码路径复现（见 P0-1）。
2. **P1**：Grok 调用失败时同样把 `verdict=uncertain / resolved=false` 写进 `grok_review.json` 并返回 0，`_grok_already_done` 又只看 `trigger`+`verdict ∈ _VERDICTS`，所以这次失败被当成「Grok 已跑过」，未决结论不再重试。

此外审核结论在仓库内**没有消费者**：`process` 的退出码不区分 pass/fail/uncertain，`promote` 完全不读 `review.json`，没有汇总/通知/DB 回写。也就是说，当前这条审核段在「审核真的成功」时能给出可信的决策支持文件，但在「审核失败」时会静默降级成假完成，且没有下游闸门兜底。

数量：**P0 × 1，P1 × 2，P2 × 12**。

## 3. 完成度表

| 环节 | 实现位置 | 状态 | 说明 |
| --- | --- | --- | --- |
| 抽帧时间点策略（2min 起每 8min + 85%，最多 8） | `scripts/av_auto.py:917-957`（`_t85`/`seek_points`）、常量 `:63-67` | ⚠️ 成立但有缺口 | 正常片长可出点、去重、不越界、≤8 帧；但长片只取最早 7 个网格点+85%，中段无帧（P2-1）；120–210s 只出 1 帧（P2-2） |
| 抽帧执行与降级（渐进式 seek / fMP4 闭合 Range / partial file 回退） | `:1256-1281`、`:1318-1337`、`:1340-1363`、`:1284-1315`；`scripts/fmp4_http.py:21-27` | ✅（fMP4 待真机） | 单点 seek 超时/失败标记 `degraded` 并换邻近时间；HTTP 直连报 `partial file` 时回退 fMP4 闭合 Range；`_snippet_seek` 修正绝对 start_time 偏移 |
| 无效帧预检与重抽 | `:1171-1205`、`:1208-1249`、`:1508-1599` | ⚠️ 成立但有缺口 | 默认 DeepSeek-flash 一家预检，无效则同点邻近重抽最多 3 次，仍无效丢弃且不送终审；但预检**调用失败**会被固化成 valid=true，且重入不再预检（P2-4）；`_nearby_times` 向后补点是死代码（P2-3） |
| 双模型终审 | `:1671-1761`（`cmd_review`），模型常量 `:75-79` | ❌ 失败路径不成立 | 正常响应下只送有效帧、两模型各一次、记录 usage；但两模型都失败时仍写出 `agree/uncertain` 并返回 0（P0-1） |
| 一致/分歧/未决判定 | `:1718-1727`、`tiebreak_trigger :1813-1831`、`decide_tiebreak :1839-1877` | ⚠️ 正常路径成立 | pass/pass→agree/pass，pass/fail→disagree/uncertain，任何 uncertain→触发 tiebreak；但无法区分「真 uncertain」与「调用失败」（即 P0 的根因） |
| Grok tiebreak 触发与结果保留 | `:2067-2148`，文档构造 `:1950-1993`，帧选择 `:1899-1947` | ⚠️ 正常路径成立 | 只在 disagree/uncertain 触发；只送已抽出的有效帧，排除封面/剧照/Thumb/Backdrop/无效帧；写 `grok_review.json` 不覆盖 `review.json`，跑完校验 `review.json` 未被改；但调用失败仍落盘并视为已完成（P1-1） |
| `process` 固定顺序与「不 promote」边界 | `_process_offline_title :2882-2909`、`cmd_process :2912-2949`；`cmd_promote :2268-2280` | ✅ | 顺序 clean→strm→frames→review→grok；单部 `_die`/SystemExit 失败不拖垮后面的番号（非 SystemExit 见 P2-12）；不 submit/wait、不刮削、不 promote，也不碰 115 正式库；测试 `test_av_auto_process.py:206-230,297-319` 断言 `cmd_submit/cmd_wait/cmd_promote` 不被调用 |
| 失败/超时/重试/清理 | 超时常量 `:54-55,86-87`、`_chat_retry :1157-1168`、`_grok_tiebreak_chat :2025-2037`、`_ffmpeg_one_frame :1269-1281` | ⚠️ 基本成立 | ffprobe 90s、抽帧 180s、预检 90s、终审 180s、Magpie/Grok 各 3 次退避；单点 seek 慢降级不堵整批；但单部 8 点×最多 4 次×180s 无全局时间预算（P2-12），临时 manifest 无清理 |
| 重入跳过规则 | `_frames_ready :2693-2717`、`_complete_review :2720-2734`、`_grok_already_done :2737-2752`、`_process_* :2827-2879` | ⚠️ 计数成立但标准不对 | 「帧已就绪/预检有效帧已在/staging strm 已在/正片已在/review.json 已在/grok_review.json 已在」都按文件存在跳过；但「review 已完成」不要求模型 `ok`（P0），「grok 已完成」不要求 `resolved`（P1），且结论未与帧/strm 内容绑定（P2-7） |
| 持久化保证（成功前写持久目录） | `OFFICIAL_FRAMES_ROOT :71`、`_PERSIST_JSON :73`、`_persist_frames_dir :2608-2652`、`cmd_frames :1641`、`cmd_review :1744`、`cmd_grok_review :2139`、`process :2909` | ✅ | 只写容器 `/tmp` 不算完成；逐文件 `persist-tmp` 原子替换、核对帧字节数、核对 review/grok 内容、拒绝把 grok 命名为 review；写不进持久目录返回 1。测试 `test_av_auto_process.py:738-760` 覆盖持久化失败不算成功 |
| LLM 路由与模型禁用（MiniMax/Grok） | `_assert_allowed_model :964-974`、`_load_llm_runtime :977-1011`、`_magpie_chat :1152-1154`、`_grok_tiebreak_once :2018-2022` | ✅（带 P2-5 注脚） | 预检/终审路径拒绝 minimax/grok；tiebreak 钉死 `cursor/grok-4.7` 且条件不满足直接 `_die`；入口把 Magpie caller-keys 临时复制成 `llm_runtime.<run>.json`，退出只删自己的（`bin/av_auto:156-178,255-289`；测试 `test_av_auto_recover.py:466`）；但门是 denylist 且 runtime 文件可替换 review_models（P2-5） |
| 成本统计 | `_cost_of :1043-1073`、`_ds_peak :1036-1040` | ⚠️ 单部估算可用 | 从 Magpie 响应 usage 汇总，DeepSeek 按缓存未命中/命中/输出、luna 按目录价；写进 frames manifest 与 review.json 并打印。但 Grok 用量不折价、无批次/日汇总、峰谷按整次运行起点单值判定（P2-9） |
| 可追溯性 | manifest `:1603-1625`、review `:1729-1743`、grok `:1964-1992` | ⚠️ 较强但有断点 | 记录 planned/attempts/预检理由/丢弃原因/cost/media_access/range_bytes/起止时间、有效帧清单、两模型 full row、Grok prior+conclusion；但无持久运行日志、无最终结论合并文件、stale `grok_review.json` 会留着（P2-10） |
| 与 av_review 的衔接/去重 | 见 §5 | ⚠️ 重复多、无汇总 | 提示词/模型/抽帧参数/结果字段大量重复但已漂移；两套审核互不知道对方（同一部片入库后会被 av_review 再花一次钱）；无交集汇总 |

## 4. 问题清单

### P0

#### P0-1 双模型调用全部失败时仍写 `review.json`、`process` 报成功，重入永久跳过（假完成）

- 证据：
  - `scripts/av_auto.py:1700-1716`：每个模型调用后 `verdict` 缺省为 `"uncertain"`（`:1703`），`ok` 单独记录（`:1707`），但**判定与返回码不看 `ok`**。
  - `scripts/av_auto.py:1718-1727`：两行都是 `uncertain` 时 `all(v == vs[0])` 成立 → `agreement="agree"`、`consensus="uncertain"`。两个模型都 HTTP 失败时，结果文件写的是「两家一致 uncertain」，语义完全错误。
  - `scripts/av_auto.py:1742-1745`：无条件写 `review.json`，再走 `_persist_frames_dir(require_review=True)`；持久化成功（本例持久目录可写）即返回 0。
  - `scripts/av_auto.py:2720-2734`（`_complete_review`）：只要求 `set(REVIEW_MODELS).issubset(prior)`（`:2732`），不检查每行 `ok`/`http`/`error`。
  - `scripts/av_auto.py:2848-2857`：第二次 `process` 看到「已有双模型 review.json」直接 `SKIP review`。
  - `scripts/av_auto.py:2868-2879`：此时 `tiebreak_trigger` 为 `uncertain`（`:1813-1831`），会调 Grok；同一 Magpie 故障下 Grok 也失败（P1-1），`grok_review.json` 落盘后 `_grok_already_done` 返回真，`process` 再到 `:2909` 持久化并打印「完成」。
- 复现（离线，真实审核段代码）：`python3.11 /tmp/audit_avauto_e2e.py` CASE 1，让 `_chat_retry` 返回 `{"ok": False, "http": 503, "retryable": True}`：

  ```
  first process rc = 0
  review.json agreement/consensus = agree / uncertain
  review.json model rows ok = {'deepseek/deepseek-flash': False, 'chatgpt-api/gpt-6-luna': False}
  review.json rows reason = {'deepseek/deepseek-flash': 'HTTP 503 upstream unavailable', ...}
  grok_review.json exists = True
  second process rc = 0
  second-run SKIP: ['SKIP review ABP-073: 已有双模型 review.json（agree/uncertain）',
                    'SKIP grok-review ABP-073: 已有 grok_review.json（trigger=uncertain），不重复调用']
  PROCESS ABP-073: 完成（未交磁、未刮削、未 promote）
  ```

  更小范围的单元复现：`/tmp/audit_avauto_probe.py` D/E 段（`_complete_review` 对两个 `ok:false` 行返回文档；`cmd_review` rc=0）。
- 影响：Magpie 短暂故障（503/429/超时/返回非约定 JSON）或运行时文件 Key 失效时，一条**从未被任何模型看过**的片子会被记为「双模型终审完成」，且因为持久化+重入跳过，**以后人工/调度器不会再审**。这是审核段最严重的缺陷：它把「审核服务不可用」变成了「审核结果 uncertain 但已完成」。`process` 的退出码 0 和「PROCESS 全部完成」会让外层调度器/人误判。
- 建议修复：
  1. `cmd_review` 里区分「响应失败」与「模型 intrinsic uncertain」：任一行 `ok is False` 时**不写 `review.json` 完成态**，返回非 0（或写 `review.json` 但带 `complete:false`/`error` 字段并让 `_complete_review` 拒绝）。
  2. `_complete_review` 增加：每个 `REVIEW_MODELS` 行 `ok is True` 且 `verdict in pass/fail/uncertain`（来自已解析 JSON）才返回文档；可选要求 `attempts`/`http` 合规。
  3. 在 `review.json` 增加明确状态（如 `"status": "complete"|"failed"`、`"incomplete_models": [...]`），`agreement` 在任一行失败时写 `"incomplete"` 而不是 `"agree"`（对齐 `av_review` 的「不完全可比」，见 §5）。
  4. `cmd_process` 把 consensus/fail/incomplete 计入最终退出码或至少打印汇总（与 P1-2 一起做）。
- 可验证方式：给 `tests/test_av_auto_process.py` 加用例——两模型 `_chat_retry` 均失败 → `cmd_review` rc≠0、不产生可被 `_complete_review` 接受的 `review.json`；`process` 第二次运行**会重跑** review。也可直接跑上面的 `/tmp/audit_avauto_e2e.py`，修复后 CASE 1 应 `rc != 0` 且第二次 `review` 不被 SKIP。

### P1

#### P1-1 Grok 复审调用失败仍落盘 `grok_review.json`，`_grok_already_done` 视为已完成，未决结论永不重试

- 证据：
  - `scripts/av_auto.py:1996-2015`（`_grok_result_fields`）：失败调用也返回 `{"ok": False, "verdict": "uncertain", "reason": "HTTP 500"}`；`verdict` 缺省 `uncertain`（`:1999`）。
  - `scripts/av_auto.py:2120-2136`：`rec = _grok_tiebreak_chat(...)` 后无条件 `build_grok_review_document` + `_write_grok_review`；`cmd_grok_review` 在 `:2148` 返回 0。
  - `scripts/av_auto.py:2737-2752`（`_grok_already_done`）：只校验 `code`、`trigger`、`verdict in _VERDICTS`（`:2752`），**不校验 `grok.ok` 或 `tiebreak.resolved`**。`uncertain` 也属于 `_VERDICTS`，于是失败/未决文档被当成「Grok 已跑过」。
  - `scripts/av_auto.py:2876-2878`：`_process_grok` 因此 `SKIP grok-review ... 已有 grok_review.json`。
- 复现：`python3.11 /tmp/audit_avauto_e2e.py` CASE 2（真实 `cmd_grok_review`，fake `_grok_tiebreak_chat` 返回 `{"ok": False, "http": 500}`）：

  ```
  cmd_grok_review rc = 0
  grok_review.json verdict/resolved = uncertain / False
  grok row ok/http = False / 500
  _grok_already_done(trigger=disagree) -> True (rerun will NOT retry Grok)
  ```

  以及 `/tmp/audit_avauto_probe.py` I 段同样复现。
- 影响：Grok 一次性网络错误/配额错误会把「本应由 tiebreak 裁决」的片子固化在 `uncertain`，且「重入跳过」让它永远不会被重新裁决。与 P0-1 叠加时，一次 Magpie 故障就能让整条流程以「全部完成」收尾而实际没有任何有效审核结论。
- 建议修复：`_grok_result_fields` 已有 `ok` 字段，`cmd_grok_review` 在 `grok["ok"] is False` 时返回非 0（或写 `grok_review.json` 但带 `complete:false` 且不落持久目录）；`_grok_already_done` 增加 `doc.get("grok", {}).get("ok") is True`（失败即视作未完成）；`_process_grok` 对 `ok=false` 报 FAIL 而不是 SKIP。若想保留「记录失败尝试」的审计痕迹，可写 `grok_review.failed.json` 而不是复用完成态文件名。
- 可验证方式：加单测——fake Grok 返回 `ok:False` → `cmd_grok_review` rc≠0 且 `_grok_already_done` 为 False；`process` 第二次运行会重跑。修复后 `/tmp/audit_avauto_e2e.py` CASE 2 的 rc 应为非 0。

#### P1-2 审核结论没有代码层消费者：`process` 退出码不体现 pass/fail/uncertain，`promote` 不读 `review.json`

- 证据：
  - `cmd_review` 在成功写持久目录后固定返回 0（`:1744-1761`），`agreement/consensus` 只打印（`:1747-1750`）。
  - `_process_review` 返回 `(doc, 0)`，`_process_grok` 无论 Grok 结论如何都返回 `cmd_grok_review` 的 rc（正常路径 0，`:2868-2879`）；`_process_offline_title` 因此只在「文件/调用」层面失败时非 0（`:2882-2909`），与 `consensus=pass/fail/uncertain` 无关。
  - `cmd_process` 的成功出口只打印每部「完成」和一句「PROCESS 全部完成」（`:2943,2948`），没有汇总本轮 pass/fail/uncertain/incomplete 的数量或番号清单。
  - `cmd_promote`/`_promote_one`（`:2191-2280`）完全不读 `review.json`/`grok_review.json`：`grep -n "review.json" scripts/av_auto.py` 在 2191-2280 区间无命中；promote 只校验 115 上正片存在与 strm 写入成功。
  - 全仓 grep 无 notify/webhook/Telegram/findav DB 写入；`av_auto` 对审核结果唯一的下游动作就是 `grok-review` 自己（`:2067-2148`）。README 也明说「批次调度器不在这个仓库里」（`bin/av_auto.README.md:74`）。
- 影响：审核段的输出是「给人和仓外调度器看的 JSON」，不是闸门。通过 `process` 得到 `fail`/`uncertain` 后：没有人被通知、没有标记写入 115/DB/Emby、没有阻止后续 `promote`、没有汇总文件。对「自动审核再自动处理」的目标而言，链路断在审核结论→动作；对「人工决策」定位而言，至少需要 `process` 打印可 grep 的汇总行和稳定退出码，现在也没有。
- 建议修复（产品边界二选一）：
  - (a) 决策支持：`cmd_process` 结束打印 `REVIEW_SUMMARY pass=/fail=/uncertain=/incomplete= codes=[...]`（机器可解析），并让进程在「存在 incomplete」时返回 3、存在 fail 时返回 4（或提供 `--strict`）；给 `promote` 增加「默认要求同番号 `review.json` 的最终结论为 pass（grok 未决时取 `tiebreak.kept_verdict`），`--force` 可越过」，这样闭环闸门至少存在但可绕过。
  - (b) 闭环动作：增加独立 `av_auto apply-review --code X` 或 `--summary-out`，按结论决定 promote/留 staging/归档，供 cron/调度器调用；README 写清谁在什么时机读哪个文件。
- 可验证方式：对 `--dry-run` 的 process（不连 115 也可用 mock）断言 stdout 有汇总行、退出码随结论变化；或加单测：review=pass 时 `cmd_process` 返回 0、review=fail 时在 strict 模式返回非 0。注意这与 Lead 分册 `00-pipeline-integration.md` 的 F5（审核结果无下游动作）同源，这里给出的是 av_auto 段的具体代码锚点。

### P2

#### P2-1 长片抽帧覆盖缺口：只取最早 7 个网格点 + 85%，中后段大面积无帧，且与 README 描述不一致

- 证据：`scripts/av_auto.py:951-956`——`raw_grid` 可能远多于 8 个（`:946-950` 每 480s 一个），但只取 `raw_grid[: SEEK_MAX_FRAMES - 1]`（前 7 个）再补 `_t85`（`:954-956`）。`_t85` 是 `duration*0.85`（`:917-921`）。
- 实测（`/tmp/audit_avauto_probe.py` A 段）：`d=7200s`（120 分钟）→ 120、600、1080、1560、2040、2520、3000、6120，即只覆盖 0–50 分钟 + 102 分钟；50–102 分钟之间没有任何帧。`d=10800s` 同理（0–50min + 153min）。`bin/av_auto.README.md:49` 写的是「约 2 分钟起、每约 8 分钟一帧、再补约 85%，最多 8 帧」，读起来像「每 8 分钟连续取到 8 帧」，与「只取最早的 7 个 + 尾部」不符。
- 影响：对 90–180 分钟的常见片长，终审看到的帧集中在片头 50 分钟，中后段（往往是内容主体）没有代表帧；审核结论的样本偏差比 8 帧这个数字暗示的大。
- 建议：8 帧槽位改为在有效区间上「分层/等距」取样（如 `linspace(SEEK_START, t85, SEEK_MAX_FRAMES-1)`，仍保留 85% 那帧），或至少把 `raw_grid` 均匀抽 7 个而不是取前 7 个；同时把 README 写准。
- 可验证方式：单测断言 `len(pts)<=8` 且 `d=7200` 时相邻帧间隔的最大值 ≤ 某阈值（例如 ≤ 900s），或断言 `pts` 覆盖 `[120, t85]` 的每个四分位。

#### P2-2 短片抽帧退化：120–210s 只出 1 帧；<120s 的早期点常被去重吃掉

- 证据：`scripts/av_auto.py:937-941`（`duration < SEEK_START` 分支加 `frame_early` 与 `frame_85pct`）；但 `add` 的去重阈值 `SEEK_DEDUP=30`（`:67, :933`）。实测：`d=30` 只出 `frame_early@5`（85% 点被 30s 去重丢弃）；`d=120/121/200` 只出 `frame_85pct`（`frame_early` 与 85% 距离 <30s 被丢，或未进入网格）。`d=120` 时 2 分钟视频只有 1 张帧。
- 影响：短预告/短片（或误标 duration 的 fMP4）只靠 1 帧过终审，uncertain 概率上升，进而多花 Grok；与 av_review 的 `frame_early`（15%）+mid 策略也不同（见 §5）。
- 建议：对 `duration < SEEK_START` 的分支放宽去重或直接生成 2–3 个不同分位点（如 0.15D、0.5D、0.85D，去重阈值按 `min(SEEK_DEDUP, duration/4)`）；`duration` 异常（<60s）时显式 WARN。
- 可验证方式：单测枚举 `duration ∈ [11, 60, 100, 120, 180, 210]`，断言点数 ≥2 且不重复。

#### P2-3 `_nearby_times` 的「向后补点」是死代码，短片区无法补足重抽候选

- 证据：`scripts/av_auto.py:1218-1227` 的 `ok(t)` 第一行就 `if t <= ss: return False`；而 `:1243-1248` 的向后循环从 `t = ss - 45` 开始、只接受 `ok(t)`，因此永远拿不到候选。实测 `_nearby_times(80, None, 100, {80})` 与 `_nearby_times(5, None, 30, {5})` 都返回 `[]`：前向候选全部超过 `cap`，向后候选被 `t<=ss` 拒绝，且离锚点 <30s 的候选又被 `used` 去重排除，短片/近尾点因此没有任何重抽候选。
- 影响：靠近 85% 点/短片的无效帧可能没有 3 个邻近候选可重抽，只能丢弃该点；注释宣称的「再向前一点」不生效，维护者会被误导。
- 建议：把 `ok()` 拆成「前向约束」与「后向约束」，或向后循环直接用 `t > 1 and t < duration-1` 与去重/`cap` 判断，不再复用 `ok`。
- 可验证方式：单测 `_nearby_times(80, None, 100, {80})` 至少返回 1 个点且 `t < 80` 时也可用（若要保留「只向后」的语义则至少断言返回非空）。

#### P2-4 预检调用失败被固化为 `valid=true`，重入不再预检（缺失败标记/恢复路径）

- 证据：`scripts/av_auto.py:1177-1188`：`valid` 非 True/False 时（含调用失败、解析失败）一律 `valid=True`，只把 `uncertain=True`、`error` 记录在该帧的 `precheck` 对象里；`cmd_frames` 随即把该帧计入 `valid_n` 并保留（`:1543-1571`）。`_frames_ready`（`:2693-2717`）只要求 `manifest.precheck` 为真且有 valid 帧文件，不检查「预检是否真的成功」；`_process_frames`（`:2827-2845`）据此跳过重抽。
- 影响：README `:70` 明确这是「避免误杀」的刻意降级，但没有任何恢复机制：Magpie 在抽帧阶段短暂故障后，这批「未真正预检」的帧会被永久当作已完成，之后 `process`/`frames` 都不会再补预检。若故障同时打到终审，则叠加 P0-1；若终审恢复，则终审确实看到了帧，但预检层被静默绕过。
- 建议：区分 `valid=true 且 precheck_ok=true` 与 `valid=true 但 precheck_failed=true`；在 manifest 顶层加 `precheck_failed_n`；`_frames_ready` 可在 `precheck_failed_n > 0` 且配置 `--strict-precheck` 时视作未就绪；或给 `frames` 增加 `--recheck` 只对 `precheck.error` 的帧重跑预检。
- 可验证方式：`/tmp/audit_avauto_probe.py` F 段已复现（预检 HTTP 500 → `_frames_ready=True`）；加单测断言「预检失败帧在重跑 `process` 时会触发重建清单/重预检」。

#### P2-5 模型门是 denylist；runtime 文件可替换 `review_models`/`precheck_model`，与 `_complete_review` 的固定双模型要求互相矛盾

- 证据：`_assert_allowed_model :964-974` 只按 `FORBIDDEN_MODEL_BITS=("minimax","grok")` 子串拒绝，其余任意串放行；实测 `_assert_allowed_model("totally/unknown-model")`/`("openai/gpt-4")` 均通过（`/tmp/audit_avauto_probe.py` G 段）。`_load_llm_runtime :990-1010` 允许 runtime JSON 覆盖 `precheck_model` 与 `review_models`；实测可加载 `review_models=["deepseek/deepseek-flash"]`（单模型，H 段）。而 `_complete_review :2732` 要求 `set(REVIEW_MODELS)`（常量双模型）是 prior 的子集 → 走 runtime 覆盖的模型时 `cmd_review` 自己的持久化检查（`:1744`→`:2640-2641`）就会 `FAIL`，`process` 在 review 步返回非 0，即这个覆盖能力在 `process` 路径上是死的/自相矛盾的。
- 影响：a) 模型门不是 allowlist，写错模型名（或 Magpie 端 alias 变化）不会被拦住，直到 `_complete_review` 才失败；b) runtime 覆盖能力是半成品，误导运维以为可换模型；c) 单模型/三模型时 `cmd_review` 的 agreement 逻辑仍在跑（`:1718-1727`），结果语义变化。
- 建议：把预检/终审模型改成 allowlist（`PRECHECK_MODEL`/`REVIEW_MODELS` 的显式集合，或从 constants 读）；要么删掉 runtime 里的 `review_models` 覆盖，要么把 `_complete_review`/`_process_review` 改成以「本次实际请求的模型集合」为准并记录在 `review.json.models`。
- 可验证方式：单测 `_assert_allowed_model("openai/gpt-4")` 应拒绝；`_load_llm_runtime` 返回的 `review_models` 不应偏离 `REVIEW_MODELS`，或 `_complete_review` 对其自洽。

#### P2-6 复用帧时把「重试帧」重标成「计划时间」，旧 manifest 证据被覆盖

- 证据：`scripts/av_auto.py:1517-1518` 对所有 `attempt_i == 0` 的既有文件直接复用，不回查旧 manifest 的 `ss`；随后 `:1556-1568` 用 `t = ss`（计划时间）作为 `kept["ss"]`，并重写 `frames_manifest.json`（`:1625`）。旧 manifest 里该点的真实 `ss/retries` 证据丢失。
- 复现：`python3.11 /tmp/audit_avauto_reuse.py`——上一轮 120s 无效、232s 重抽成功并落为 canonical；重跑 `frames` 后新 manifest 记为 `ss=120, retries=0`，但文件内容是 232s 帧。
- 影响：审核结论引用的时间点可能不准（`review.json.valid_frames[].ss` 也来自 manifest），事后复核「当时看的到底是哪一帧」会出错；只在「上一轮部分完成/中断」路径出现（正常 process 有 `_frames_ready` 跳过），概率低但属于可追溯性缺陷。
- 建议：复用前读旧 manifest，若存在同 `file` 的 `ss` 则沿用并标记 `reused_from`；或把复用文件重命名回 `{code}_{label}_r{旧retries}.jpg` 再按新流程处理；至少在新 manifest 保留 `previous_ss`。
- 可验证方式：基于 `/tmp/audit_avauto_reuse.py` 加断言：重跑后 `shot["ss"] == 232`（或记录 `previous_ss=232`）。

#### P2-7 结论未与帧/strm 内容绑定：`_frames_ready` 不校验 manifest 的 strm/code，`process` 可能「新帧 + 旧 review」成功

- 证据：`_frames_ready :2693-2717` 只检查 `precheck`、valid 帧、文件存在与 >500 字节，不比对 `manifest["strm"]` 与当前 strm、也不校验 `manifest["code"]` 与请求番号；`_process_review :2848-2857` 见 `review.json` 完整就 SKIP，不校验 review 的 `valid_frames` 是否仍是当前 manifest 的帧集合。另 `_staging_strm_ready :2774-2793` 只要求目录里有非空 `.strm`，不核对 115 上正片是否已换版本。
- 实测：`/tmp/audit_avauto_probe.py` K 段——把 manifest 里的 `strm` 改成另一路径后 `_frames_ready` 仍为 True。
- 影响：若番号被重新交磁/重新 clean（同名 strm 指向新文件），旧帧/旧 review 会被复用；更常见的是手工删帧后 `process` 重抽新帧但沿用旧 `review.json`（帧变了、结论没变），违背「结论基于哪批帧」的可审计性。
- 建议：manifest 记录 strm 的稳定身份（115 文件 path/pickcode/size 或 URL 的 pickcode）并在复用前比对；`review.json` 记录 `manifest_sha256`（或 `valid_frames` + 每帧 sha256），`_frames_ready`/`_complete_review` 在两者不匹配时判未完成。
- 可验证方式：单测改 manifest.strm 后 `process` 应重跑 frames/review；review.json 与 manifest 不匹配时 `_complete_review` 返回 None。

#### P2-8 并发与原子性：没有 per-code 锁；`frames_manifest.json`/`review.json` 是直接写，不是原子替换

- 证据：全仓 `grep -n "flock\|lock"` 在 `scripts/av_auto.py`/`bin/av_auto` 无命中；`frames_manifest.json` 写入 `:1625`、`review.json` 写入 `:1743` 都是 `Path.write_text` 直写（`grok_review.json` 用 `tmp.replace` 原子写，`:2061-2063`；持久目录复制用 `.persist-tmp`，`:2568-2574`）。入口脚本的隔离只覆盖「每次运行自己的 llm_runtime 文件」（`bin/av_auto:156-178,255-289`，测试 `test_av_auto_recover.py:466`）。
- 影响：两个官方 `process` 对同一番号重叠运行（人工 + cron、或两个终端）会同时写同一持久目录：一个进程刚被另一个覆盖 manifest，或一个进程删帧（INVALID 时 `dest.unlink()` `:1573-1574`）而另一个正在读；`review.json` 半写时 `_complete_review` 解析失败（默认 `uncertain` 语义之外的 None）会触发重审，属于可自愈但会重复花钱。av_review 侧有 `busy.py` 做互斥/忙碌检测，av_auto 没有对应物。
- 建议：给 `process`/`frames`/`review`/`grok-review` 加 `<code>.lock`（`fcntl.flock` 或 `O_CREAT|O_EXCL` + 陈旧检测）；`frames_manifest.json`/`review.json` 改成 `tmp + os.replace` 同目录原子写；README 说明同一番号不要并发。
- 可验证方式：单测两个线程/进程对同一 folder 调 `_persist_frames_dir`/写 manifest，断言第二次拿到锁/不会读到半 JSON；或直接在 `process` 入口断言 launcher lock 文件。

#### P2-9 成本统计：Grok 用量不折价、无批次/日汇总、峰谷按运行起点单值判定

- 证据：`_cost_of :1043-1073` 只对 `deepseek-flash`（`:1057`）和 `luna`（`:1058`）计费；`cursor/grok-4.7` 的 usage 不进入任何金额（`grok_review.json` 里存了 `grok.usage`，但没有 cost 字段，`:1964-1992`）。峰谷 `mul` 只按 `started` 一个时间点（`:1045, :1066`），而 av_review 的 `compute_cost` 按每条调用时间戳分别判峰谷（`av_review/cost.py:10-15,42-65,80-93`）。`process` 不汇总多部片成本；没有像 av_review 那样读 Magpie `usage.jsonl` 对账（`av_review/cost.py:18-39`）。
- 影响：单部片的 DeepSeek/luna 估算可看，但「今天跑这批花了多少、Grok 用了多少」无法从 av_auto 直接得到；一次跨峰谷的 `process` 会把所有 token 都按开始时刻计价，跨 12:00/18:00 时误差可达 2 倍。
- 建议：Grok usage 按 av_review `compute_cost` 的「只记 token、不折人民币」处理并写进 `grok_review.json`；`process` 结束打印本轮 `deepseek_cny/luna_usd/grok_tokens` 合计；长期用 Magpie `usage.jsonl` 对账（可复用 `av_review/cost.py` 的 peak 逻辑）。
- 可验证方式：单测 `_cost_of` 传入 grok usage 时输出含 `grok_tokens`；构造跨峰谷 `usages` 断言按每条时间戳计价（或至少文档写明「按起点估价」）。

#### P2-10 可追溯性断点：无持久运行日志、无最终合并结论文件、stale `grok_review.json` 保留

- 证据：入口 `bin/av_auto` 不 tee 日志（没有 `logs/` 约定，`cron/` 与 `systemd/` 只服务 av_review）；`process` 的成功/失败只在 stdout；`review.json`（consensus）与 `grok_review.json`（`tiebreak.kept_verdict`）是两份文件，没有「最终结论」文件供下游一次读取（`build_grok_review_document :1964-1992` 只在 grok 文档里冗余 `prior`，不合并）。当 review 后来变成两家一致而旧 `grok_review.json` 还在时，`cmd_grok_review` 的 SKIP 路径只是打印「已有 grok_review.json 未改动，以这次一致结论为准」（`:2093-2096`），旧文件继续留在持久目录，可能被仓外脚本误读。
- 影响：一次失败的运行只留下零散 JSON，没有「这次 run 做了什么、结论是什么、失败在哪」的单一日志；人工排障要翻 stdout（若未捕获则丢失）和多个 JSON；stale grok 文件与 `review.json` 结论冲突时没有权威字段。
- 建议：`process` 写 `<frames>/process_run.json`（或 profile 级 `av_auto_process_YYYYmmdd.jsonl`）记录每步 rc、耗时、model ok、结论与持久路径；增加 `final_verdict`/`final_source` 字段（review 或 grok tiebreak）；review 变为一致时把旧 grok 文件改名为 `grok_review.stale-<ts>.json` 或写 `superseded` 标记。
- 可验证方式：`process` 后断言存在 run log 且含每部片的 `review.consensus` 与 `grok.kept_verdict`；SKIP 后断言 stale grok 被标记。

#### P2-11 持久帧没有保留策略：`/config` 卷持续增长且内容是敏感影像

- 证据：`OFFICIAL_FRAMES_ROOT=/config/scripts/av-auto-fs/frames`（`:71`）是持久目录；`_persist_frames_dir :2608-2652` 只增不删；`process` 固定 `cleanup=False`（`:2832-2839`），全仓没有删除/过期/配额逻辑（`--cleanup` 只用于临时目录，`:1644-1654,1751-1760`）。
- 影响：每部 8 帧（800px JPEG）约 0.5–2MB，几百部后 `/config` 卷几百 MB～GB 级增长，且是未删的色情缩图长期留在 MoviePilot 配置卷；没有「审完即删、只留 JSON」的选项。
- 建议：给 `process` 增加 `--keep-frames`（默认可选择审完删除帧、保留 `frames_manifest.json/review.json/grok_review.json`，README 已警告先 grok 后 cleanup，正好可复用该顺序）；或加 `av_auto frames-gc --older-than 30d`。
- 可验证方式：单测 `process --cleanup-after-review` 后目录只剩 JSON；无参数时行为不变（兼容）。

#### P2-12 `process` 单部异常隔离只捕 `SystemExit`，且整批/单部没有全局时间预算

- 证据：`cmd_process :2930-2941` 的 `try/except SystemExit` 只处理 `_die`；其他异常（p115client 抛错、`httpx` 异常、`OSError` 未转成 `SystemExit`）会向上冒泡，导致整批剩余番号不再处理，也不会打印 `PROCESS 未完成` 清单。抽帧最坏耗时：每个时间点 `1 + PRECHECK_RETRIES = 4` 次尝试（`:1514`）× 8 点 × `FRAME_TO=180s`（`:55`）≈ 96 分钟/部，尚未计预检 90s/终审 180s×2/重试退避；`process` 无总超时。
- 影响：单部网络/库异常会打断整批（虽然 shell 入口仍会 apply pending，见 `bin/av_auto:302-319`）；一个 CDN 卡死的番号可能长时间占住调度窗口。
- 建议：`cmd_process` 对每部 `except Exception as exc` 转成 FAIL 并继续下一部（记录 traceback 到 run log）；增加 `AV_AUTO_TITLE_BUDGET_SEC`（或 `--timeout`），超时把该部标记 timed-out、保留已完成步骤供重入；抽帧循环里按剩余预算缩短 `FRAME_TO`。
- 可验证方式：单测让某部抛 `RuntimeError`，断言后续番号仍被处理且最终 rc=1、`PROCESS 未完成` 列出该部；构造多部耗时 mock 断言预算生效。

## 5. 与 `av_review` 的重复/差异

先给定位差异：**两者是不同生命周期、不同运行位置的两套审核**——`av_review` 在 grokbot-vm 上跑、经 ssh 进 Unraid `embyserver`、判「Emby 已入库」条目（`av_review/vision.py:311-403`、`README.md:1-10`）；`av_auto` 在 MoviePilot 容器内跑、用自己写的 staging strm、判「离线完成、promote 之前」的新片。因此**抽帧执行器应保持分开**（宿主/图纸/失败模式不同：一边是 Emby 条目图 + 远端 ffmpeg，一边是 115 redirect/fMP4 + 容器内 ffmpeg），但**提示词、模型门、结果 schema、成本口径应统一**；现状是复制后各自漂移，并且互不知道对方。

| 能力 | av_review | av_auto | 建议 |
| --- | --- | --- | --- |
| 定位/生命周期 | Emby 已入库复查；每日 cron | 115 离线完成后、promote 前终审 | 保持分开；在 README 一张图里写清两条审核的边界 |
| 取图来源 | Emby `Primary`/`Thumb`/`Backdrop[0]` 800q80 data URI + 片源帧（`vision.py:292-307`） | 只用片源 ffmpeg 抽帧，不取封面/剧照（`cmd_frames`，`:1393-1660`） | 刻意分开（目的不同：av_review 要看“预览图”是否符合偏好，av_auto 只看正片内容） |
| 抽帧时间点 | 180s、900s、片长 50%（特殊时 70%）或 80%，短于 210s 用 15% 早期点（`vision.py:109-129`） | 120s 起每 480s、85%，最多 8 帧；短片分支见 P2-2（`:917-957`） | 策略可按用途不同，但应把「片长→时间点」抽成可测试的共享模块，避免两套各自有边界 bug |
| 抽帧执行 | ssh → Unraid → `docker exec embyserver ffmpeg`，批处理 `bin/av_review_ff_batch.sh:44-46` | 进程内 `ffmpeg`（`_ffmpeg_one_frame:1256-1281`）+ fMP4 闭合 Range（`_extract_fmp4_frame:1318-1337`、`_snippet_seek:1284-1315`） | 保持分开（宿主/失败模式不同）；可共享 ffmpeg 参数常量（`-frames:v 1 -q:v 2 -vf scale=…`）与超时值 |
| 有效性预检 | 无（用封面/剧照兜底） | DeepSeek-flash 单图预检 + 邻近重抽（`:1171-1249`） | av_auto 的预检是独有价值；可考虑给 av_review 加可选预检，但要先降成本 |
| 系统提示词 | `PROVEN_SYSTEM`（`av_review/constants.py:88-99`） | `REVIEW_SYSTEM`（`scripts/av_auto.py:105-116`） | 近乎逐字重复但已漂移（「预览/封面」vs「预览」；av_review uncertain 多了「P图/审核/无图」）。建议单一文本源（共享常量文件/构建时同步），并加一条测试断言两处字符串相等 |
| 预检提示词 | 无 | `PRECHECK_SYSTEM`（`:95-103`） | av_auto 独有，保留；可抽成独立常量文件 |
| 模型 | `deepseek/deepseek-flash` + `chatgpt-api/gpt-6-luna`（`constants.py:39`），禁 MiniMax（`:41`） | 同两家（`:75-76`），默认禁 `minimax` **和** `grok`（`:79`） | 统一禁用语义/常量：av_review 的 `FORBIDDEN_MODELS` 不含 grok 是因为 grok 走显式 flag，容易误解；建议两个仓用同一 allowlist+flag 约定 |
| Grok tiebreak | 仅 `consensus=="disagree"` 且 `--grok-review`（`vision.py:599-612`） | `disagree` **或** 任一 `uncertain`（`:1813-1831`）；独立入口 + 独立结果文件（`:2067-2148`） | av_auto 的触发更完整（含 uncertain），应把 av_review 对齐；但两边都要处理「Grok 调用失败」（av_auto 的 P1-1 是前车之鉴） |
| 一致/分歧词汇 | `agreement` 人类可读串：「两家一致 pass」「分歧（a / b）」「不完全可比（…）」（`vision.py:249-256`） | `agreement` 枚举 `agree/disagree/partial`，`consensus` 枚举 `pass/fail/uncertain`（`:1718-1727`） | 统一 schema 与词表（推荐 `agreement: agree|disagree|partial|incomplete` + `consensus: pass|fail|uncertain`），否则下游无法 join |
| 结果文件 | `av_review_vision_YYYYMMDD_HHMMSS.{json,md}` + `publish_pair` 到 findav profile；含 plan/summary/route_check/cost（`vision.py:646-704`、`util.py:216-228`） | `<frames>/frames_manifest.json` + `review.json` + `grok_review.json`（`:1603-1625,1729-1743,1964-1992`）；无 md/summary/route_check | 可保留文件位置差异（用途不同），但建议 av_auto `process` 生成一份可汇总的 `av_auto_review_YYYYMMDD_HHMMSS.json`（每部 code→consensus→kept_verdict→cost），与 av_review 的 profile 结果同 schema，便于合并 |
| 成本口径 | 从 Magpie `usage.jsonl` 按每条调用时间戳拆峰谷，含 grok token（`av_review/cost.py:10-15,18-144`） | 从响应 usage 汇总，峰谷按运行起点单值；grok 不计（`:1043-1073`，P2-9） | av_auto 复用 `compute_cost`（或等价实现），至少写出 grok token；process 出批次合计 |
| 忙碌/并发 | `busy.py` + daily `--skip-if-busy`（`busy.py:1-45`、`README.md:48-66`） | 无锁/无忙碌检测（P2-8） | av_auto 复用 flock/busy 模式；两个审核不要同时打 Magpie（配额/限流） |
| 去重/增量 | 跳过已出现在 `av_review_vision_*` / `av_mismatch_vision_*` 的番号（`inventory.py:35-42,92`、`constants.py:60-69`） | 按每部自己的持久目录跳过（`_frames_ready/_complete_review`） | 两套互不感知：av_auto 审过且 pass 的片子 promote 后，av_review daily 仍会再抽帧+再调两家模型（重复花费）。建议 av_review `plan_review` 额外跳过「av_auto 最终结论 pass 且有 grok 决断（若未决）」的 code，或在 profile 里写一个 `av_auto_pass_codes` 供 av_review 读取 |
| 下游消费 | 无仓内消费者（Lead F5） | 无仓内消费者（P1-2；promote 不读 review） | 两条审核如果都定位为「决策支持」，应共用一张「最终结论表」，由仓外调度器/一个 `apply-review` 入口消费 |

**应当统一的**：提示词文本、模型 allowlist 与禁用语义、`review` 结果 schema/词表、成本口径、`agreement=incomplete` 语义、并发/忙碌约定、下游结论表。
**应当保持分开的**：取图来源（Emby 图 vs 片源帧）、抽帧执行位置（Unraid embyserver vs MoviePilot 容器）、触发时机（入库后复查 vs promote 前）、输出位置（profile 汇总 vs 每部持久目录）。原因：运行宿主机、失败模式、产品用途都不同；强行合并反而把两条链的故障域绑在一起。

## 6. 已确认正常

1. `process` 顺序固定为 clean→strm→frames→review→grok（`:2882-2909`），且不 submit/wait/刮削/promote；测试 `test_av_auto_process.py:206-230,297-319` 断言了被禁命令不会被调用。
2. 「不覆盖 review.json」在代码层有多重保证：Grok 只写 `grok_review.json`（`:2051-2057`）、复制时拒绝把同名 grok 当 review（`:2593-2594`）、持久化时比对二者内容禁止相同（`:2647-2648`）。
3. 成功标准包含持久化：`frames`/`review`/`grok-review`/`process` 都在打印成功前写 `/config/scripts/av-auto-fs/frames/<番号>/`（`:1641,1744,2139,2909`），写不进返回 1（`:2598-2605`）；测试 `test_av_auto_process.py:738-760` 覆盖。
4. 持久目录按 `frames_manifest.json / review.json / grok_review.json / probe.json + 帧图` 复制，逐文件 `persist-tmp` 原子替换、核对帧字节数与 review 内容（`:2568-2582,2608-2652`）。
5. 无效帧确实不送终审：`cmd_review` 只取 `ok && valid is True && file 存在` 的 shot（`:1684-1691`），Grok 再从 manifest+review.valid_frames 交集取帧，并排除封面/剧照/Thumb/Backdrop（`:1899-1947`、正则 `:83-85`）；测试 `test_grok_tiebreak.py:128-160` 覆盖。
6. 双模型正常路径的一致/分歧判定正确（pass/pass→agree/pass，pass/fail→disagree/uncertain，含 uncertain→触发 tiebreak），tiebreak 决策矩阵与「Grok pass/fail 保留、Grok uncertain 保持未决」有单测（`test_grok_tiebreak.py:60-127`）。
7. Grok 只钉死 `cursor/grok-4.7`，且 `_grok_tiebreak_once` 对常量被改/含 minimax 直接 `_die`（`:2018-2022`）；测试 `test_grok_tiebreak.py:206-209`。
8. 默认预检/终审路径拒绝 minimax/grok：`_assert_allowed_model` 在 `_load_llm_runtime`（`:1009-1010`）和 `_magpie_chat`（`:1152-1154`）双重把关；测试 `test_grok_tiebreak.py:162-204`；离线实测见 `/tmp/audit_avauto_probe.py` G 段。
9. Grok 跑完前后校验 `review.json` 未变（`:2134-2142`），`_write_grok_review` 拒绝写 secret（`:2058-2060`）；不重跑 DeepSeek/luna（`:2076-2078`）。
10. 抽帧失败降级不堵整批：单点 seek 超时/失败只标 `degraded` 并换邻近时间（`:1256-1281,1514-1531`）；HTTP 直连报 `partial file` 会回退 fMP4 闭合 Range（`:1340-1363`）；fMP4 取数受 `FMP4_MAX_*` 上限约束、不整文件下载（`fmp4_http.py:21-27`）。
11. ENOSPC 处理与 pending strm 复用成立：`/media` ENOSPC 时 strm 写入 pending（`:323-455`），`process`/`frames` 可用 pending 内容继续（`:458-467,2812-2824`），入口即使 python 非 0 也跑 `av_auto_apply_fs`（`bin/av_auto:302-319`）；测试 `test_av_auto_recover.py:128-405`。
12. 番号不能当子命令：Python 侧 `reject_code_as_command`（`:854-860`）与 shell 侧同正则拦截（`bin/av_auto:17-44`）；测试 `test_av_auto_process.py:56-122` 与 shell 测试。
13. 每次运行独立 `llm_runtime.<run>.json`，只删自己的，重叠运行不互删（`bin/av_auto:156-178,255-289`；测试 `test_av_auto_recover.py:466`）。

## 7. 待确认（需真机）

1. **fMP4 闭合 Range 路径**：本机无 ffmpeg，`test_fmp4_http` 的 `IndexTests.setUpClass` 与 `HttpExtractTests.setUpClass` 直接 `FileNotFoundError`（即基线那 2 个环境错误），无法验证 sidx/mfra、相对/绝对 sample offset、GOP 片段拼接在真实 115 CDN 上的成功率；`_snippet_seek` 对「ffprobe duration 是片段绝对 end」的假设也需真实 m4s 样本。
2. **ffmpeg -ss 对 115 redirect 的渐进式 seek**：`FRAME_TO=180s` 是否足够？`partial file` 触发频率？需要 MoviePilot 容器内实测（不在本环境跑）。
3. **Magpie 调用失败率**：P0-1/P1-1 的触发条件是「双模型/Grok 调用失败」。生产上是否出现过、失败比例多少，可查 Magpie `usage.jsonl` 里 `status != 200` 的比例；本审计只能证明代码在失败时的行为错误。
4. **Grok 配额/失败**：`cursor/grok-4.7` 在 Magpie 的配额与错误率；是否已经存在 `grok_review.json` 里 `grok.ok=false` 的历史文件（grep `/config/scripts/av-auto-fs/frames/*/grok_review.json`）。
5. **审核质量本身**：8 帧是否足够、预检误杀/漏放率、Grok tiebreak 与人工判断的一致率——需要人工抽检样本（本审计只验证逻辑，不评价模型效果）。
6. **持久帧容量与保留**：`/config` 卷现在体积、增长速率、是否已有清理 SOP（P2-11）。
7. **重复审核花费**：promote 后 av_review daily 是否实际重审了 av_auto 已审的番号（查 profile 里 `av_review_vision_*` 与 `frames/<番号>` 的交集）。
8. **入口脚本 `av_auto_apply_fs` 的真实位置/权限**：`/mnt/user/appdata/findav/bin/av_auto_apply_fs` 不在本仓库，脚本存在性与失败行为只能看 README/入口（`bin/av_auto:302-319`）。
9. 用户环境具体 IP/token/模型串均按数据对待，未做任何连接性验证。

## 8. 附：复现命令

```bash
# 基线（无 ffmpeg）：60 tests, 58 过, 2 个 test_fmp4_http setUpClass 环境错误
cd /workspace/av-review-main && python3 -m unittest discover -s tests -v

# 纯逻辑探针 A-K（seek_points/trigger/complete_review/失败落盘/白名单/cost/frames identity）
python3.11 /tmp/audit_avauto_probe.py

# 假成功端到端 CASE 1（双模型 503）/ CASE 2（Grok 500）
python3.11 /tmp/audit_avauto_e2e.py

# 复用帧时间重标
python3.11 /tmp/audit_avauto_reuse.py
```

## 9. 与其它分册 / Lead 的衔接

- 与 `00-pipeline-integration.md` 的 **F5（审核结果无下游动作）**同源：本分册的 P1-2 给的是 av_auto 段的具体锚点（`cmd_review` 固定 0、`process` 无汇总/无退出码语义、`cmd_promote` 不读 `review.json`），并据此建议了 `--strict` / `apply-review` / `REVIEW_SUMMARY` 这几个可落地点。
- 与 **F1/F3（两条管线写同一 `/AV`、strm 目录形状不一致）**相关：`cmd_promote` 写 `<strm_root>/<番号>/` 而 mp-115 写 `<strm_root>/<演员>/<番号>/`（`scripts/av_auto.py:2205-2215`、`mp-115 av115_library.py:57-61`），本分册只从「审核段的 promote 边界」提到它，具体修复归 Lead/分册 02。
- 与 **P2-7（结论未与 strm 内容绑定）**相关的 115 侧细节（重新交磁后文件是否同名/同 pickcode）需要分册 02 或真机确认。
- 分册 04 的 fMP4 测试缺口（本机 2 个环境错误）直接限制了本分册对「抽帧执行」的验证深度，§7-1 已标注。
