# 01 · av_review 库内审核链路（Emby 已入库复查 + 库外候选 + API）

审计对象：`wyh137815840/av-review` `main@8337582` 的 `av_review` 包（`/workspace/av-review-main` 只读 clone）。
链路位置：仓库 A（`mp-115` 插件）把 AV 写进 `/AV` + `/mnt/user/media/AV` 并刷新 Emby 之后，由本包的 `review` 复查「已入库内容」、`catalog` 产出「findav 库有、Emby 没有」的候选短名单、`catalog --judge` 做文本初判，`api` 提供触发入口。
总评（一句话）：**骨架完整、设计方向对，但在 python3.11 下 `review`/`daily` 连 import 都过不去（P0），Grok 复审是永不触发的死路，互斥锁是死代码，结果只在整轮成功末尾落盘且没有任何下游动作——作为「人工决策支持工具」大约完成一半，作为「自动审核闭环」基本未完成。**

## 审计范围与方法

**读了什么**（逐行通读，非抽样）：

- `av_review/`：`__init__.py`(2)、`__main__.py`(3)、`cli.py`(240)、`constants.py`(114)、`util.py`(235)、`secrets.py`(66)、`busy.py`(97)、`inventory.py`(126)、`cost.py`(144)、`api.py`(223)、`vision.py`(705)、`catalog.py`(859)，共约 2.8k 行。
- `README.md`、`bin/av_review`、`bin/av_review-api-start.sh`、`bin/av_review_ff_batch.sh`、`cron/av-review.cron`、`systemd/av-review-api.service`。
- 对照阅读（只为判断重复/衔接，不审其内部）：`scripts/av_auto.py` 的抽帧/预检/终审/Grok/promote 段、`bin/av_auto.README.md`；仓库 A 的 `plugins.v2/av115scraper/av115_codes.py`、`av115_library.py`、`__init__.py` 的 `/redirect` 与 Emby 刷新段。
- `git log -- av_review/`：整个包只出现在一个提交 `fb04cc0`（"Add av_review entry and av_auto staging pipeline."），之后从未修过。
- 交叉核对：`diff -rq /workspace/av-review/av_review /workspace/av-review-main/av_review` 无差异，即报告仓库副本与只读 clone 同代码。

**跑了什么**（全部在 /tmp；被审仓库未改，`git status` 为空）：

1. `cd /workspace/av-review-main && python3 -m unittest discover -s tests -v` → **60 用例 / 58 过 / 2 个 `FileNotFoundError: ffmpeg` 环境错误**（属预期；注意：**这 60 个用例没有一个覆盖 `av_review`**，全是 av_auto/fmp4/grok）。
2. `python3 -m py_compile av_review/*.py` → **exit 1**，`vision.py` 语法错误（见 P0-1）。
3. CLI 冒烟：`--help`/`--version`/`health` 正常；`review --dry-run`、`daily --dry-run` 在 `from av_review.vision import ...` 处 `SyntaxError`；`catalog --dry-run` 在本机停在 `FileNotFoundError: ssh`（审计机无 ssh 客户端，属环境限制，非代码缺陷）。
4. 把 `av_review` 复制到 `/tmp/patchtest/av_review`，**只修 P0-1 那两行 f-string**（其余原样），再用 stub 做行为验证：
   - `seek_points`（11/120/210/211/930/931/1200/1800/3600s 等）、`agreement`/`consensus`（含双 503、split、单模型）；
   - `inventory.plan_review`（合成 `av_items_raw`：正常、无番号、HARD_SKIP、limit=0/1、`--codes`、skip-scanned 命中）；
   - `util.collect_scanned_codes`/`is_vision_result_name`/`assert_safe_basename`/`redact`/`norm_code`/`extract_item_code`；
   - `catalog.run_catalog` 端到端（stub `_ssh_mariadb_tsv`/`scp_from`/`ssh`/`load_emby_from_raw`/`load_user_profile`；合成 DB 7 行 + Emby 3 码 + strm 2 文件）；
   - `catalog.run_catalog_judge` 端到端（stub 双模型 `_chat_text` + usage.jsonl + publish；验证 tier/增量/resume 计数）；
   - `vision.run_review` 端到端（stub 取图/抽帧/模型/route/发布；2 条目、chunk=1、`--grok-review`），以及 `maybe_grok` 单测（有/无 `image_files` 两种输入）；
   - `cost.compute_cost`（合成 usage：高峰/谷时、deepseek/luna/grok 各一条）。
5. 结论中凡标「实测」者均有上述脚本输出支撑；标「待确认」者需真机/真实 schema。

## 总体结论

**完成度评分**

| 维度 | 评分 | 依据 |
|---|---|---|
| 功能覆盖（按设计） | 80% | 库内复查、库外候选、文本初判、增量跳过、成本、API、cron/systemd 都有对应实现 |
| 在审计环境（python3.11）可运行性 | **15%** | `review`/`daily`/API 的 `/review`、`/daily` 全部 `SyntaxError`；仅 `catalog`/`health`/`--help` 可用 |
| 结论可信度 | 55% | 无抽帧有效性预检、把封面/剧照与帧混送；HTTP 失败被记成 uncertain 并永久跳过；Grok 复审实际不生效 |
| 数据新鲜度/一致性 | 50% | Emby 清单只来自仓外快照；`emby_item_id` 全空；只对「存在性」不对元数据；番号规则偏弱 |
| 可靠性与并发 | 30% | 锁是死代码；结果无增量落盘；单次 ssh/Emby/解析异常即整轮丢失；无失败隔离 |
| 安全 | 65% | token/密钥缓存/防注入做得不错；但 Emby key 进 URL、DB 密码进 `docker exec` argv、`redact()` 对 Bearer 失效 |
| 审核 → 动作闭环 | 10% | 结果没有任何仓库内消费者/动作；连「建议入库」的磁链都没导出 |
| 工程质量（测试/部署/文档） | 35% | `av_review` 零测试、无 CI/requirements、无版本约束、三处副本、大量硬编码 |
| **综合（作为人工决策支持）** | **约 55%** | 修掉 P0 + P1-1/P1-2 后可用，但仍是「出报告给人看」 |
| **综合（作为自动审核闭环）** | **约 15%** | 缺动作层 + 缺失败恢复 + 缺互斥，不能无人值守 |

**整条链路最关键缺口**：审核结论（pass/fail/uncertain / 建议入库）落盘后**没有任何消费者与动作**（P1-7）；且当前 `review`/`daily` 在本环境根本跑不起来（P0-1）。即使修好，失败条目仍会被「增量已扫」永久忽略（P1-4），并发时还会互相覆盖（P1-2）。

## 环节完成度表

