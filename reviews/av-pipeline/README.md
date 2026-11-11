# mp-115 × av-review：AV 自动化链路整体评估

评估对象：
- **仓库 A**：[wyh137815840/mp-115](https://github.com/wyh137815840/mp-115)（MoviePilot V2 插件「115 AV刮削」v1.5.8，`main@3394c3f`）。职责：接收番号/磁力/ed2k → 115 离线 → 整理进正式目录 → 写 strm/nfo → 刷新 Emby（让 Emby/MetaTube 刮削）。
- **仓库 B**：[wyh137815840/av-review](https://github.com/wyh137815840/av-review)（`main@8337582`）。职责：`av_review` 基于 findav 数据库/Emby 已有内容做 AV 审核；`av_auto` 做 115 staging → 清理 → strm → 抽帧 → 双模型终审 → Grok 复审 → 显式 promote；`fmp4_http.py` 负责 fMP4 抽帧。
- 宿主：MoviePilot v2.15.6、Emby（Unraid）、findav MariaDB、Magpie LLM 网关。评审时为只读快照，未连接真实 115/Emby/Magpie。

> **一句话结论：两条链各自都能跑，但不是一条闭环流水线。** 仓库 B 里又长出了一条完整的「采集→整理→strm→Emby」管线（`av_auto`），并且直接 import 仓库 A 的私有模块、读它的插件配置；两套审核（`av_review` 库内复查、`av_auto` 入库前终审）互不知道对方。**3 个 P0（均有前置条件，需真机确认生产版本）**：`av_auto promote` 恒失败并反向撤销搬家、`av_review` 在 Python<3.12 无法启动、`av_auto` 审核失败被记成假成功。**22 条 P1（去重后约 18）**、**50 条 P2**。最关键的架构缺口是「审核结论 → 动作」没有实现，以及两套管线会写同一个 `/AV` 与同一个 Emby 库。

## 1. 与用户描述对照：实际是什么

| 用户描述 | 现实 | 完成度 |
| --- | --- | --- |
| 仓库 A 提供接口创建 115 离线 | 插件 `/task` API + 表单；此外 `av_auto submit` 另开一条 p115client 直提 | 90% |
| 仓库 A 离线完成 → 正式目录 | mp-115 `_settle_downloads`→`/AV/<番号>`；av_auto 另走 `/av-auto/_staging`→`/av-auto`→`promote`→`/AV` | 80% |
| 仓库 A 转 strm | mp-115 写 `<strm_root>/<演员>/<番号>/`+nfo/封面；av_auto promote 写扁平 `<strm_root>/<番号>/` 且不写 nfo | 75% |
| 仓库 A 通知 Emby 调 MetaTube 刮削 | 两者都只做 Emby 库刷新（mp-115 走 MoviePilot EmbyModule，av_auto 走 Emby REST）；**都没有直接调 MetaTube，是否真触发取决于 Emby 的 NFO/MetaTube 优先级（待真机）** | 60% |
| 仓库 B 根据数据库已有内容做 AV 审核 | `av_review`：Emby 已入库条目抽帧识图复查 + findav 库外候选短名单；`av_auto`：入库前 staging 抽帧终审 | 80% |
| 审核 → 决策/动作 | 仓库内**没有**：`av_review` 只写 profile 报告，`av_auto` 的 `promote` 不读 `review.json`，无通知/标记/交磁/DB 回写 | 15% |
| 两仓协同（接口/数据/版本/锁） | 单向私有 import + 读插件配置；无版本约束、无共享锁、无统一番号/strm 规范 | 30% |

## 2. 三个 P0（分册发现，Lead 复核）

| 编号 | 结论 | 触发条件 | 证据 |
| --- | --- | --- | --- |
| **P0-A** | `av_auto promote` **恒失败并反向撤销搬家**：刚搬进 `/AV` 的整夹立刻被搬回 `/av-auto`，正式 strm 与 Emby 刷新永不执行 | 生产 `/config/plugins/av115scraper` 是 mp-115 **≥v1.5.1**（`Pan115._dirs` 缓存生效）。按被审 v1.5.8 必然触发；只有生产插件是 ≤v1.5.0 旧副本才幸免（**待确认**） | `scripts/av_auto.py:2195` 缓存 `dir_id(src)`，`:2223` 再用 `_id_ok(pan.dir_id(src_path))` 读同一缓存恒真 → `:2225-2229` 回滚；mp-115 `av115_pan.py:141-167` 的 `_dirs` 不随 move 失效 |
| **P0-B** | `av_review` 在 **Python < 3.12** 下 `review`/`daily`/API `/review` 全部 **SyntaxError 无法启动**（catalog/health 不受影响） | 目标机 `/usr/bin/python3` 为 3.10/3.11（Ubuntu 22.04 默认 3.10；本容器 3.11.2 实测必现）；3.12+ 不触发 | `av_review/vision.py:561,566` f-string 表达式内含 `\|`（PEP 701 才允许）；仓库/systemd/start.sh 均未声明 Python 版本 |
| **P0-C** | `av_auto` 审核段**假成功**：双模型调用全失败仍写 `review.json`（`agreement=agree`、`consensus=uncertain`）并返回 0；`_complete_review` 只看模型键不看 `ok`，重入永久 SKIP | Magpie 故障（5xx/429/超时/Key 失效）且该片此前无 `review.json` | `scripts/av_auto.py:1700-1716`（失败也记 uncertain）、`:1718-1727`（判成 agree）、`:1742-1745`（无条件写盘+返回 0）、`:2720-2734`、`:2848-2857` |

三条 P0 的共同点：**「文件存在」不等于「步骤成功」，「退出码 0」不等于「业务完成」**。修复前先确认两件事：生产 `mp-115` 插件版本、目标机 Python 版本——它们决定 P0-A/P0-B 是否已经在发生。

## 3. P1 汇总（去重后按主题）

完整证据/修复/验证见各分册。标 ★ 的是架构级缺口。

### 3.1 采集与正式库（仓库 A/B 交叉）

1. **★ 两条管线写同一个 `/AV` 与同一个 Emby 库**：mp-115 扫描会写 `<strm_root>/<演员>/<番号>/`+nfo（`av115_scan.py:291-310`），av_auto promote 写扁平 `<strm_root>/<番号>/` 且无 nfo（`scripts/av_auto.py:2205-2215`）；`find_existing` 以 `movie.nfo` 为判据 → 同一番号两套目录/两份 strm/Emby 重复条目。只要 mp-115 定时扫描还开着，av_auto promote 过的番号必被插件再整理一次。
2. **promote 状态机不可恢复、回滚不完整、0 测试**：move 与写 strm 之间崩溃后重跑只会 `FAIL 正式库已有`；`_rollback` 只搬回 115，不撤销已写正式 strm（`av_auto.py:2191-2264`）。
3. **clean 有误删风险**：`_is_keeper` 比 mp-115 更狠（`.rmvb`、大小未知正片会被当垃圾 `fs_delete`），且 `clean` 无 `--dry-run`（`av_auto.py:636-649,2471-2477`）。
4. **配置解析不一致**：`av_auto` 取 `cloud_paths` 第一行原文，mp-115 `_library_path` 会跳过注释/空行（`av_auto.py:583` vs `__init__.py:605-610`）→ 可能把 `# 有码` 当目录。
5. **av_auto 忽略插件播放/刷新配置**：写死 `play_base_lan or play_base`，不读 `strm_use_lan`；也没有 `refresh_emby` 开关（`av_auto.py:585-589`）。
6. **★ 对 mp-115 的私有耦合**：`PLUGIN_DIR=/config/plugins/av115scraper`、`sys.path` 后 import `av115_pan/av115_library` 及 `_file_id/_is_directory/_name/_size` 私有函数，读 MoviePilot `user.db` 的 `plugin.Av115Scraper`（`av_auto.py:49,517-601,603-609,653,683`）；mp-115 升级/搬模块即断。P0-A 就是这种耦合的实例。
7. **promote 不读审核结论**：`cmd_promote` 完全不看 `review.json/grok_review.json`（`av_auto.py:2191-2280`）。

### 3.2 审核（av_review + av_auto review）

8. **★ 审核结果没有下游消费者**：`av_review` 只写 `profile/av_review_vision_*`、`av_catalog_missing_latest.*`；`av_auto` 只写每部 `review.json/grok_review.json`；无通知/标记/交磁/DB 回写，`process` 退出码不体现结论，`promote` 不设闸门（01-P1-7、03-P1-2、Lead F5）。
9. **`av_review --grok-review` 是死功能**：`judge_all` 的 `item_out` 不带 `image_files`，`maybe_grok` 永远「无图可复审」（`vision.py:450-459 vs 599-608`），Grok 实际从不调用。
10. **并发/忙闲保护失效**：`busy.py` 的 `VisionLock` 全仓无调用；`BUSY_PATTERNS` 漏判 cron 实际命令 `bin/av_review daily` 与 `catalog --judge`；API 检查后立刻 Popen（TOCTOU）；共享固定 `/tmp/av-review-work` 目录（01-P1-2、04-F-P1-02）。
11. **无增量落盘/无异常隔离**：`items` 只在所有 chunk 结束后写盘，任一 ssh/scp/probe 异常 → 整轮结果全丢、远端残留（01-P1-3）。
12. **瞬时失败被永久记成已扫**：双 503 → 两家「一致 uncertain」，`collect_scanned_codes` 不看 verdict → 该番号以后永久跳过（01-P1-4）。
13. **Emby 清单只有仓外快照**：`av_items_raw.json` 由仓外生成、无新鲜度/条数校验；`emby_id` 缺失无兜底（01-P1-5、Lead F6）。
14. **`av_review` 无 fMP4 分支**：把 115 strm URL 直接给 `ffprobe/ffmpeg -ss`，fragmented MP4 会 `partial file` 一包不出（`bin/av_review_ff_batch.sh:24,38`）；同仓 av_auto 已有 fmp4 闭合 Range，未复用。
15. **Grok 失败仍落盘并视为已完成**：`grok_review.json` 写入 `ok:false/uncertain` 后 `_grok_already_done` 只看 trigger+verdict，未决永不重试（03-P1-1）。
16. **Emby API key 进 URL**：`vision.py:82-84` 每请求 `?api_key=`，违反 README「Key 不进 URL」。

### 3.3 工程与部署

17. **部署副本无同步/版本机制**：实际执行的是容器内 `/config/scripts/av_auto.py`，仓库 `scripts/` 不是运行副本；无 `--version`/commit 输出，依赖 `av_auto_apply_fs` 不在仓库（04-F-P1-04、Lead F8）。
18. **fMP4 解析层 0 单测且测试绑死 ffmpeg**：`fmp4_http.py` 函数进入 6/48、语句行 6.4%；6 个 fmp4 测试在无 ffmpeg 时是 `error` 而不是 `skip`（04-F-P1-03）。
19. **无 CI、无依赖声明、Python 版本未声明**：`av_review` 60 个测试 0 覆盖；两个仓库都没有 `requirements.txt`/workflow（01/04/05）。

## 4. P2 速览（共 50 条，详见分册）

- **fmp4 实现**：sidx 子段带 `styp` 时 materialize 硬失败且不退回 scan；`moof` 与 `mdat` 间夹 `free` 时静默产出无 mdat 的 snippet；`prev+cur` 双片 cap 检查自相矛盾（默认 32MB 下两片各 16MB 即失败）；无 sidx/mfra 时线性扫全片且 Range 无 pacing（04）。
- **av_auto 审核段**：长片只取最早 7 个网格点+85%（2h 片 50–102min 无帧）；短片 120–210s 只出 1 帧；`_nearby_times` 向后补点是死代码；预检失败固化 valid=true 且重入不再预检；模型门是 denylist + runtime 可换模型矛盾；复用帧重标时间；结论未与 strm/帧内容绑定；无 per-code 锁、manifest/review 非原子写；Grok 成本缺失、无批次汇总；无运行日志/最终结论；持久帧无保留策略；`process` 仅捕 `SystemExit` 且无全局时间预算（03）。
- **av_review**：结果文件秒级命名可覆盖；`--judge` 空档检查 TOCTOU；cost 估算口径；`/health` 无鉴权却 ssh 返回进程 cmdline；secrets 长期缓存；API token 比较/文件权限；`catalog --judge` 并发 8 workers 可能打 Magpie 配额（01）。
- **部署/入口**：cron 的 `logs/` 目录未建、cron 与 systemd 双启动 API、无 logrotate、TZ 依赖未校验；`bin/av_auto` 的 `--url` heredoc 卫生（04/02）。
- **mp-115 侧**：见独立评审 `reviews/mp-115-v1.5.8/`（1 个 P0：aps 分页大目录漏扫/可能死循环；16 个 P1）。

## 5. 架构建议（先做这 5 件）

1. **定权威边界 + 稳定接口**：建议 mp-115 负责「离线接口 + 正式库整理 + 刮削 + Emby 通知」，av_auto 只负责「审核 + 显式 promote + 审核结果动作」；用公开函数/HTTP API 替代私有 import，加契约自检与版本声明。
2. **统一 strm/目录规范与 nfo 归属**：`<演员>/<番号>/` 或 `<番号>/` 二选一；明确「谁写 nfo、谁触发 MetaTube」（真机验证 Emby 的 NFO/MetaTube 优先级）。
3. **补「审核 → 动作」闭环**：给 `process` 加可解析的结论汇总与退出码；`promote` 默认要求 review 结论为 pass（`--force` 可越过）；或独立 `apply-review` 入口 + 调度器契约；`av_review` 的 pass/fail 也落成可查询的稳定文件。
4. **修 3 个 P0 + 补最小工程门禁**：promote 缓存核验、Python 3.11 语法、review 假成功；加 CI（`py_compile` + `unittest` + import 冒烟）、`requirements.txt`、部署同步/版本自检。
5. **消重**：统一番号解析器（4 套→1 套）、统一提示词/模型白名单/结果 schema/成本口径、两条审核互相同步已完成状态避免重复花钱；给两条管线加跨进程锁（115/Emby 写）。

## 6. 完成度评分

| 维度 | 分数 | 依据 |
| --- | --- | --- |
| 采集/离线/整理 | 80% | 两条实现都在，但 promote P0 + 重复/误删风险 |
| strm/元数据/Emby | 60% | 能写能刷；目录形状不一致、MetaTube 触发未验证 |
| 库内审核（av_review） | 55%（人工决策支持）/ 15%（自动闭环） | 报告能出；P0-B 阻塞、无下游动作、无锁 |
| 入库前审核（av_auto） | 70%（正常路径）/ 30%（失败路径） | 持久化/可追溯做得好；假成功 + 无闸门 |
| 两仓协同 | 30% | 私有 import、无版本、无锁、无统一规范 |
| 工程/部署 | 35% | 无 CI/版本/同步；测试覆盖不均 |

## 7. 复现与验证

```bash
# 仓库 B 基线（本机无 ffmpeg）：66 个 test 方法，Ran 60 / 58 pass / 2 error（环境性）
cd /workspace/av-review-main && python3 -m unittest discover -s tests -v

# P0-B（Python 3.11 实测）
python3 -m py_compile av_review/vision.py     # SyntaxError: f-string ... backslash
python3 -c "import av_review.vision"          # 同上

# P0-A（伪 Pan115 复现 promote 反向撤销；见分册 02 附录）
#   期望（修复前）：MOVES=[('2','1','4'),('2','4','1')]、strm_written=[]

# P0-C（fake Magpie 503 走真实 process；见分册 03 附录）
#   期望（修复前）：review.json agreement/consensus = agree/uncertain、process rc=0、第二次全 SKIP

# 各分册另附 /tmp 下的探针脚本与最小复现命令
```

## 8. 局限与待确认

- 全程只读仓库快照；**未连接真实 115/Emby/Magpie/Unraid/ssh**，未做线上端到端回归。3 个 P0 中 P0-A/P0-B 依赖生产前置条件（插件版本、Python 版本），P0-C 需 Magpie 故障触发——都需真机确认。
- 待确认：生产 mp-115 插件副本版本与挂载方式、grokbot-vm Python 版本、`av_items_raw.json` 生成者与新鲜度、findav `av_title.emby_item_id` 回填、Emby 的 NFO/MetaTube 优先级、仓外「批次调度器」的实现、`av_auto_apply_fs` 维护位置、115 aps 超 1200 项真实行为。
- 仓库 B 的 `main` 属于用户项目；本报告在评审分支发布，未改动 `main`。

## 9. 分册

- [00-pipeline-integration.md](findings/00-pipeline-integration.md) — Lead：两仓职责/数据流/结构性发现 F1–F10
- [01-av-review-core.md](findings/01-av-review-core.md) — av_review 库内审核链路（P0×1、P1×8、P2×15）
- [02-av-auto-acquire.md](findings/02-av-auto-acquire.md) — av_auto 采集/落盘段与 mp-115 耦合（P0×1、P1×7、P2×10、P3×6）
- [03-av-auto-review.md](findings/03-av-auto-review.md) — av_auto 审核段（P0×1、P1×2、P2×12）
- [04-fmp4-tests-eng.md](findings/04-fmp4-tests-eng.md) — fMP4/测试/入口/部署/文档（P0×0、P1×5、P2×13）
- 相关：mp-115 独立评审 [`../mp-115-v1.5.8/`](../mp-115-v1.5.8/README.md)