| # | 环节 | 实现位置 | 状态 | 说明 |
|---|---|---|---|---|
| 1 | Emby 清单获取（`av_items_raw.json` 快照） | `inventory.py:16-32`、`catalog.py:100-129` | 部分 | 唯一来源是 Unraid 上的仓外快照；无刷新/新鲜度/条数校验；`emby_id` 缺失无兜底（见 P1-5） |
| 2 | Emby 取图（Primary/Thumb/Backdrop，800q80 data URI） | `vision.py:266-308`、`vision.py:144-151` | 已实现 | 三图逐个下载、单图失败降级为跳过；但 `/Items?Ids=` 与整体流程无 try/retry（P1-3） |
| 3 | strm → 1 跳 CDN → embyserver 内 ffprobe | `vision.py:311-368`、`bin/av_review_ff_batch.sh:22-31` | 部分 | 依赖 `MediaSources[0].Path` 以 http 开头；**fragmented MP4 未处理**（P1-6） |
| 4 | 抽帧点位 180s / 900s / 中后段 | `vision.py:109-129`、`vision.py:386-402` | 已实现 | 实测 120/210/211/930/931/1200/1800/3600s 点位符合设计（>930 且 mid 撞 900 时改 70%）；短片也恒提示「未抽到 900s 帧」（P2-12） |
| 5 | 双模型一致性 + 超时/重试 | `vision.py:168-263`（`chat_once`/`chat_with_retry`/`agreement`/`consensus`） | 部分 | 3 次重试、180s 超时、2 次退避都在；**传输失败被归入「两家一致 uncertain」**（实测，P1-4） |
| 6 | Grok 复审（`--grok-review`） | `vision.py:599-612`、`vision.py:661-662` | **缺口** | 实际永不调用（`image_files` 不在结果 dict 里）；即使调用也不改 consensus/summary、进不了 MD（P1-1） |
| 7 | 超时/重试/临时文件清理 | `vision.py:573-596`、`vision.py:652-664` | 部分 | 只有 chat 有重试；ssh/scp/Emby/抽帧无重试无隔离；清理只在每个 chunk 末尾，异常即残留远端帧与 jobs(URL) 清单 |
| 8 | 增量与跳过规则 | `inventory.py:45-126`、`util.py:174-198` | 部分 | 按番号跳过 mid/low/pilot/rest/grok/`av_review_vision_*`；但失败/无图也计入已扫（P1-4）；无「内容变更才重审」；重审需 `--no-skip-scanned`（P2-13） |
| 9 | 结果文件命名与保护 | `vision.py:646-649`、`util.py:216-228`、`constants.py:46-58` | 部分 | 时间戳 + `PROTECTED_BASENAMES` 有效（实测拒绝 `av_mismatch_scan.json`）；秒级命名 + 无锁可互相覆盖；review 产物无 `latest`；latest/partial 非原子（P1-2/P2-12） |
| 10 | catalog：DB ↔ Emby 按番号对照 | `catalog.py:281-324` | 已实现 | 实测「库有 Emby 无」集合正确；但只对存在性、不比元数据；strm-only 混入 Emby 统计（P2-6） |
| 11 | catalog：strm 补码 | `catalog.py:132-148` | 部分 | 能补 `/mnt/user/media/AV` 的 strm 番号；`ssh` 返回码不检查、目录硬编码、不扫 av_auto staging/pending（P2-7） |
| 12 | catalog：rule_prefilter | `catalog.py:158-203` | 已实现 | 只用于排序，不是终判；品牌前缀启发式；`beauty_score` 非数值会抛异常（P2-8 邻域） |
| 13 | catalog `--judge` 文本判断 | `catalog.py:394-472`、`catalog.py:596-854` | 部分 | 双模型、增量落盘、resume、transport 过滤都在；`--dry-run` 不生效（P2-4）、只判旧 latest、不读 `av_magnet`（P1-7） |
| 14 | 内网 API 与 token | `api.py:18-33`、`api.py:86-223` | 部分 | token 生成/`compare_digest`/拒绝 URL 内 key 都正确；`/health` 未鉴权且每次 ssh；inline 无异常保护；无任务/结果查询（P2-5） |
| 15 | busy 互斥 | `busy.py:36-97` | **缺口** | `VisionLock`（65-97）全仓库**无调用者**；`review` CLI 完全不查 busy；`daily`/API 只在入口查一次（TOCTOU）（P1-2） |
| 16 | 成本统计（Magpie usage.jsonl） | `cost.py:18-144` | 部分 | 数学正确（实测高峰/谷时、DS ¥ / luna $、grok 只记 token）；只在成功末尾核算；时间窗归因会把并发扫描算进来（P2-15） |
| 17 | 审核闭环（结果 → 动作） | 无对应实现 | **缺口** | pass/fail/uncertain 只落 profile 文件；仓库内唯一「消费者」是下次扫描的 skip 集合；无通知/标记/交磁/重刮/入库/promote（P1-7） |
| 18 | 与 `scripts/av_auto.py` 的衔接/去重 | 见专节 | **缺口** | 两套抽帧/提示词/结果 schema/Grok 语义；`av_review` 不读 `review.json`；av_auto 已修的 fMP4 未回流（P2-11） |

## 问题清单

### P0（1 条）

#### P0-1 · `vision.py` 在 python3.11 下语法错误，`review`/`daily`/API 的 review 全部不可用

- **证据**：`av_review/vision.py:561`、`:566` 的两条 f-string 表达式里含反斜杠：
  ```python
  lines.append(f"| {rec['code']} | {(rec.get('actress') or '').replace('|','\\|')} | ...")
  ```
  Python ≤3.11 不允许 f-string 表达式内出现反斜杠（PEP 701 到 3.12 才放开）。实测：
  - `python3 -m py_compile av_review/*.py` → `exit=1`；
  - `python3 -m av_review review --dry-run` → `av_review/cli.py:128` 触发 `av_review/vision.py:561 SyntaxError: f-string expression part cannot include a backslash`；
  - `python3 -m av_review daily --dry-run` → `cli.py:189` 同一处失败。
  影响面按 import 点：`cli.cmd_review`（`cli.py:128`）、`cli.cmd_daily`（`cli.py:189`）、`api._spawn_or_run` 拉起的子进程（`api.py:169-172`/`200-204`）、cron（`cron/av-review.cron:12`）。`catalog`/`health`/`--help` 不 import vision，所以能跑——这解释了问题为何长期没被发现。
- **影响（整条链路）**：用户目标里的「Emby 已入库复查」这一半整体不可用；每天 19:30 的 `daily` cron 每夜以 traceback 结束，既不识图也不刷新短名单（`cli.py:206-210` 里 review 在前，异常会跳过 catalog）；API `/review`、`/daily` 只会返回 500/子进程崩溃。`--grok-review`、成本、增量等设计全部无从谈起。
- **建议修复**：把两处 `.replace('|','\\|')` 提取为模块级函数（如 `_pipe_escape(s)`），f-string 里只调用函数；或先 `act = (rec.get('actress') or '').replace('|', '\\|')` 再拼。修完对 `av_review` 加 `python3 -m compileall` + 最小 import 测试进 CI（见 P2-1）。
- **可验证方式**：`cd /workspace/av-review-main && python3 -m py_compile av_review/vision.py && python3 -m av_review review --dry-run`（应输出 plan JSON）；再跑 `python3 -m av_review daily --dry-run`。若宿主 python ≥3.12 则此错不触发——见「待确认」第 1 条。

### P1（8 条）

#### P1-1 · `--grok-review` 是死功能：Grok 实际从不被调用；调用了也不改结论、不进 MD

- **证据**：
  - `vision.judge_all` 产出的每个 item 只含 `code/emby_id/actress/actresses/title/source/images_used/ffmpeg/evidence/models/agreement/consensus/verdicts`（`vision.py:450-459`），**没有 `image_files`**；
  - `vision.maybe_grok` 却用 `rec.get("image_files") or []` 取图（`vision.py:605-608`），永远得到空列表，于是只写一条 `models[grok] = {"ok": False, "reason": "无图可复审"}` 然后 `continue`，**既不写 `grok_review` 也不调用模型**；
  - 实测 `run_review(grok_review=True)` 的分歧条目：`grok_review=None`、`verdicts` 无 `cursor/grok-4.7`；把 `image_files` 手工注入同一函数后 Grok 才会被调用（说明缺的是字段传递，不是开关）。
  - 即使触发：`maybe_grok` 只写 `rec["models"][GROK_MODEL]` / `rec["verdicts"][GROK_MODEL]` / `rec["grok_review"]`（`vision.py:609-612`），**不回写 `consensus`/`agreement`**；`summarize` 只看 `consensus`（`vision.py:511-539`），所以 Grok 判 pass/fail 之后条目仍被算进 `new_disagree`/`review_codes`；`write_md` 没有 Grok 列/段落（`vision.py:542-570`，实测 MD 中唯一出现 "Grok" 的是静态免责句）。
- **影响（整条链路）**：README（第 32-33 行）与 `--help` 承诺的「分歧时 Grok 复审」在成本上已付费（调用会走 Magpie）但在产物上不存在；两家分歧永远停在人工 review 列表，没有任何自动裁决结论可给下游。这与 av_auto 已实现的 `grok_review.json`+`tiebreak.kept_verdict`（`scripts/av_auto.py:1839-2004`）形成明显落差。
- **建议修复**：把图片路径/帧清单随结果传递（`judge_all` 的 `item_out` 增加 `image_files`，或在 chunk 循环里把 `by_code[code]["image_files"]` 合并进结果）；`maybe_grok` 用 `decide_tiebreak` 式逻辑产出 `kept_verdict` 并回写 consensus/agreement/summary；`write_md` 增加 Grok 复审表；无图时也要在结果里显式标 `grok_review: {"skipped": "no_frames"}` 而不是静默。
- **可验证方式**：stub 模型跑 `run_review(grok_review=True)`，断言分歧条目 `verdicts` 含 `cursor/grok-4.7`、`grok_review.kept_verdict` 存在、`summary.new_disagree` 按结论下降；或真机对 1 条已知分歧跑 `review --codes X --grok-review --no-skip-scanned`。

#### P1-2 · 互斥锁是死代码，并发会互踩同名目录/结果文件

- **证据**：
  - `busy.py:65-97` 定义了 `VisionLock`，但全仓库 `grep "VisionLock"` 只有定义处，**没有任何调用**；`LOCK = RUN_DIR/"vision.lock"`（`busy.py:11`）因此永远不会存在，`vision_busy()` 的 lock 分支（`busy.py:47-57`）恒空。
  - `vision.run_review`（`vision.py:615-664`）从头到尾没有 `acquire()`/busy 检查；`bin/av_review review`（`bin/av_review:1-7` → `cli.cmd_review`）也不检查 busy，只有 `daily`（`cli.py:190-191`）和 API 的 `/review`（`api.py:143-148`）在入口查一次——从检查到子进程真正开跑之间是 TOCTOU 窗口。
  - 工作目录是固定路径：`WORK=/tmp/av-review-work`（`constants.py:34`），`IMG/FR/OUT` 全在其下（`vision.py:76-78`）；远端也是固定 `/tmp/av-review-work`（`constants.py:35`、`bin/av_review_ff_batch.sh:6`）。两个 review 并发会共用 `images/`、`frames/`、`av_items_raw.json`、远端 `jobs_*.jsonl`；`cleanup_temps()` 每个 chunk 结束会 `unlink` 整个目录（`vision.py:573-587`）并远端 `rm -rf .../frames`（`vision.py:588-596`），**会删掉另一个进程正在判读的帧**。
  - 结果名是秒级时间戳：`out_name = f"av_review_vision_{now_stamp()}"`（`vision.py:646`，`now_stamp` 为 `%Y%m%d_%H%M%S`，`util.py:28-29`），两个进程同秒启动会写同一文件；`publish_pair` 直接 scp 覆盖同名（`util.py:216-228`）。
  - API 的 heavy 分支用 `subprocess.Popen` 后台跑（`api.py:200-204`），不占锁、不跟踪 pid 以外状态；API 自己的 busy 检查同样在子进程启动前完成。
- **影响（整条链路）**：cron 19:30 与手动/API 触发、或与仍在跑的 rest 扫描叠在一起时（README 第 26 行只是口头警告），会互相覆盖图片/帧与结果文件、互相清理临时目录；结果 JSON 可能被后一个进程覆盖，人工拿到的报告无法复现；同时段 Magpie 配额被打满、cost 归因混在一起（P2-15）。这也是 `busy.py` 名字存在但用户以为「有锁」的原因。
- **建议修复**：`run_review`/`run_catalog_judge` 进入重活前 `with VisionLock():`（并让 `daily`/API 直接用锁而不是先查后跑）；锁文件记录 pid+cmd+started；`WORK` 按 run id 隔离（`/tmp/av-review-work/<stamp>_<pid>`）或在锁内使用；结果命名加 pid/纳秒或用 `O_EXCL` 创建；stale lock 检测（pid 不存在则清理）。
- **可验证方式**：同时启动两个 `review --codes ... --no-skip-scanned`（同一批 code），观察第二个是否被拒/排队；检查结果文件是否两个都在、帧目录不互相清空。或用 `unittest` 直接对 `run_review` patch 慢速 stub，验证第二次调用拿不到锁。

#### P1-3 · 结果无增量落盘、无异常隔离：任一环节异常 = 整轮结果全丢

- **证据**：
  - 所有 item 先攒在内存 `results`，chunk 循环结束、cost/route 之后才 `out_json.write_text(...)`（`vision.py:652-664` → `vision.py:700-703`）。中途任何异常都不会落任何结果文件。
  - 循环内大量「裸异常」路径：`fetch_meta_and_images` 的 `/Items?Ids=` 没有 try（`vision.py:266-274`，`emby_get_json` 内 `OPENER.open` 抛 HTTPError/URLError 直接冒泡）；`route_stat()` 用 `ssh(..., check=True)` 且在扫描前后各一次（`vision.py:155-158`、`638-641`、`678`）；`run_ffmpeg` 的 `ssh mkdir`、`install_ff_batch`、`scp`、`tar` 全 `check=True`/`check_call`（`vision.py:312-335`、`370-384`）；probe JSON 的 `float(duration)` 无 per-code try（`vision.py:349-361`），probe 文件里 `"duration": "N/A"` 这类值会 `ValueError` 打断整轮。
  - 与之对照：av_auto 明确把「frames/review.json 在报成功前落到持久目录，写不进就是 FAIL」（`bin/av_auto.README.md:51`、`scripts/av_auto.py:2608-2650`），且 `process 非 0 也会 apply pending`（README 第 55 行）。
- **影响（整条链路）**：一个第 5 个 chunk 的 115/Emby/ssh 抖动，会让前 4 个 chunk 的有效结论一起消失，重跑还要重新付费识图；临时帧只在「成功走到 cleanup」时才删，异常时远端 `/tmp/av-review-work/jobs_*.jsonl`（含 pickcode 播放 URL）和容器内帧会残留到下一次成功运行。
- **建议修复**：每个 chunk 完成即写一份 `av_review_vision_<stamp>.partial.json`（或按 code 写 per-item 临时结果），成功后聚合成正式文件再删除 partial；对 `fetch_meta_and_images`/`run_ffmpeg`/probe 解析做 per-code try 并把错误写进 `ffmpeg.errors`/`models.reason`；`route_stat` 失败降级为 warning；`cleanup_temps` 放进 `try/finally` 且只在整轮结束（或按 run 隔离目录）执行。
- **可验证方式**：stub 让第 2 个 chunk 的 `fetch_meta_and_images` 抛异常，断言进程退出码非 0 但第 1 个 chunk 的 partial 文件存在且可读；恢复后重跑应能跳过已完成的 code（或至少人工可取证）。

#### P1-4 · 瞬时失败被记成 uncertain 并永久「已扫」：一次 Magpie 5xx 就让条目再也不会被复查

- **证据**：
  - `chat_once` 对 HTTP 错误/超时返回 `verdict="uncertain"`（`vision.py:214-224`）；`chat_with_retry` 重试 3 次后同样落 `uncertain`（`vision.py:227-246`）。
  - `agreement()` 的判定是 `models[m].get("ok") or verdict in ("pass","fail","uncertain")`（`vision.py:250`），所以两个模型都 503 时返回 **「两家一致 uncertain」**（实测），而不是「不完全可比（error/error）」；`consensus` 同样返回 `uncertain`（`vision.py:259-263`）。
  - 结果文件被 `collect_scanned_codes` 收集时**不区分 verdict**（`util.py:151-161`、`174-198`），`inventory.plan_review` 只要 code 在集合里就跳过（`inventory.py:92-94`）。于是「因为 Magpie 挂了/超时」而记下的 uncertain，下一轮 daily 起就永久不再复查。
  - 同一条不可见噪声还来自「无图」：`message "无可用图片"` 也写入结果并计入已扫（`vision.py:421-425`）。
- **影响（整条链路）**：增量复查的「覆盖率」不可信：一次网络抖动或额度耗尽就会让一批片子在报告里变成「已审核：uncertain」且不再被触碰，人工也不会看到「需要重试」的清单。这与 av_auto 在 resume 里专门剔除 transport failure 的做法相反（`scripts/av_auto.py:655-675`）。
- **建议修复**：在 item 里区分 `status: ok|transport_error|parse_error|no_images`，传输失败的不写进「已扫」集合（或写进单独的 `failed_codes` 并在下次优先重跑）；`agreement` 保留 `error` 语义（「不完全可比」）；结果文件里增加机器可读的 `failed` 列表；daily 输出重试清单。
- **可验证方式**：stub 两个模型都返回 HTTP 503，跑 run_review，断言结果里 `status=transport_error` 且下一次 `plan_review` 仍把该 code 放进 todo；再对真实 Magpie 断网/改错 key 复现一次。

#### P1-5 · Emby 清单只有仓外快照一条路，无新鲜度校验、无按番号兜底

- **证据**：
  - `inventory.load_emby_items_local` 每次现拉 `/mnt/user/appdata/findav/profile/av_items_raw.json`（`inventory.py:16-32`），`catalog.load_emby_from_raw` 又单独拉一遍（`catalog.py:100-104`）；仓库里**没有任何生成/刷新该快照的代码**，也没有 mtime/条数校验（`catalog.py:122-129` 只把 `TotalRecordCount` 记进 meta，从不与 `len(items)` 比较）。
  - 取图只认 `av_items_raw.json` 里的 `Id`（`vision.py:268-274`、`297-298`）；`emby_id` 为空时不做任何按番号的 Emby 查询（没有 `SearchTerm`/`AnyProviderIdEquals`），直接进入「无图 uncertain」（`vision.py:298`、`421-425`）。
  - 结果：新入库但快照未更新的条目不会被复查；Emby 里 item 被重建/换 Id 后快照里的旧 Id 查不到 → 无图 uncertain → 又被 P1-4 计入已扫。
- **影响（整条链路）**：仓库 A/B 之间的数据流靠一个仓外文件维系；用户目标「根据数据库里已有内容做审核」的「已有内容」是否完整取决于没人审计的 findav 流程。一旦快照过期，复查会静默漏片，而报告仍显示「已扫 N 条」。
- **建议修复**：`--refresh-snapshot`（或调用 findav 侧生成器/直接 Emby `/Items?Recursive` 拉 AV 库）；对 `TotalRecordCount` 与 `len(Items)` 不一致告警；`emby_id` 缺失时按番号走 `SearchTerm` + `ProviderIds.MetaTube` 兜底；报告里显示快照生成时间与年龄。
- **可验证方式**：把 `av_items_raw.json` 换成缺一个 code 的旧副本，跑 `review --dry-run`，确认该 code 不会出现在 plan；或看 `review` 输出里是否有快照年龄字段（当前没有）。

#### P1-6 · 抽帧未处理 fragmented MP4，115 strm 的 fMP4 片源大概率抽不到帧

- **证据**：
  - `av_review` 把 strm 解析出的 http URL 直接交给 `docker exec embyserver ffprobe/ffmpeg`：`bin/av_review_ff_batch.sh:24`（probe）、`:38`（frame，`ffmpeg -ss "$ss" -i "$url" ...`），`vision.py:311-384` 只是调度，没有 fMP4 分支；`grep -rn fmp4 av_review/` 为空。
  - 同仓库的 `av_auto` 已明确记录并解决过这个问题：`bin/av_auto.README.md:70` 写「HTTP(S) 上的 fragmented MP4 … 不再把 URL 直接交给 `ffmpeg -ss`。那条路径会发不闭合的 `Range: bytes=offset-` … 源上会报 `partial file`、一个包都写不出」；实现见 `scripts/av_auto.py:47`（import `open_fmp4_session`）、`:1318-1360`、`:1438`，并有测试 `tests/test_fmp4_http.py`。
- **影响（整条链路）**：115 CDN 上的 AV 片源有相当比例是 fMP4；`av_review` 对这类条目的 probe 会失败或 frame 全失败，只剩 Emby 的 Primary/Thumb/Backdrop 三张图进双模型（`vision.py:292-305`），等于把「看片源抽帧的审核」降级成「看封面/剧照审核」——而封面恰恰是最容易被修图误导的证据；Grok 复审（修好 P1-1 后）也会因无帧失效。且报告只加一条「ffprobe 失败或超时」的备注（`vision.py:500-501`），读报告的人未必意识到这是系统性降级。
- **建议修复**：把 `scripts/fmp4_http.py` 的闭合 Range 逻辑复用过来（抽成共享模块，见 P2-11）：先 `open_fmp4_session(url)`，能建 session 就在本地取目标分片再送 ffmpeg；失败再退回直连。probe 与 frame 两条路径都要覆盖。没有帧的条目在 summary 里单独计数（`n_no_frames`）并以 fail-safe 方式降级为「仅封面」，而不是默默 pass。
- **可验证方式**：拿一条已知 fMP4 的 115 strm（真机）跑 `review --codes X --no-skip-scanned`，看 `ffmpeg.frames` 是否 `ok=false`、`ffmpeg.errors` 是否含 `partial file`；改后应能抽出帧且 `media_access` 风格字段为 `fmp4-range:*`。

#### P1-7 · 审核结果没有任何下游动作/消费者；连「建议入库」的磁链都没导出

- **证据**：
  - 产物只有：`av_review_vision_<时间戳>.{json,md}`（`vision.py:648-649`、`700-703`）、`av_catalog_missing_<时间戳>.{json,md}` + latest（`catalog.py:353-371`）、`av_catalog_judged_<时间戳>.{json,md}` + latest + partial（`catalog.py:740-773`）；全部 `publish_pair` 到 `PROFILE_REMOTE`（`util.py:216-228`）。
  - 全仓库 grep 这些文件名，只有生成处与 README；`/workspace` 范围内（mp-av、moviepilot 插件、reports）也没有任何读取方。仓库内唯一的「消费」是下一轮扫描把 code 当已扫（`util.py:174-198`），即**结果只影响「以后还扫不扫」**，不触发任何动作。
  - 代码层面没有任何动作实现：`grep -niE "INSERT|UPDATE|DELETE FROM|notify|webhook|magnet|ed2k|promote|refresh" av_review/` 只剩 Magpie 请求（`vision.py:188`、`catalog.py:422`）与 `POST` 方法名；不写 findav DB、不改 Emby（只 GET `/Items`、`/Images`）、不调 mp-115/av_auto。
  - 更具体的一处断点：catalog 的摘要/文档提到 DB 里有 `av_magnet` 表（`catalog.py:214` 的说明），但 `_ssh_mariadb_tsv` 只 `SELECT ... FROM av_title`（`catalog.py:46-48`），**「建议入库」清单里没有磁链/ed2k**。因此即使人工想用 `av_auto submit --code X --url ...`（需要 URL）接手，也必须另写 SQL 查 `av_magnet`——「候选 → 交磁 → 离线」这一段在产物层面是断的。
  - 两份产物也没有 join：`av_catalog_missing_latest` 不会回填某 code 已判/已交磁状态，`av_catalog_judged_latest` 也不含磁链；人工要自己交叉比对两个 JSON。
- **影响（整条链路）**：这是整条链路最关键的缺口（与 Lead 00 的 F5 一致，本文给出更细的验证）：如果产品预期是「审核完自动处置」，链路断在这里；如果预期是「出报告给人看」，则缺一份 SOP + 稳定查询接口，且 catalog 的下一跳（交磁）缺少必需字段。pass/fail 都没有去处：fail 不归档/不删/不重刮，pass 不标记、不 promote、不写回 DB（`av_title.user_verdict` 可回写但没有 UPDATE 代码），uncertain 不进通知队列。
- **建议修复**：先用最小改动补「可执行清单」：catalog 读 `av_magnet`（只 SELECT），把 `code + magnet/ed2k + tier + reasons` 写进 `av_catalog_judged_latest` 的 `actions[]`；`review` 结果增加 `failed_codes/cleanup_candidates` 稳定字段；再加一个独立入口（如 `av_review apply --dry-run/--code X` 或交给 av_auto `apply-review`）把结论映射到允许动作（交磁、移入待删目录、Emby 加标签/收藏、写回 DB verdict），默认 dry-run，并要求显式确认。若决定维持人工决策，则 README 写明「谁在什么时机读哪个文件」的 SOP。
- **可验证方式**：现在 grep 证明无消费者；修后可测 `catalog` 产物含 `magnet` 字段、`av_review apply --dry-run` 打印的动作清单与真实一致（不连 115 也能验证 dry-run）。

#### P1-8 · Emby API key 被拼进每个请求的 URL，违反项目自定安全约束

- **证据**：`emby_url()` 把 key 直接拼 query：`... + "api_key=" + urllib.parse.quote(EMBY_KEY)`（`vision.py:82-84`）；所有 Emby 请求（`/Items?Ids=`、Primary/Thumb/Backdrop 取图）都走它（`vision.py:87-101`）。而 README 第 6 行明确写「Key 只从 Magpie `caller-keys.json` 与 findav `emby.env` / `connection.env` 读取，**不进 URL**、不进日志、不进结果文件」。
- **影响（整条链路）**：Emby 自身的访问日志、反代/网关日志、抓包都会记录完整 query，key 泄漏面从「已 600 权限的文件」扩大到一个长期运行服务的日志；与项目自我声明的约束冲突，也会让后续「密钥不进 URL」的审计失效。局域网部署使实际风险有限，但这是一条**可零成本避免**的违规。
- **建议修复**：改用 `X-Emby-Token` 请求头（urllib `Request(headers={"X-Emby-Token": EMBY_KEY})`），或 `Authorization: MediaBrowser Token=...`；`emby_url` 只拼 path；顺手检查 `redirect_safe`/日志里不出现 key。
- **可验证方式**：改后对任意 Emby 请求抓包/打 Emby 日志，确认 URL 无 `api_key`；用错误 key 测试仍能 401（证明头生效）。

### P2（15 条）

#### P2-1 · `av_review` 零测试、无 CI、无 requirements/版本约束

- **证据**：`tests/` 只有 `test_av_auto_process.py`、`test_av_auto_recover.py`、`test_fmp4_http.py`、`test_grok_tiebreak.py`（`grep -l av_review tests/*` 为空）；基线 60 用例全属 av_auto/fmp4；仓库无 CI 配置、无 `requirements.txt`/`pyproject.toml`，`av_review/__init__.py:2` 只有 `__version__ = "1.0.0"`；`av_auto` 无版本号（`bin/av_auto` 亦无）。
- **影响**：P0-1 这种「一行语法错误」可以长期存在于 main 就是门禁缺失的直接后果；python 版本变化（f-string 规则、magpie/httpx 依赖）无任何自动检测。
- **建议**：加 `python3 -m compileall av_review` + 最小 import/参数解析测试进 CI；补 `tests/test_av_review_*.py`（util/norm_code、seek_points、catalog 对照、cost、api token、busy lock）；声明 `python_requires>=3.11`（或提升到 3.12 并写清）。
- **可验证**：`python -m unittest discover -s tests` 出现 av_review 用例且全绿；CI 在 py3.11/3.12 跑。

#### P2-2 · `redact()` 对 `Authorization: Bearer <token>` 不脱敏 token

- **证据**：`util.py:231-235` 的正则 `(api[_-]?key|authorization|bearer|...)(["'=\s:]+)([^\s"']+)` 在 `Authorization: Bearer abc123` 上匹配到 `Authorization: ` 后把 **`Bearer`** 替换成 `***`，token 本体留在后面。实测：
  `'Authorization: Bearer eyJ...SECRETTOKEN' -> 'Authorization: *** eyJ...SECRETTOKEN'`。
- **影响**：`api.py:89-92` 的日志脱敏与 `api.py:180` 的 stderr 脱敏在「带 Authorization 头的日志/错误」场景失效；目前 API 访问日志不含头，所以是防御失效而非已泄漏路径，但一旦有人把请求头/异常字符串塞进日志就会直出 token。
- **建议**：正则先处理 `(authorization|...)` 后跟 `bearer\s+([^\s"']+)` 时替换 token；或改为按行解析头名再统一打码；补单测。
- **可验证**：`python3 -c "from av_review.util import redact; print(redact('Authorization: Bearer abc123'))"` 应输出 `Authorization: Bearer ***`（或整行打码）。

#### P2-3 · findav MariaDB 密码出现在 Unraid 上 `docker exec` 的命令行

- **证据**：`catalog.py:37-52` 的远程脚本 `source /mnt/user/appdata/findav/connection.env` 后执行
  `docker exec -i findav-db mariadb -u"$FINDDAV_USER" -p"$FINDDAV_PASSWORD" ...`（`catalog.py:45`）。远端 shell 展开后，**宿主 `ps`/`/proc/*/cmdline` 能看到明文密码**；`2>/dev/null` 还把「Using a password」警告与部分错误吞掉。
- **影响**：Unraid 上任何能跑 `ps` 的进程/用户都能读到 DB 口令；与 README「密码不进日志/命令行」的意图冲突（`secrets.py`/`catalog.py` 的注释声称密码不会被打印，但没覆盖 argv）。风险面限于单机 Unraid。
- **建议**：改用 `MYSQL_PWD` 环境变量（`docker exec -e MYSQL_PWD="$FINDDAV_PASSWORD"`）或 `--defaults-extra-file` 临时 600 文件；不要吞 stderr，把错误回传（脱敏后）以便定位。
- **可验证**：执行期间在 Unraid `ps aux | grep mariadb` 看是否还有 `-p<明文>`；改后应看不到。

#### P2-4 · `catalog --judge --dry-run` 仍会真调 Magpie 并写盘

- **证据**：`cli.cmd_catalog`（`cli.py:140-149`）在 `judge=True` 时只做 busy 检查就 `run_catalog_judge(...)`，**从未看 `args.dry_run`**；`--dry-run` 仅传给 `run_catalog(judge=False, ...)`（`cli.py:175`）。实测把 `run_catalog_judge` 换成探针后，`catalog --judge --dry-run` 确实进了 judge 分支。
- **影响**：用户按 README 用 `--dry-run` 预演时，会实际消耗 Magpie 配额并写 `av_catalog_judged_*`/latest/partial，覆盖上一轮判断结果；与「--dry-run 不调模型」的整体语义冲突。
- **建议**：`if judge and args.dry_run: 只打印将判断的条数/模型/预计窗口并返回`；或把 `--dry-run` 透传给 `run_catalog_judge(dry_run=True)` 做计划。
- **可验证**：`catalog --judge --dry-run` 不产生新 `av_catalog_judged_*` 文件、无 Magpie usage 增量。

#### P2-5 · API 的可用性/可观测性缺口

- **证据**：
  - `GET /health` 不需要 token（`api.py:101-108`），而 `_health()` 每次都 `ssh` 到 Unraid 做 `stat+md5sum`（`cli.py:104-124`，timeout 15s）——局域网内任何人可反复请求放大 ssh 压力；`_health()` 顶层 `"ok": True` 恒真（实测 ssh 失败也只体现在 `route.raw="error: FileNotFoundError"`）。
  - heavy 任务 spawn（`api.py:184-209`）只回 `pid`；日志名 `api-{kind}-{os.getpid()}.log` 用 **API 服务器 pid**（`:185`），同类任务共用一个日志、追加写；没有 job id/状态/结果查询接口，失败只能翻日志，`/health` 只显示 busy。
  - inline 分支 `subprocess.run(..., timeout=180)`（`api.py:169-172`）未捕获 `TimeoutExpired`，catalog 内联（DB 查询+scp 大 JSON+find）一旦超 180s 会由 handler 线程抛异常、连接直接断开、无 JSON 响应；`_body` 无 Content-Length 上限（`api.py:45-56`）。
- **影响**：内网运维体验差，任务失败静默；`/health` 无法作为「Unraid/route 正常」的健康信号。
- **建议**：health 结果加 15-30s 缓存 + 顶层 ok 反映 route 状态；heavy 任务给 job id、落 `run/jobs/*.json` 状态文件，加 `GET /jobs`/`GET /jobs/{id}`；inline 包 try/except 返回结构化错误；限制 body 大小。
- **可验证**：Unraid 断开时 `GET /health` 的 `ok` 应为 false（当前仍 true）；连续两次 `/review` 应返回不同 job id 且可查状态。

#### P2-6 · catalog 统计口径会误导：strm-only 混入 Emby、n_todo/n_done 不一致、规则排除被说成「两家一致 fail」

- **证据**：
  - `supplement_strm_codes` 把只在 `/media/AV` 有 strm、不在 Emby 快照里的番号补进 `emby_by`（`catalog.py:132-148`）；随后 `eset=set(emby_by)`、`emby_unique=len(eset)`、`emby_only=sorted(eset-fset)`（`catalog.py:288-292`）——**strm-only 被算成「Emby 有」**，页面上「Emby AV 唯一番号」和「Emby 有、findav 没有」都掺了 strm-only（实测：3 个 Emby 码 + 1 个 strm-only → emby_unique=4）。
  - `run_catalog_judge` 对 `prefilter.bucket=="exclude"` 的条目直接赋 `consensus="fail"`、`tier="不建议"`（`catalog.py:684-692`），于是 `_summarize_judged` 把它计入 `agree_fail`（`catalog.py:571-593`）；实测 `n_todo=5` 而 `n_done=6`（excluded 也算 done），MD 里「不建议（两家一致 fail）」下 DS/luna 两列显示 `None`。
  - `HPET-022`/`MFYD-175` 是 `review` 侧的 `HARD_SKIP`（`constants.py:43`），但 catalog 侧不排除，会正常进入 priority/建议入库（实测进「再看」）。
- **影响**：人工看报告会高估已入库数、低估缺失候选；「两家一致 fail」与「规则排除」混淆，无法据此统计模型质量；HARD_SKIP 语义在两个子命令下不一致。
- **建议**：summary 拆 `emby_indexed`/`strm_only`/`emby_only_indexed`；`agree_fail` 只统计真调过模型的条目，规则排除单列 `rule_exclude`；catalog 对 `HARD_SKIP` 打标或排除并在 MD 说明。
- **可验证**：用合成数据跑 `run_catalog`/`run_catalog_judge`，断言 summary 各计数互斥且与明细行可加总（现有 harness 已复现 5 vs 6）。

#### P2-7 · strm 补码对失败不敏感、目录硬编码、且看不到 av_auto staging/pending

- **证据**：`supplement_strm_codes` 用 `ssh(f"find {MEDIA_AV_REMOTE} -type f -name '*.strm' ...")`（`catalog.py:134-137`），`ssh()` 返回码不检查、stderr 被 `2>/dev/null`（`util.py:43-50`），find 失败/超时/部分输出与「目录里没有 strm」不可区分；`MEDIA_AV_REMOTE="/mnt/user/media/AV"` 硬编码（`constants.py:22`），而仓库 A 的 `strm_root` 是可配置的（`scripts/av_auto.py:584` 会读插件配置，`av_review` 不读）。同时 av_auto 的暂存 strm 在 `/mnt/user/media/av-auto`（`bin/av_auto.README.md:7`）、promote 前还有 `pending` 队列，`catalog` 完全不知道这两个位置。
- **影响**：`strm_root` 改过就会静默漏补；find 超时会把已在 `/AV` 的片子当成「库外候选」；已在 av_auto staging 下完、审完等待 promote 的番号会被再次建议交磁（重复下载/重复付费审核）。
- **建议**：检查 `r.returncode` 并对非 0 告警（不静默）；目录从配置/环境变量读（或复用 av_auto 的 `_paths()`）；把 `/mnt/user/media/av-auto` 与 pending 队列纳入「已存在」集合并标注 `in_staging`。
- **可验证**：把 `MEDIA_AV_REMOTE` 指向不存在目录，当前会静默按「无 strm」继续（应改为告警）；在 staging 放一个番号，catalog 不应把它列为缺失。

#### P2-8 · catalog 的 MariaDB TSV 解析静默丢行、空番号计入总数

- **证据**：`load_findav_titles` 逐行 `split("\t")`，`if len(parts) < 10: continue`（`catalog.py:62-65`）——**没有丢弃计数/告警**；`code` 为空的行仍 append（实测 4 行源数据含 1 行坏行 → 只解析 3 行正常 + 1 行空 code，坏行静默消失；`findav_titles` 用 `len(titles)` 统计，会把空 code 行算进去）。另外 `beauty_score` 直接 `float(bs)`（`catalog.py:75-82`），DB 里若出现非数值就整轮抛异常；`emby_item_id` 列读了但从不使用（`catalog.py:88`）。
- **影响**：报告里的「findav 条数」不可信；列数异常/字段含异常字符时无法定位；`float` 崩溃会让 `daily` 的 catalog 段整段失败（见 P2-15）。
- **建议**：解析失败计数并写进 summary/meta；空 code 单列计数；`float` 用 try 包+告警；删除未使用的 `emby_item_id` 或注释说明「全空，仅备查」。
- **可验证**：注入一条 3 列行与一条空 code 行，summary 应报 `parse_skipped=1`、`empty_code=1`（当前为 0/无字段）。

#### P2-9 · catalog judge 的 resume 语义与代码注释不一致

- **证据**：`run_catalog_judge` 从 `WORK/out`、`~/agent-xfer`、`/tmp/av-review-work/out` 收集 `av_catalog_judged_*.json`（`catalog.py:627-644`），但每个目录内部各自 `sorted(..., mtime)` 后再拼接（`catalog.py:632`），**不是全局 mtime 序**，注释「older first, newer overwrites」在跨目录时可能反；随后 `transport_fail` 先被赋了一个复杂表达式（`catalog.py:659-665`）又立刻用简化版覆盖（`:667-672`），第一段是死代码；resume 只读本地三目录，远端 `profile/av_catalog_judged_partial.json` 只写不读（`:752`、`:767`）；CLI 没有 `--resume/--no-resume` 开关。
- **影响**：新机器/清过 /tmp 后，远端 partial 不能续跑（要重判全部 → 重复付费）；跨目录旧文件可能覆盖新判定；代码难读。
- **建议**：合并候选后统一排序（按 mtime，相等时按文件名）；删死代码；resume 也拉远端 partial；加开关与 dry 统计。
- **可验证**：在 XFER 放一个旧 partial、WORK/out 放一个新结果，断言 resume 采用新结果（构造 mtime 交叉即可复现当前行为不可靠）。

#### P2-10 · 番号解析是第四套实现且比仓库 A 弱，可能产生假番号

- **证据**：`util.py:17-19` 的 `CODE_RE` 只有三条分支（FC2-PPV / `[A-Z]{2,10}-\d{2,5}` / `\d{6}[-_]\d{2,3}`），`norm_code`（`util.py:66-75`）不做零填充、只处理部分后缀；而仓库 A 有专门的 `av115_codes.py`：`_BAD_PREFIXES` 排除 `HDR/HEVC/MP4/...`（`av115_codes.py:10-41`）、`_HEYZO/_PONDO/_CARIB/_FC2` 分别解析（`:52-61`）、有码号码 `zfill(3)`（`:132-140`）、`-C/-UC` 标签语义（`:73-95`）。实测 `av_review`：
  `extract_code_from_text("Some.Movie.HDR-10.mkv") == "HDR-10"`、`"AAC-20 audio" == "AAC-20"`、`"1pondo-123456" == "PONDO-12345"`；整个 item `Path="/mnt/user/media/AV/Some Movie HDR-10/Some.Movie.mkv"` 返回 `HDR-10`；而 `"ABP-73"` 与仓库 A 产出的 `ABP-073` 不会归一。
- **影响**：AV 库里任何带 `HDR-10` 之类字样的名字都会变成假番号 → `catalog` 的 `emby_only` 多出噪声、`review --codes`/`from-json` 会去审一个不存在的条目；无码/FC2 格式与仓库 A 产出不一致时，`catalog` 会同时把同一部片算成「missing」和「emby_only」，`emby_item_id` 又全空无法纠正。
- **建议**：抽共享 `code_utils`（直接移植仓库 A 的 `av115_codes.parse_av` 规则），至少在 `av_review.util` 补 `_BAD_PREFIXES` 与 zfill；加一批跨仓对照用例（`SSIS-001-C`、`FC2PPV-1234567`、`123456_001`、`123456-001`、`HEYZO-1234`、`ABP-73`）。
- **可验证**：上述字符串的断言用例（harness 已复现）；再用真机 DB/Emby 抽样对比四套解析结果（见「待确认」第 4 条）。

#### P2-11 · 与 `scripts/av_auto.py` 大面积重复：两套抽帧、两套提示词、两套结果、两套 Grok 语义

- **证据**（只列对照，av_auto 内部问题归 02/03）：
  - 抽帧：av_review 180s/900s/mid 稀疏 2–3 帧（`vision.py:109-129`），av_auto 2min 起每 8min + 85%、最多 8 帧并做 DeepSeek 有效性预检、丢弃无效帧（`scripts/av_auto.py:63-65`、`:924-957`、`:1671-1746`）。
  - 图源：av_review 把 Primary+Thumb+Backdrop[0] 与帧一起送审（`vision.py:292-305`、`:436-459`）；av_auto 明确「只送有效抽帧」，Grok 更能只读帧、显式排除 covers/stills/Thumb/Backdrop（`scripts/av_auto.py:82-84`、`:1899-1960`、`bin/av_auto.README.md:76`）。
  - 提示词：`PROVEN_SYSTEM`（`constants.py:88-99`）与 av_auto `REVIEW_SYSTEM`（`scripts/av_auto.py:105-116`）是两份近似文案，已出现漂移（av_auto 多「只使用有效抽帧；不要根据无效空景…」）。
  - 结果：av_review 单文件聚合 `items[]`（无 per-code 持久）；av_auto 每番号 `frames/<code>/frames_manifest.json + review.json + grok_review.json` 持久到 `/config/scripts/av-auto-fs/frames/`（`scripts/av_auto.py:71-73`、`:2608-2650`、`bin/av_auto.README.md:51`）。
  - Grok：av_auto 有独立 `grok-review` 子命令 + `decide_tiebreak` 的 `kept_verdict/conclusion`（`scripts/av_auto.py:1839-2004`、`:2067-2146`）；av_review 的 `--grok-review`（P1-1）既不调用也不裁决。
  - fMP4：av_auto 有 `fmp4_http` 闭合 Range（`scripts/av_auto.py:47`、`:1318-1360`），av_review 没有（P1-6）。
  - 入口/模型白名单：av_auto 有 `_assert_allowed_model` 拒绝 minimax/grok 进默认 review（`scripts/av_auto.py:964-976`），av_review 的 `FORBIDDEN_MODELS` 常量（`constants.py:41`）从未被使用。
- **影响**：同一部片在「入库前（av_auto）」和「入库后（av_review）」会得到两套不同证据/不同提示词/不同格式的结论，可能一个 pass 一个 fail，且没有任何仲裁/引用字段；修复（fMP4、预检、Grok 裁决）只在一边做，另一边持续落后；同一份 Magpie 配额被买两次。
- **建议（去重路径，按成本从低到高）**：(a) 抽 `av_common` 共享模块：`code_utils`（直接用仓库 A 规则）、`frames`（含 fmp4）、`llm`（`chat_with_retry`/prompt 常量/JSON 解析）、统一 per-code 结果 schema；(b) av_review 对「av_auto 已审过且已 promote」的番号直接消费 `review.json`/`grok_review.json`（存在则引用，不重复调模型），只补审 av_auto 没有的；(c) 若要保留两套独立审计，至少在结果里写 `source_pipeline`、互相引用，并在 summary 里列出「两套结论冲突」清单。
- **可验证**：对同一 code 分别跑 av_auto `review` 与 av_review `review`（stub 或真机），比较 verdict/证据帧；去重后应能证明 av_review 不再对已有 `review.json` 的 code 调模型。

#### P2-12 · 产物与提示文案的误导/噪音

- **证据**：
  - 「无 redirect」被写成「ffprobe 失败或超时」：`annotate_timeouts` 只看 `ff.ok_probe is False`（`vision.py:500-501`），而 `run_ffmpeg` 对没有 http `MediaSources[0].Path` 的条目直接 `error="no redirect URL"`（`vision.py:316-319`）——两种完全不同原因同一句 note。
  - 短片恒提示「未抽到 900s 帧」：`judge_all` 只要 `frame_900s` 不在 `frames` 就加 note（`vision.py:445-446`），而 `seek_points` 对 `duration<=930` 本来就不会抽 900s（`vision.py:121-122`）——所有 <15.5min 的视频都会带这条误导性缺省。
  - MD 标题重复：「# Emby AV 识图复查 — Emby 已入库 AV 识图复查（DeepSeek-flash + gpt-6-luna）」（`write_md` 前缀 + `doc.title`，`vision.py:543`、`:681`，实测）。
  - review 产物没有 `latest`（只有 catalog 有），结果 JSON 里也没有 `written` 字段（`written` 只加在返回值，`vision.py:703`）。
- **影响**：人工判断成本上升、容易把「无帧」与「无 ffprobe」混为一谈；下游想找「最新复查」只能 glob+排序。
- **建议**：note 按原因分类（`no_redirect/no_probe/frame_900_unavailable_by_length/...`）；对短片不加 900s note；MD 标题去重；review 也发布 `av_review_vision_latest.{json,md}` 并把 `written` 写进文件。
- **可验证**：stub 无 redirect 的视频与 600s 视频，检查 `evidence.missing_notes` 是不同文案；检查产物目录出现 `latest`。

#### P2-13 · 无法对已扫条目重审/单独补 Grok

- **证据**：`--codes` 也受 `--skip-scanned`（默认 True）约束（`inventory.plan_review:76-97`）；实测 `plan_review(codes=["SSIS-001"])` 在已扫集合含该 code 时 `n_todo=0`、`skipped_scanned=["SSIS-001"]`，必须显式 `--no-skip-scanned`。没有「只跑 Grok」的子命令（对比 av_auto 的 `grok-review`），且跑完一轮后 `cleanup_temps` 已把帧删掉（`vision.py:573-587`、`:664`），想事后补 Grok 也无图可送（P1-1 修好后同样受限）。
- **影响**：用户按 README 想「对分歧补一次 Grok」时，最自然的命令 `review --codes ... --grok-review` 会静默返回「没有需要新扫的条目」；想重审得知道隐藏开关；帧不持久化导致无法复盘。
- **建议**：README 明确重审命令（`--no-skip-scanned`）；加 `--grok-only`（或独立 `grok-review` 子命令）从上次结果的 `images`/可重建帧路径复审；帧按 run id 保留到整轮成功（或保留 1 天）。
- **可验证**：先跑一次，再 `review --codes X --grok-review`（当前无动作），加 `--no-skip-scanned` 后应能重扫；grok-only 应只对分歧条目调用 Grok 且不重跑双模型。

#### P2-14 · 配置/部署硬编码与多副本风险

- **证据**：`constants.py:8-22` 写死 `root@192.168.1.9`、`http://192.168.1.9:8096`、`http://192.168.1.9:3430`、`/mnt/user/appdata/findav/...`、`/mnt/user/media/AV`；`HOME=/home/yuanhao/av-review`（`:28`）；README 第 3-4 行说明代码在 grokbot-vm，并且 Unraid 与 agent-xfer 各有一份镜像副本；`bin/av_review-api-start.sh:6` 用写死的 HOME 路径；`systemd/av-review-api.service:10` 也是绝对路径；无 `requirements.txt`、无版本 pin（P0-1/P2-1）；`FORBIDDEN_MODELS` 未使用（P2-11）、`HARD_SKIP` 写死两个番号（`constants.py:43`）。
- **影响**：换机/换 IP/改 `strm_root` 即静默失配（P2-7）；「改仓库 ≠ 改生产」，实际跑的可能不是这份代码（与 Lead 00 F8 一致）。
- **建议**：抽 `config.toml`/环境变量（host/port/paths/roots）；README 写部署自检（代码 hash、python 版本、依赖版本、可达性）；发布脚本同步三处副本并打印 hash。
- **可验证**：改环境变量后 `health`/`catalog` 应指向新目标；`health` 输出代码 hash/版本。

#### P2-15 · `daily` 的错误语义与成本归因

- **证据**：`cmd_daily` 先 `run_review(cfg)` 再 `run_catalog(...)`（`cli.py:198-216`），中途没有 per-step try；`run_review` 抛异常（P0/P1-3 任一路径）会让**整个 daily 进程退出、catalog 短名单不刷新**，与 README 第 56 行「若当时有识图在跑…仍刷新短名单」以及「daily 会刷新短名单」的承诺不符；cron 输出只是一段 traceback（`cron/av-review.cron:12`）。成本：`compute_cost` 只按 `[started_ts, now]` 时间窗过滤 usage（`cost.py:27-28`、`39-51`），窗口内若还有别的扫描/判断在跑，其 token 会被算进本轮；且只有整轮成功走到最后才核算（`vision.py:666-676`），失败轮成本不可见。
- **影响**：一次失败既没有产物也没有成本记录；日常报表/配额判断失真。
- **建议**：`daily` 每段独立 try/except 且汇总 `steps: {review: ok|error, catalog: ...}`；异常时仍尝试 catalog；成本按 run-id/请求日志关联（Magpie usage 若有 request id 就用），或在成本里标注「窗口内混入其他调用」风险。
- **可验证**：注入 review 异常，daily 仍应产出 catalog 与结构化 JSON、退出码非 0；比对并发时段成本与实际调用数。

## 四方数据一致性（findav DB / Emby / av_items_raw / local strm）

| 数据源 | 生成者 | `av_review` 的使用方式 | 已知缺口 |
|---|---|---|---|
| findav MariaDB `av_title` | 仓外 findav 流程（`findav-db` 容器） | `catalog` 只读 `SELECT` 15 列（`catalog.py:37-52`）；`--judge` 用其中的 title/actress/beauty/erotic/user_verdict | `emby_item_id` 全空（由 catalog 自己在 MD 里承认，`catalog.py:214`）→ 只能按番号；不读 `av_magnet`（P1-7）；无任何写回；`code` 格式未验证（P2-10）；列解析易静默丢行（P2-8） |
| Emby（含 MetaTube ProviderIds） | Emby + MetaTube 插件（仓库 A 触发） | 不直连盘点，只按快照里的 `Id` 取 `/Items?Ids=` 与图片；`ProviderIds` 只在路径无番号时兜底（`util.py:95-101`） | 快照过期/条目重建即失配（P1-5）；不校验 MetaTube 元数据是否正确；DB↔Emby 只对存在性，不比 title/actress（`catalog.py:288-292`） |
| `profile/av_items_raw.json` | **仓外**（仓库内无生成器） | review 的 Emby 清单、catalog 的 Emby 集合 | 无新鲜度/条数校验（`TotalRecordCount` 有但不比，`catalog.py:122-129`）；两个模块各自重复 scp（`inventory.py:19`、`catalog.py:103`）；并发时抢同一本地缓存路径（P1-2） |
| `/mnt/user/media/AV` 的 strm | 仓库 A `write_library`（`<演员>/<番号>`，`av115_library.py:57-61`）与 av_auto `promote`（`<番号>` 扁平，`scripts/av_auto.py:2205-2215`） | `catalog` 只按 basename 补番号（`catalog.py:132-148`）；`review` 不读目录，只经 Emby | 两种目录形状（Lead 00 F3）→ 同一番号可能出现两份 strm / Emby 重复条目；`catalog` 用 `by.setdefault`/集合按 code 折叠，重复条目只算一个、`review` 去重后只审第一个 `emby_id`（`inventory.py:69-74`），第二个重复条目永远不被审；目录硬编码（P2-7） |
| `/mnt/user/media/av-auto` + pending 队列 | av_auto | **完全不感知** | 已下完/已审完待 promote 的番号会被 catalog 当「库外候选」再次建议交磁（P2-7） |

补充：`av_title` 的 `user_verdict`（pending/skip/dislike 等）只用于 `rule_prefilter` 排除（`catalog.py:167-172`），**审核结论从不回收进该字段**——所以「审核结果写回数据库」这条最自然的数据闭环也不存在（P1-7）。历史 `av_mismatch_*` 结果只被用来跳过番号（`util.py:174-198`），其原始「元数据不一致对账」职责在 `av_review` 里没有替代实现（需确认是否由仍在跑的 rest/mismatch 外部流程承担）。

## 与 `scripts/av_auto.py` 的重复与衔接建议

| 维度 | av_review（库内复查/库外候选） | av_auto（入库前采集+终审） | 处理建议 |
|---|---|---|---|
| 触发 | cron daily 19:30 / LAN API / 手动 `review` | `process`/`intake` 显式 + 仓外调度器 | 明确「谁在哪个阶段审」；避免同一 code 两阶段重复付费 |
| 对象 | Emby 已入库（来自 av_items_raw 快照） | /av-auto staging 新片 | av_review 应跳过已在 av_auto 有 `review.json` 且已 promote 的 code（引用其结论） |
| 图源 | Primary+Thumb+Backdrop[0]+稀疏帧（`vision.py:292-305`） | 只有效帧；Grok 还能只读帧并排除封面（`scripts/av_auto.py:82-84`） | 抽共享 frame/prompt 模块；av_review 至少要能区分「有帧/仅封面」并降级标注 |
| 抽帧 | 180s/900s/mid，≤3 帧，无预检，无 fMP4 | 2min 起每 8min+85%，≤8 帧，DeepSeek 预检，fMP4 闭合 Range | 直接复用 av_auto 的实现/参数；这是最大的重复与质量差 |
| 提示词 | `PROVEN_SYSTEM`（`constants.py:88-99`） | `REVIEW_SYSTEM`（`scripts/av_auto.py:105-116`） | 抽到共享常量，避免文案漂移 |
| 结果 | 单文件聚合 `items[]`，无 per-code 持久 | 每 code `frames_manifest.json`/`review.json`/`grok_review.json` 持久 | av_review 改为 per-code 结果 + 聚合索引；或至少输出同构字段 |
| Grok | `--grok-review`（当前失效） | `grok-review` + `kept_verdict`（`scripts/av_auto.py:1839-2004`） | av_review 复用 `decide_tiebreak`，并把结论写回 consensus/MD |
| 失败语义 | HTTP 失败=uncertain 且记入已扫 | resume 时剔除 transport failure | 统一「失败不算已审」 |
| 番号 | `util.CODE_RE/norm_code` | `CATALOG_CODE_RE/_norm_code` + 复用仓库 A `av115_codes` | 统一到仓库 A 的 `parse_av` |
| 帧持久化 | 每 chunk 结束删 | 持久目录，写完才算成功 | 结论：要么 av_review 不删帧（供复盘/补 Grok），要么明确「一次性」并在产物里说明 |

**结论**：两套审核不是「互补」而是「两条独立且逐渐分叉的实现」。短期最划算的是把 **代码规则 / 抽帧（含 fmp4）/ LLM 调用与提示词 / 结果 schema** 抽公共模块；中期让 av_review 消费 av_auto 的 `review.json`，把 av_review 定位成「已入库后的二次抽查/漂移检测」而不是「同样的活再干一遍」；这样也自然解决 P1-1/P1-6/P2-11。

## 已确认正常（审计中核对过）

1. **单测基线符合预期**：`python3 -m unittest discover -s tests -v` = 60 用例 / 58 过 / 2 个 `ffmpeg` 缺失环境错误；**但 0 个 av_review 用例**（这是 P0-1 没被发现的原因，见 P2-1）。
2. **不写 `media_llm_route.json`**：全程只有 `stat -c` + `md5sum`（`vision.py:154-158`、`cli.py:109`），结果里记录 before/after md5 与 `unchanged`（`vision.py:638-641`、`678-689`），未发现任何写该文件的代码。
3. **模型白名单符合 README**：默认硬编码 `deepseek/deepseek-flash` + `chatgpt-api/gpt-6-luna`（`constants.py:39`），Grok 仅 `--grok-review` 显式触发（`vision.py:661-662`），全仓库无 MiniMax 调用点（`grep -i minimax av_review/` 只有 `constants.py` 的禁用常量）。
4. **密钥加载方向正确**：只从 Unraid 的 `emby.env` / `caller-keys.json` scp 到 `700` 的 `secrets/cache` 并 `chmod 600`（`secrets.py:21-45`）；Magpie key 走 `Authorization: Bearer` header（`vision.py:184-189`、`catalog.py:418-422`）；`load_env` 不打印值。Emby key 放 URL 是唯一例外（P1-8）。
5. **API token 处理正确**：随机 24 字节 hex、文件 600、`hmac.compare_digest` 等值比较、支持 `Authorization: Bearer` 与 `X-AV-Review-Token`、显式拒绝 query/body 里出现 `api_key/key/token/...`（`api.py:18-33`、`113-123`）；`/health` 响应不含密钥；日志走 `redact()`（该方法本身有缺陷 P2-2，但当前日志不含头）。
6. **结果文件保护有效**：`PROTECTED_BASENAMES` 覆盖历史 mismatches / route / `av_items_raw.json` / user_profile（`constants.py:46-58`）；`assert_safe_basename` 实测拒绝 `av_mismatch_scan.json`、`../av_items_raw.json`；正式结果带时间戳，不覆盖历史。
7. **dry-run 语义（review/catalog 非 judge）正确**：`review --dry-run`/`--limit 0` 只出计划不取图不调模型（`vision.py:617-620`）；`catalog --dry-run` 只返回摘要不写 profile（`catalog.py:356-359`）。例外是 `catalog --judge --dry-run`（P2-4）。
8. **catalog 核心对照计算正确**：合成 DB 7 行 / Emby 3 码 / strm 2 文件实测：`missing = DB − (Emby ∪ strm补码)`、`in_both`、`emby_only`、prefilter 分桶与排序、`user_verdict=skip` 排除、`limit`、`--no-strm` 均符合预期；`--judge` 的分层（建议入库/不建议/再看）、增量落盘、resume、transport 过滤代码路径存在且实测计数合理（口径问题见 P2-6）。
9. **抽帧点位函数符合设计**：实测 120/210/211/930/931/1200/1800/3600s 输出（180s、900s、mid；>930 且 mid 撞 900 时改 70%），去重间隔 30s 生效。
10. **只读性**：findav 只 `SELECT`；Emby 只 `GET /Items`、`/Images`；不删库、不移原片、不写 Emby；`grep` 无 INSERT/UPDATE/DELETE/Emby 写请求。
11. **成本核算数学正确**：合成 usage 实测 deepseek 高峰 ¥2.8/百万→…… 合计 ¥4.2、谷时半价、luna $0.10/$0.50 估算、grok 只记 token、窗口容差 5s/30s 与说明一致（`cost.py:18-144`）。
12. **环境隔离有做**：`WORK=/tmp/av-review-work` 与 rest 扫描的 `/tmp/rest-vision-*` 明确分开（`constants.py:33-34`、`vision.py:626-627`），远端脚本也写在独立路径（`bin/av_review_ff_batch.sh:6`）。

## 待确认（需要真机 / 生产数据）

1. **宿主 python 版本**：grokbot-vm/Unraid 上 `bin/av_review` 与 systemd 用的 `/usr/bin/python3` 到底是 3.11 还是 3.12。≥3.12 则 P0-1 不触发（PEP 701），但仓库未声明/未固定版本，本审计环境 3.11 实测必现——请先确认，再决定 P0 是否降级。
2. **Emby strm 的实际形态**：`.strm` 在 Emby 里 `MediaSources[0].Path` 是否真为 `http...`（`vision.py:316` 依赖）；embyserver 容器内的 ffprobe 能否跟随 MoviePilot `/redirect` 1 跳到 115 CDN（仓库 A `__init__.py:371-405`）；fMP4 片源占比决定 P1-6 的实际影响面。
3. **`av_items_raw.json` 的生成者/刷新频率/字段**：是否含 `ProviderIds`/`TotalRecordCount`，是否与 Emby 同步刷新，是否有截断（P1-5）。
4. **findav `av_title.code` 的真实格式**：与仓库 A `labeled`（`FC2-PPV-xxxx` / `HEYZO-xxxx` / `123456_001` / `123456-001` / `PREFIX-0NN`）是否一致；`emby_item_id` 是否仍全空（决定 P2-10 的实际影响）。
5. **历史 `av_mismatch_vision_{pilot5,mid,low,grok4,grok_low5,rest}.json` 的 schema**：`codes_from_result_file` 只认「顶层 list」或「dict.items[]」（`util.py:142-161`）；若实际是 `rows`/`results`/`mismatches` 键，skip-scanned 会静默失效、导致全量重扫（成本）。
6. **Magpie `usage.jsonl` 字段**：`req/model/provider` 是否与 `cost.py:39-51` 的匹配条件一致；`cursor/grok-4.7` 当前是否可用、成本口径。
7. **pickcode URL 是否落进远端错误日志**：`bin/av_review_ff_batch.sh:24/38` 的重定向只传 stderr 到 `$OUTROOT/logs/<code>_*.err`，ffprobe/ffmpeg 错误信息常带输入 URL；异常中断时这些日志与 `jobs_*.jsonl` 会在 Unraid `/tmp/av-review-work` 保留到下次成功运行（P1-3）。需真机看实际内容。
8. **仓外消费者/调度器**：`profile/av_review_vision_*`、`av_catalog_missing_latest.*`、`av_catalog_judged_latest.*` 是否被某个仓外脚本/人工 SOP 读取（与 Lead 00 F5 的待确认项相同）。
9. **`--grok-review` 修好后 Grok 的实际产出质量**（`cursor/grok-4.7` 对分歧的裁决一致性）。
10. **`connection.env` 的写法**：`load_env`（`util.py:32-40`）不支持 `export KEY=...`；`_ssh_mariadb_tsv` 却用 `source` + `set -a`（`catalog.py:42-44`）。若 findav 的 env 文件带 `export`，`load_emby_key` 会读不到 key（目前只读 emby.env，需确认其格式）。

## 附：复现与证据脚本（均在 /tmp，非仓库）

- `/tmp/patchtest/av_review`：只修 P0-1 两行 f-string 的副本，用于行为验证；`/tmp/avr_probe.py`/`avr_harness1.py`（util/inventory）、`avr_harness2.py`（catalog + judge）、`avr_harness3.py`（vision e2e + grok）、`/tmp/avr_grok`（maybe_grok 单测）、成本/TSV/redact 小脚本。
- 关键实测量（供复核）：py_compile exit=1；`review --dry-run`/`daily --dry-run` SyntaxError；`unittest` 60/58/2；`grok_review` 在 e2e 中为 None；`catalog --judge --dry-run` 进入 judge 分支；`redact('Authorization: Bearer …')` 保留 token；`extract_code_from_text('HDR-10')=='HDR-10'`；catalog judge 产物 `n_todo=5`/`n_done=6`、`不建议` 行 DS/luna=None。
