# 02 · av_auto 采集/落盘段审计（与 mp-115 插件的耦合）

- 被审仓库：`/workspace/av-review-main`（`wyh137815840/av-review` `main@8337582`，只读），重点 `scripts/av_auto.py:1-3061` 的采集/落盘段与 `bin/av_auto:1-319`。
- 对照仓库：`/workspace/mp-av`（`wyh137815840/mp-115` 插件「115 AV刮削」v1.5.8，`main@3394c3f`）；宿主 `/workspace/moviepilot` v2.15.6；已审报告 `/workspace/av-review/reviews/mp-115-v1.5.8/findings/`（下称 `00`/`04` 等）、Lead 总报告 `00-pipeline-integration.md`（下称 `00-Fx`）。
- 审计范围：`cmd_setup/status/submit/wait/clean/strm/intake/promote`、`_paths/_plugin_cfg/_load_cookie/_emby_auth/_emby_refresh`、`_import_plugin`（`av115_pan.Pan115` + `av115_library.*`）、115 目录操作、`_is_keeper`/clean 规则、ENOSPC pending（`_queue_fs/_perform_pending_job/apply_pending_queue/media_*`）、`bin/av_auto` 的 ssh/docker exec/`--url` 传参；只在与采集段衔接处提及 `process/frames/review`。
- 方法：逐行静态审计 + 与 mp-115 v1.5.8 / 宿主 v2.15.6 逐符号核对 + `/tmp` 内 stub/伪 115 复现 + 离线核对 p115client 0.0.9.7.2 wheel 源码。**没有连接任何 115/Emby/ssh/Unraid。**
- 基线（实测）：`cd /workspace/av-review-main && python3 -m unittest discover -s tests -v` → `Ran 60 tests in 1.303s`，58 过、2 个 `FileNotFoundError: ffmpeg` 环境错误（`test_fmp4_http`），与任务描述一致。
- 局限：115/Emby/真机行为无法验证的一律标「待确认」；本报告只覆盖仓库快照，不保证三处部署副本与该快照一致（`00-F8`）。

## 0. 一句话结论

**av_auto 的「交磁 → 等待 → 清杂质 → staging strm → 抽帧审核」这条前置链是完整的，但用户描述的最后一公里「离线完成转移到正式目录 → 转正式 strm → 通知 Emby 调 MetaTube」中的 `promote` 在当前 mp-115 v1.5.8 下永远失败并会把自己刚搬过去的 115 文件夹搬回来（P0-1，已用伪 Pan115 复现）；同时它与 mp-115 是两条并行管线：离线目录、整理规则、strm 目录形状、Emby 刷新、番号规则全部不一致，同一番号会得到两套目录/两份 strm/可能重复的 Emby 条目；升级 mp-115 会直接打断 av_auto（P0-1 就是 mp-115 v1.5.1 引入的 `dir_id` 缓存导致的现成案例）。**

## 1. 完成度（对照用户描述的四步）

| 步骤 | 实现位置 | 状态 | 说明 |
| --- | --- | --- | --- |
| ① 提供接口创建 115 离线 | `cmd_submit` `av_auto.py:2367-2395`（`--code/--url`）→ `Pan115.add_magnet`（`av115_pan.py:169-175`）→ p115client `clouddownload_task_add_url`；`cmd_intake` `:2485-2512` 串起全部前置 | ⚠️ 有 CLI、无 HTTP 接口 | 直接把链接交给 115，无本地任务表/去重/失败重试；依赖插件私有 `Pan115` 与未声明版本的 `p115client`；`--code` 不做番号校验；同一链接重复提交会产生重复离线任务 |
| ② 离线完成 → 转移正式目录 | `cmd_wait` `:2398-2424`（p115client `clouddownload_task_list`）→ `cmd_clean` `:2427-2482`（`/av-auto/_staging/<code>` → `/av-auto/<code>`）→ **`promote` `:2191-2266`（`/av-auto/<code>` → 正式库）** | ❌ `promote` 不可用（P0-1） | clean 只到 115 暂存库；真正“进正式目录”的 promote 在当前 mp-115 下恒失败；且无自动触发（README 明说“验证条不要自动调用”），依赖人工/仓外调度器 |
| ③ 转 strm | `cmd_strm` `:792-815`（staging `/media/av-auto/<code>/`）+ `_write_strm_dir` `:774-789`；正式 strm 只在 `promote` 成功路径写（`:2244-2253`） | ⚠️ 能写 staging，正式写被 P0 卡住 | 目录形状 `destination(strm_root, code, "")`→扁平 `/media/AV/<code>`；不写 nfo/封面；忽略插件 `strm_use_lan`；文件名/编码规则沿用插件 `build_play_url` |
| ④ 通知 Emby 调 MetaTube 刮削 | `_emby_refresh` `:2151-2188`（`/Library/Media/Updated` + `/Items/{libraryId}/Refresh`） | ⚠️ 只在 promote 成功时执行（当前=永不执行） | 不是“调用 MetaTube”，而是让 Emby 刷新媒体库；`MetadataRefreshMode=Default`、`ReplaceAllMetadata=false` 不会强制重刮已存在条目；`Media/Updated` 传的是容器路径 `/media/AV/...`，对 Emby 是否可见未验证；两套 Emby 刷新（本函数 vs mp-115 `_refresh_emby_library`）并存 |

**缺的步骤/能力**：① 无 115 任务状态持久化（mp-115 有 `tasks`/`last_run`）；② promote 与审核结论无强制关系（不读 `review.json`/`grok_review.json`，`00-F5`）；③ 不做 115 文件/夹改名、不写 nfo/封面（mp-115 有）；④ 无跨进程锁、无幂等状态机；⑤ 无自动串联（仓内没有调度器）。

## 2. 问题清单

### 2.1 P0

#### AQ-P0-1 · `promote` 恒失败：mp-115 的 `Pan115.dir_id` 缓存让“搬家后核验”永远认为旧目录还在，刚搬进 `/AV` 的夹会被立刻搬回

- **证据（av_auto 侧）**
  - `scripts/av_auto.py:2195` `src_id = pan.dir_id(src_path)` 在搬家前解析并**缓存**了 `/av-auto/<code>` 的 id；
  - `scripts/av_auto.py:2219` `pan.move(src_id, official_id)` 搬家；
  - `scripts/av_auto.py:2223` 核验：`if not _id_ok(pan.dir_id(dst_path)) or _id_ok(pan.dir_id(src_path)):`——`pan.dir_id(src_path)` 命中插件缓存，返回搬家前的旧 id，`_id_ok(...)` 恒为真，于是**无论搬家是否成功都进入失败分支**；
  - `scripts/av_auto.py:2225-2230`：若 `dst_path` 已可见，就 `pan.move(pan.dir_id(dst_path), staging_parent)` 把夹**搬回** `/av-auto`，返回 `FAIL ... 搬家后核验失败，已尝试留在/搬回 av-auto`；后续写正式 strm（`:2244-2253`）与 Emby 刷新（`:2255-2260`）永不执行；
  - `scripts/av_auto.py:2236-2240` `_rollback` 里的 `back = _id_ok(pan.dir_id(src_path))` 同样读缓存 → 永远报“115 已回滚到 av-auto”，无法据实报告。
- **证据（mp-115 侧）**：`plugins.v2/av115scraper/av115_pan.py:39` `self._dirs = {"/": "0"}`；`:141-150` `dir_id` 调 `_child_dir(..., fresh=False)`；`:152-167` `_child_dir` 命中 `self._dirs[path]` 就直接返回且只缓存命中项。该缓存是提交 `6dab82c`（2026-11-08 03:31 +0800，`package.v2.json` 版本 **v1.5.1**，“List 115 folders through aps…”）引入的——**在它之前 `dir_id` 每次现场 `/files/getid`，不存在这个校验陷阱**。注意时间线：av_auto 的首次提交是 2026-11-10，晚于该缓存，所以按被审版本 `promote` 可能**从未成功运行过**（也与 README“验证条不要自动调用 promote”一致）；只有当生产 `/config/plugins/av115scraper` 是一份 ≤v1.5.0 的旧副本时才会侥幸可用（见 AQ-P1-7，待确认）。
- **复现**（`/tmp/avauto-audit/t_promote.py`，按 `av115_pan.py` 的 `dir_id/_child_dir` 语义实现伪 Pan115，并替换 `_paths/_item_fields/_emby_refresh` 为桩）：
  ```
  RESULT: FAIL ABP-073: 搬家后核验失败，已尝试留在/搬回 av-auto
  MOVES: [('2', '1', '4'), ('2', '4', '1')]      # 先 /av-auto→/AV，再 /AV→/av-auto
  real_children_of_staging: ['ABP-073']           # 最终仍在 av-auto
  strm_written: []                                # 正式 strm 一个都没写
  ```
- **影响**：整条链路唯一的“入库”动作完全不可用且会反向撤销搬家；如果 `staging_parent` 解析失败则夹会留在 `/AV` 但没有正式 strm，形成半成品（见 AQ-P1-4）；每次尝试还会在 115 上多打两次 move（风控/异步可见性风险）。
- **建议修复**：核验不要用带缓存的 `dir_id`——
  1. 搬家后用 `list_children(official_id)`/`list_children(staging_parent_id)` 现场比对名字，或 `pan._dirs.pop(path, None)` 后重查（后者仍是私有耦合）；
  2. 更好的做法是让 mp-115 提供 `dir_id(path, fresh=True)` 之类的公开语义，av_auto 只用公开 API；
  3. 重入时幂等：`dst` 已存在且正式 strm 已存在 → 打印 `SKIP` 返回 0，而不是 `FAIL`；
  4. 补一条“带缓存语义的 FakePan”单测（现有测试没有任何 promote 用例，见 AQ-P2-8）。
- **可验证方式**：跑 `/tmp/avauto-audit/t_promote.py`（修复前恒 FAIL 且两次 move；修复后应只 move 一次、写出 `/media/AV/ABP-073/ABP-073.strm`、`MOVES` 长度 1）。真机：`av_auto promote --dry-run X` 后 `av_auto promote X`，观察 115 文件夹最终所在位置与 `/media/AV/X/` 是否出现。
- **备注**：这是 `00-F2`“升级即断”的**现实发生版本**，不是未来风险；也说明 `promote` 段没有真机验收（README 只写了“验证条不要自动调用”，见 AQ-P2-8 的覆盖缺口）。

### 2.2 P1

#### AQ-P1-1 · `_paths()` 用 `cloud_paths` 第一行原文，与 mp-115 `_library_path()` 的“跳过注释/空行”不一致 → promote 目标错、`setup` 会在 115 根建出 `# 有码` 之类目录

- **证据**：`scripts/av_auto.py:583` `official_115 = str(cfg.get("cloud_paths") or "/AV").splitlines()[0].strip() or "/AV"`，不跳过 `#` 注释和空行；mp-115 `__init__.py:605-610` `_library_path()` 会跳过空行/`#` 行（且补前导 `/`）。实测（`/tmp/avauto-audit/t_paths.py`）：
  - `cloud_paths="# 有码\n/AV/有码\n/AV/FC2"` → av_auto `official_115="# 有码"`（经 `av115_scan.norm()` 变成 `/# 有码`），插件是 `/AV/有码`；
  - `cloud_paths="\n/AV/有码"` → av_auto `official_115="/AV"`，插件 `/AV/有码`；
  - `cmd_setup` `:702` `pan.dir_id(official_115) or pan.ensure_dir(official_115)` 会**主动创建**这个错误目录。
- **影响**：promote/`setup` 写到一个插件永远不会扫描的 115 目录（或 115 根建垃圾夹）；两侧“正式库”从此分裂，后续 strm、Emby、审核判断全部对不上。
- **建议**：直接复用插件 `_library_path()` 的规则（跳过空行与 `#`、补前导 `/`、空则回退 `/AV`），或从插件公开一个 `library_path()`；`setup/status/promote` 把解析结果打印出来并在不存在且非 `setup` 时明确报错。
- **可验证方式**：`/tmp/avauto-audit/t_paths.py`；修复后 comment-first 应仍得 `/AV/有码`。

#### AQ-P1-2 · clean 的 `_is_keeper` 比 mp-115 `_is_video` 更狠：`.rmvb` 正片、大小未知的正片会被当垃圾**删除**，且 clean 没有 dry-run

- **证据**：
  - av_auto `VIDEO_EXT`（`:53`）不含 `.rmvb`，而 mp-115 `av115_scan.py:14` 的 `VIDEO_EXTS` 含 `.rmvb`；
  - av_auto `_is_keeper`（`:636-649`）对 `size==0` 直接 `return False`（`:642-643`“未知大小=非 keeper”）；mp-115 `av115_scan.py:106-116` 的 `_is_video` 对未知大小是**保留**（`if item.size and item.size < min_bytes` 才拒）；
  - `cmd_clean`（`:2441-2482`）在存在 keeper 时，把 inbox 里所有非 keeper 文件/子夹用 `client.fs_delete`（`:2471/:2477`，115 `/rb/delete`）清掉——不是像 mp-115 `relocate_code`（`av115_pan.py:177-244`）那样把垃圾**搬回**云下载目录；
  - clean 没有 `--dry-run`（`build_parser` `:2984-2986`），`promote` 才有（`:3029`）。
- **实测**（`/tmp/avauto-audit/t_rules.py`，min 300MB）：
  ```
  rmvb (2000MB)      av_auto keeper=False   mp-115 keeps=True
  unknown-size       av_auto keeper=False   mp-115 keeps=True
  small (200MB)      av_auto keeper=False   mp-115 keeps=False
  good (2000MB)      av_auto keeper=True    mp-115 keeps=True
  ```
- **影响**：inbox 同时有一个 mp4 keeper 时，另一个 `.rmvb` 正片/大小字段缺失的正片会被静默删除（115 回收站可恢复，但若回收站被清就是**丢片**）；与 mp-115 对同一类文件的判定相反，两个管线的“正片”概念不一致。`.srt/.ass` 等辅助文件也一律删除而非保留/搬回。
- **建议**：把 keeper 规则收敛到一处（最好由 mp-115 暴露公开规则，或 av_auto 复刻并写测试矩阵）；未知大小 fail-open（保留）；clean 增加 `--dry-run` 和“将删除清单”先行输出；删除只走回收站并记录 fid 清单。
- **可验证方式**：`/tmp/avauto-audit/t_rules.py`；或构造一个含 `X.rmvb` + `Y.mp4` 的 inbox 跑 `cmd_clean`，断言 `X.rmvb` 的去向。

#### AQ-P1-3 · 两条管线写同一 `/AV` 与同一 Emby 库：strm 目录形状/文件名不同 → 同番号两套目录、两份 strm、Emby 重复条目

- **证据**：
  - mp-115：`av115_scan.py:291-295` 在 `existing is None` 时用 `destination(strm_root, labeled, actress)` → `/media/AV/<演员>/<labeled>/`，并 `write_library(...)`（`:300-310`）写 `<video stem>.strm` + `movie.nfo` + 封面；`find_existing`（`av115_library.py:37-54`）**以 `movie.nfo` 为判据**。扫描还会把 115 文件夹/文件改名为 `labeled`（`av115_scan.py:333-394`）。
  - av_auto：`_promote_one` `:2205-2206` `existing = find_existing(official_strm, code); dest = existing or destination(official_strm, code, "")` → `/media/AV/<code>/`（无演员层）；`_write_strm_dir` `:774-789` 只写 strm，**不写 nfo/封面**，单文件时命名为 `<code>.strm`，多文件时用原文件名 stem；不重命名 115 文件夹。
  - 触发时序（两仓都在运行时的必然结果）：av_auto promote → `/media/AV/<code>/<code>.strm`（此时没有 `movie.nfo`）→ mp-115 下一次 cron `/run` 扫到 promote 进 `cloud_paths[0]` 的 `<code>` 夹 → `find_existing` 因为无 nfo 找不到 av_auto 的目录 → 刮削后写 `/media/AV/<演员>/<labeled>/`（第二套目录 + 第二份 strm + nfo/封面）；若刮削失败，mp-115 会在扁平目录写 `movie.nfo` 和 `<labeled>.strm`——`labeled==code` 时与 av_auto 的 `<code>.strm` **同名，直接覆盖**（两份 URL 语义还不同，见 AQ-P1-6）。
  - 命名差：`code`（用户传，如 `ABP-073`）vs `labeled`（含 `-C`/`-UC`，如 `ABP-073-C`）——`find_existing`/`destination` 用错名字时命中率更低（`av115_codes.py:72-86`）。
- **影响**：Emby 库出现同番号两个条目/两份 strm（emby 不跨目录去重）；播放地址取决于最后写入者；`av_review` 的库内对照也会出现重复；用户看到的“Emby 重复条目”根因在此（`00-F3` 的模块级证据与触发序列）。
- **建议**：二选一并在 README 固化：① av_auto 复用 `write_library`/`destination`，按 `labeled`（必要时带演员）建目录并写 nfo，promote 与扫描同形；② av_auto 只把 strm 写进 mp-115 已刮好的既有目录（`find_existing` 命不中就拒绝 promote 并提示先跑插件扫描），同时把 av_auto 写入的 115 夹从 mp-115 的 `cloud_paths` 排除（或给插件加排除目录配置）。
- **可验证方式**：真机上对同一番号先 `av_auto promote` 再触发一次插件扫描，`find /media/AV -path '*<code>*'` 应只有一处；当前会出现 `/media/AV/<code>/` 与 `/media/AV/<演员>/<labeled>/` 两处。

#### AQ-P1-4 · promote 不是可恢复状态机：move 与写 strm 之间有崩溃窗口，重跑只会 `FAIL 正式库已有`，工具内没有任何恢复路径

- **证据**：`_promote_one` 顺序为 115 move（`:2217-2230`）→ 写正式 strm（`:2244-2253`）→ Emby（`:2255-2260`）→ 清 staging strm（`:2262-2264`），全程无状态文件/日志标记；若进程在 move 之后、strm 之前被杀：
  - `/AV/<code>` 已存在，`/media/AV/<code>/*.strm` 不存在，`/media/av-auto/<code>/` 还在；
  - 重跑 promote：`:2198` `if _id_ok(pan.dir_id(dst_path)): return FAIL "正式库已有 ... 整夹留在 av-auto，未搬"` → 永远 FAIL；
  - `cmd_strm`（`:801-811`）只读 `/av-auto/<code>`（已空）→ `SKIP ... 115 上没有`；
  - `frames/process` 的 `_find_strm_for_code`（`:1366-1390`）找不到 strm → 审核也无法继续；只能人工搬回/手写 strm。
  - 另外 `_rollback`（`:2233-2242`）只把 115 夹搬回，不清掉已经写了一半的 `/media/AV/<code>/` 和部分 strm；这些残留可能被 Emby 扫描/被 `_find_strm_for_code` 命中。
- **影响**：一次 kill/断网/ssh 掉线即可把某番号锁死在“半入库”状态，且没有任何命令能自愈；与“显式 promote、可反复跑”的设计目标矛盾。
- **建议**：加状态文件（如 `/config/scripts/av-auto-fs/promote/<code>.json`：moved/strm/emby 三步），重跑据此续跑；或在顺序上先写 strm（pickcode 与 115 目录位置无关，先写是安全的）再 move，让中途失败可重入；`dst 已存在` 时检查正式 strm 是否存在，缺则补写并返回 0（幂等）；`_rollback`/成功路径都清理 dest 半成品（只删本次自己写的文件）。
- **可验证方式**：stub Pan115 在 move 后抛异常 / 在第 2 个文件 write 后抛异常，断言重跑能补齐并最终 `OK`，而不是 `FAIL 正式库已有`。

#### AQ-P1-5 · `promote` 不读审核结论：`review.json`/`grok_review.json` 不是闸门，用户描述的“双模型终审 → promote”没有强制连接

- **证据**：`cmd_promote`（`:2268-2280`）只解析 codes 后逐个 `_promote_one`；`_promote_one` 只查 115 路径/成片/strm/Emby；仓库内唯一读 `review.json` 的地方是 `_complete_review`/`_process_review`（`:2720-2736`、`:2848-2865`）和 `grok-review`（`:2067-2148`）。`promote` 无 `--require-review` 之类的参数（`build_parser:3027-3030`）；也没有 `apply-review` 入口（`00-F5`）。
- **影响**：人工或仓外调度器可以对 `verdict=fail/uncertain` 的片子执行 promote，审核形同虚设；反过来 pass 的片子也不会自动入库，闭环仍缺一环。这是用户描述里“双模型终审”与“入库”之间断绝的**仓库内证据**。
- **建议**：`promote --require-review pass`（或默认要求）：读 `/config/scripts/av-auto-fs/frames/<code>/review.json` 的 `agreement/consensus`（必要时 `grok_review.json.tiebreak.kept_verdict`），不满足则 `FAIL` 并打印原因；再配一个 `apply-review`/调度器契约。
- **可验证方式**：伪造 `review.json`（fail）后 `promote --require-review pass` 应拒绝；现实现会照常搬家。

#### AQ-P1-6 · av_auto 忽略插件的 `strm_use_lan` 与 `refresh_emby` 开关：strm URL 与刷新行为跟 mp-115 不一致

- **证据**：`_paths`（`:585-589`）取 `cfg["play_base_lan"] or cfg["play_base"] or <写死的 192.168.1.9:3000 模板>`，**完全不读 `strm_use_lan`**；mp-115 写 strm 用 `choose_strm_template(options.play_base, options.play_base_lan, options.strm_use_lan)`（`av115_play.py:48-54`，`__init__.py:111-121` 读开关）。`_paths` 也不读 `refresh_emby`（`__init__.py:134`）、`dry_run`、`enabled`——av_auto 的 `_emby_refresh` 无条件执行（`:2255-2258`）。
- **影响**：同一番号若 mp-115 先写过 WAN 模板（用户关了“strm 写内网地址”），av_auto 之后会把同目录 strm 改写成**内网**模板（或反之），外网播放/Emby 播放行为随最后写入者漂移；用户在插件里关掉“完成后刷新 Emby”时 av_auto 仍刷新（且 `_paths` 的写死回退正是 `04-D-P1-02` 里作者环境的 `192.168.1.9:3000`，配置为空时会把 strm 指向作者内网）。
- **建议**：复用 `choose_strm_template` 语义（读 `strm_use_lan`）；`play_base` 两侧都空时拒绝写 strm 而不是回退作者环境；尊重 `refresh_emby`（或至少打印“插件开关关闭但 av_auto 仍刷新”的警告）。
- **可验证方式**：设置 `strm_use_lan=False`、`play_base=https://wan/...`，分别跑 `av_auto promote` 与插件扫描，比较两份 strm 内容。

#### AQ-P1-7 · 对 mp-115 私有模块的硬 import + `/config/plugins` 路径假设：插件升级/搬迁即断

- **证据**：`scripts/av_auto.py:49` `PLUGIN_DIR = Path("/config/plugins/av115scraper")`；`:603-609` `sys.path.insert(0, PLUGIN_DIR)` 后 `from av115_library import build_play_url, destination, find_existing, safe_name, write_library`、`from av115_pan import Pan115`；`:653`/`:683` 直接 import `_file_id/_is_directory/_name/_size` 下划线私有函数；`:1367`/`:2778` 又在 3 个函数里重复 `sys.path.insert` + `from av115_library import safe_name`。
- **当前契约匹配**（v1.5.8，已实测 import + `inspect.signature`）：`build_play_url(play_base, pickcode, filename)`、`destination(strm_root, labeled, actress="")`、`find_existing(strm_root, labeled)`、`safe_name(name)`、`write_library(dest, *, strm_url, strm_name, nfo_text, cover, force, crop=True)` 全部存在且签名一致；`Pan115.ensure_dir/dir_id/list_children/move/add_magnet` 全部存在。**但**：
  - `/config/plugins` 在 MoviePilot v2 里是 `PLUGIN_DATA_PATH`（宿主 `app/core/config.py:953`，`app/plugins/__init__.py:264` 用来放插件数据），插件**代码**装到 `ROOT_PATH/app/plugins/<id>`（`app/core/config.py:949`、`app/core/plugin.py:207`、`app/helper/plugin.py:43,748`，官方镜像即 `/app/app/plugins/av115scraper`）。所以 `/config/plugins/av115scraper` 只有在“手工复制一份 / 软链 / 自定义镜像”时才存在——到底是哪种**待确认**；若是手工副本，插件升级不会同步，av_auto 会一直用旧代码，或副本被删后所有采集命令 `ImportError`。
  - `write_library` 只是被 import、从未调用（`_write_library` 在 `cmd_strm`/`_promote_one` 里被忽略）——插件哪怕删掉它也会让 `_import_plugin` 崩掉，进而 `_pan()` 崩，`setup/status/submit/wait/clean/strm/promote/process/frames` 全线不可用。
  - 任何一次重命名/搬模块/改签名/删私有函数（`av115_pan._size` 等）都会在运行时炸；**P0-1 已经是同一机制下的语义级破坏**（`dir_id` 从“现场查”改成“缓存”）。
- **影响**：两仓没有版本约束、没有契约自检；生产靠人工同步。失败形式是 traceback（不是可读的 `_die`），排查成本高。
- **建议**：在 `_import_plugin` 外包 try/except 输出明确错误；启动自检（`inspect.signature` + 配置键存在性）；给 mp-115 固定兼容版本（README/部署记录）；中期让 mp-115 暴露公开接口（或 av_auto 改用插件 `/task`、`/run` HTTP API，见 3.3），去掉私有 import；删除未使用的 `write_library` import。
- **可验证方式**：把 `/config/plugins/av115scraper` 改名（或换成删掉 `find_existing` 的副本）后运行 `av_auto status`，观察是否给出可读错误；用 `inspect` 契约测试（见 AQ-P2-8）。

### 2.3 P2

#### AQ-P2-1 · 番号规则两套且不等价：`ABP-123C`/`SSIS001` 被 av_auto 拒绝，`FHD-1080` 被误收，`ABP-73` 不会补零，`FC2-123456` 不归一

- 证据：`CATALOG_CODE_RE` `:90-92` + `_norm_code` `:818-819`；mp-115 `av115_codes.py`（`_BAD_PREFIXES:9-45`、`parse_av:115-126`、`_normalize:129-137`、`detect_tag:77-86`）。实测（`/tmp/avauto-audit/t_codes.py`）：

  | 输入 | av_auto | mp-115 `parse_av` |
  | --- | --- | --- |
  | `ABP-123C`（粘连字幕标） | ❌ 不是番号 | `ABP-123` + `-C` |
  | `SSIS001`（无连字符） | ❌ | `SSIS-001` |
  | `FHD-1080` | ✅ 误收为番号 | 空（BAD_PREFIXES 排除） |
  | `ABP-73` | ✅（不补零） | `ABP-073`（zfill 3） |
  | `FC2-123456` | ✅（原样） | `FC2-PPV-123456` |
  | `010115-001` / `010115_001` / `HEYZO-1234` | ✅ | ✅（但 `_` 与 `-` 两种形态的目录名需一致） |

- 影响：`av_auto process ABP-123C` 报“不是番号”；`promote ABP-73` 建 `/AV/ABP-73` 而插件把它改成 `/AV/ABP-073`；`FHD-1080` 被当番号处理。目录/strm 名跨阶段对不上（`00-F7` 的具体矩阵）。
- 建议：抽共享 `code_utils` 或至少用同一套用例表跑两仓；`submit --code` 也应校验（当前完全不校验，可建任意 115 目录）。
- 可验证方式：`/tmp/avauto-audit/t_codes.py` + 两仓单测共享 fixture。

#### AQ-P2-2 · `cmd_wait` 默认无限等待，且 `_list_tasks` 吞掉 115 返回的错误：cookie 失效/风控时会静默挂死

- 证据：`cmd_wait` `:2398-2424`，`timeout` 默认 0（`:2400`、`:2421-2423` “0 表示一直等”）；`_list_tasks` `:2328-2348` 对返回 `{"state": false, ...}` 只走 `raw.get("tasks") or ... or []` → 返回 `[]`，不打印错误、不区分“任务不存在”和“接口失败”，于是日志一直 `WAIT ... no matching task yet` 直到人工 kill；`_task_matches` 依赖任务名/`url`/`wp_path_id` 命中（`:2316-2325`），任务名不含番号且 `wp_path_id` 字段名变化时会永远匹配不到（**待确认** task_lists 真实字段）。
- 影响：官方入口 `intake` 可能无限占着 ssh/docker 会话；错误不可见，容易误判为“115 还没下完”。
- 建议：默认超时（如 6–12h）；`_list_tasks` 打印 `state/error` 并在连续失败 N 次后返回非 0；把匹配不到任务的情况单独提示；状态落盘（`/config/scripts/av-auto-fs/wait/<code>.json`）以便重入。

#### AQ-P2-3 · Emby 刷新实现直接读 `MediaServers`、忽略 `enabled`/多实例，且多服务器时可能刷到非 Emby 实例

- 证据：`_emby_auth` `:556-578` 遍历 `MediaServers`，取“第一个 `type=="emby"` 的 host+apikey，若没有则用最后一个有 host+key 的实例”（`:572-575` 的 `break` 只在命中 emby 时生效），**不检查 `enabled`**；`MediaServerConf` 结构见宿主 `app/schemas/system.py:25-41`（含 `enabled`）。`_emby_refresh` `:2151-2188` 只按库名匹配 `/Library/VirtualFolders` 的第一台。mp-115 走 `ModuleManager().get_running_type_modules(ModuleType.MediaServer)`（`__init__.py:693-725`）——那是“启用的实例”，两者语义不同（多实例/禁用实例/私有方法问题另见 `00-L4`、`04-D-P2-05`）。
- 影响：禁用/第二台 Emby 的配置可能被 av_auto 使用；库名相同但多实例时只刷第一台；与 mp-115 的刷新路径重复（两套刷新无共享锁）。
- 建议：改走 MoviePilot EmbyModule 公开能力（`refresh_library_by_items`/`refresh_root_library`），或至少过滤 `enabled` 且遍历全部 emby 实例、逐台记录结果。

#### AQ-P2-4 · `Media/Updated` 用的是容器路径 `/media/AV/...`，参数语义未真机验证；`ReplaceAllMetadata=false` 不会让 MetaTube 重刮已存在条目

- 证据：`:2159-2163` `{"Updates":[{"Path": str(dest), "UpdateType": "Created"}]}`，`dest` 是 av_auto 眼里的 `/media/AV/<code>`（`_paths` 的 `strm_root` 来自插件配置，插件 README 称该路径就是 Emby 扫描的库路径，所以**很可能一致，但取决于 Emby 容器是否也把宿主机媒体目录挂成 `/media`**——待确认）；`:2174-2184` `Items/{libraryId}/Refresh` 参数 `Recursive=true, MetadataRefreshMode=Default, ImageRefreshMode=Default, ReplaceAllMetadata=false, Reload=true`。`Default` 语义是“只补缺失”，对已存在条目不会重新走 MetaTube；mp-115 的私有刷新只带 `Recursive=true`（宿主 `app/modules/emby/emby.py:577-597`），默认模式相同——**两条链都只能保证“新条目被扫描”，不能保证“已有条目被 MetaTube 重刮”**。
- 影响：如果番号曾经以错误元数据入库，promote 后不会自动纠正；`Media/Updated` 传错路径时该通知静默无效（仍靠库级 Refresh 兜底）。
- 建议：明确“谁写 nfo、谁触发 MetaTube、是否允许覆盖已存在条目”，写进 README；如需强制重刮，用 `ReplaceAllMetadata=true`（谨慎）或删除旧条目后再刷新；`Media/Updated` 的 path 改成 Emby 视角路径（或干脆去掉，只保留库级刷新）。

#### AQ-P2-5 · ENOSPC/pending 队列的边界缺口

- 证据与行为：
  - `_queue_fs` `:323-341` 无锁、无去重；若进程在 `candidate.mkdir()`（`:329`）与写 `kind/path`（`:336-337`）之间崩溃，会留下没有 `kind/path` 的目录，`_pending_jobs`（`:343-354`）直接忽略且永不清理；
  - `media_rmtree` `:499-508`：路径不是目录时（例如 staging strm 从未创建）只要 `_is_media_path(path)` 就 `_queue_fs("rmtree", path)`——`_promote_one:2263-2264` 正好会触发，产生大量“删不存在目录”的空 job，白占 apply_fs 的 ssh 往返；
  - `_find_strm_for_code` `:1387-1390` 只判 `p.is_file()`，不判大小；而被 ENOSPC 截断的半写 strm（`media_write_text` 排队后文件可能已存在但内容不全）可能被选中去抽帧（`_staging_strm_ready:2787-2792` 有 `size > 0` 检查，两处判据不一致）；
  - `promote` 不先 `apply_pending_queue()`（`cmd_process:2928` 才先 apply），旧 pending 的 strm 可能在 promote 写的 strm 之后才落盘，覆盖新内容（同路径时以 job 时间序最后写入为准）；
  - 宿主侧 `av_auto_apply_fs` 不在本仓库（`00-F8`），无法审计其是否与容器内 `apply_pending_queue` 并发、是否只处理 `/media`/`/mnt/user/media`。
- 建议：job 创建用临时目录 + 原子 rename；`_pending_jobs` 清理孤儿；`media_rmtree` 先判存在；`_find_strm_for_code` 增加最小长度/以 `http` 开头校验；promote 前先 `apply_pending_queue()`；补一条“两个进程同时 apply 同一 job”的测试。

#### AQ-P2-6 · 并发无锁：av_auto 与 mp-115 的 `_settle_downloads`/`run_scan`、以及两个 av_auto 进程之间可能互相破坏

- 证据：mp-115 `_watch_offline` 每 20s 跑 `_flush_offline`→`_settle_downloads`（`__init__.py:467-533`，会 `pan.search(code)` 全盘搜番号并把正片搬进 `/AV/<code>`），定时/手动 `run()` 会扫描 `cloud_paths`；av_auto 的 `submit/clean/promote` 也写 115。两边都没有跨进程锁（`av_review` 的 `busy.py` 只检测识图进程）。两个 `av_auto promote` 并发时（P0 修复后）后者的 rollback 会撤销前者的搬家；与插件扫描并发时插件可能把 av_auto 刚 promote 的夹改名，造成 AQ-P1-3 的重复目录。
- 影响：115 风控（连续 move/mkdir）、目录竞态、Emby 重复刷新；「同一番号两条入口」时插件 `relocate_code` 的 `search(code)`（`av115_pan.py:186`）可能把 av_auto 暂存文件搬进 `/AV`。
- 建议：加跨进程文件锁（如 `/config/scripts/av-auto-fs/run.lock`）并在 README 约定“同一时间只跑一条写 115 的管线”；把 av_auto/mp-115 的运行标记并入 `av_review` 的 busy 检测。

#### AQ-P2-7 · 配置健壮性：非法 `min_size_mb` 直接抛 ValueError（插件会回退），默认值静默指向作者环境

- 证据：`_paths` `:599` `int(cfg.get("min_size_mb") or MIN_SIZE_MB_DEFAULT)`——配置里写 `"300MB"` 时 `ValueError`（实测 `/tmp/avauto-audit/t_rules.py`），而 mp-115 `init_plugin:137` 用 `_as_int(..., 300)` 回退；`_plugin_cfg` `:517-527` 只在行缺失时 `_die`，配置存在但为空/缺键时用默认值静默继续（`official_115=/AV`、`play_base=192.168.1.9:3000` 作者 LAN 地址，`:588`）；`offline_dir` 在 `:598` 被读入但**从未使用**（实际目录写死 `/av-auto/_staging`）；`AV_AUTO_*`/`MP_USER_DB` 环境变量覆盖未在 README 说明。
- 建议：`_as_int` 式容错 + 缺键时明确告警；`offline_dir` 要么使用要么删除读取；README 补环境变量。

#### AQ-P2-8 · 测试覆盖：采集/落盘段核心函数 0 覆盖，60 用例无法发现 P0

- 证据（grep `/workspace/av-review-main/tests/`）：`cmd_submit`/`cmd_wait`/`_promote_one`/`promote`/`_emby_refresh`/`_emby_auth`/`_plugin_cfg`/`_list_tasks` **没有真实实现测试**；`_paths` 在测试里全部被 mock；`CleanRerunTests`（`test_av_auto_process.py:355-436`）用普通 `mock.Mock()` 当 Pan115，不模拟 `dir_id` 缓存语义，所以即使有 promote 测试也会假绿；实测基线 60/58 过，P0-1 在这套测试下不可见。
- 建议：补 6 个最小单测：① 带缓存语义的 FakePan 跑 `_promote_one`（含成功/核验失败/strm 失败回滚/重入）；② `_paths` 注释/空行/非法 `min_size_mb`；③ `_is_keeper` 矩阵（rmvb/未知大小/preview）；④ `httpx.MockTransport` 假 Emby 验证 `_emby_refresh` 的两个请求与参数；⑤ `_list_tasks` 的 `state:false` 与分页；⑥ `cmd_wait` 超时/失败任务。

#### AQ-P2-9 · 安全/数据卫生遗留项（不阻塞，但应记录）

- `Pan115` 把 115 cookie 放在 **client 级 header**（`av115_pan.py:40-47`）且 `follow_redirects=True`；httpx 对重定向会保留显式 `Cookie` 头 → 若 115 接口发生跨域跳转，`UID/SEID` 会发给跳转目标（**待确认** 115 是否跨域；这是 mp-115 代码，但 av_auto 直接复用）。
- cookie 只从 `user.db` 读、不打印（`:529-554`，实测 `bin/av_auto` 的 URL 隐藏测试通过），这点正常；但 `pending/*/data` 与 `*.strm` 里含 **pickcode**（`/redirect` 匿名可用的播放凭据），落盘默认权限（目录 755/文件 644）比 600 宽，建议 pending 与 strm 至少 640。
- `bin/av_auto` 的 ssh 用 `BatchMode=yes root@192.168.1.9`，未 pin host key（`StrictHostKeyChecking`），LAN 内 MITM 风险（P3）。
- `--url` 不会进 ssh 命令行（`bin/av_auto:211-253`），但执行期会出现在 Unraid 宿主机/容器 `ps` 的 `docker exec ... --url <URI>` argv 里（`bin/av_auto:251`）；magnet/ed2k 非密钥，可接受，但不要把它当成“完全不可见”。
- `_summarize_task`（`:2351-2356`）直接打印任务名/`hash` 前缀，未过 `_redact_media`；若磁力 `dn` 含 URL 会进 stdout（P3）。

#### AQ-P2-10 · 其他小问题（P3 表格）

| 编号 | 事项 | 证据 | 建议 |
| --- | --- | --- | --- |
| AQ-P3-1 | `_child_names` 前半段计算 `name/fid` 是死代码（随后被 import 覆盖） | `:676-692` | 删掉或改成一次 `_item_fields` |
| AQ-P3-2 | `cmd_status` 固定打印“搬家后 /AV/<番号>”，但正式库是 `cloud_paths[0]`（可能是 `/AV/有码`） | `:749` | 用 `paths["official_115"]` |
| AQ-P3-3 | `_import_plugin` 每个命令多次 `sys.path.insert`，`Pan115`/`P115Client` 的 httpx client 从不 close | `:604,1367,2778`；`:611-623` | 统一初始化一次；进程退出无碍但可加 `close` |
| AQ-P3-4 | `cmd_setup` 只禁止 staging 落在 official 下，不禁止 official 落在 staging 下 | `:699-700` | 双向检查 |
| AQ-P3-5 | `build_play_url` 遇到不含 `{pickcode}` 的模板会拼出 `/redirect/redirect?pickcode=` | `av115_library.py:31-34`（实测） | 与插件一起修（同 `04-D-P2-03` 相邻） |
| AQ-P3-6 | `_import_plugin` 的 `write_library` 未使用 | `:605` | 删 |

## 3. 两仓接口与冲突专节

### 3.1 av_auto 依赖 mp-115 的每个符号/配置键

（对应 mp-115 v1.5.8 实测全部存在且签名一致；风险等级针对“未来升级/改私有实现”。）

| av_auto 依赖 | av_auto 位置 | mp-115 对应位置 | 当前兼容 | 风险 |
| --- | --- | --- | --- | --- |
| `sys.path.insert(0, "/config/plugins/av115scraper")` | `:49,604,1367,2778` | 代码实际在 `ROOT_PATH/app/plugins/av115scraper`（`config.py:949`、`plugin.py:207`、`helper/plugin.py:43,748`）；`/config/plugins` 是 `PLUGIN_DATA_PATH`（`config.py:953`、`app/plugins/__init__.py:264`） | ⚠️ 取决于部署副本/软链（待确认） | **高** |
| `av115_library.build_play_url(play_base, pickcode, filename)` | `:605,783` | `av115_library.py:26-34` | ✅ | 中（签名/编码规则变更） |
| `av115_library.destination(strm_root, labeled, actress="")` | `:605,2206` | `av115_library.py:57-61` | ✅（av_auto 传 `actress=""`） | 中（目录规范变更） |
| `av115_library.find_existing(strm_root, labeled)` | `:605,2205` | `av115_library.py:37-54`（以 `movie.nfo` 为判据） | ✅（但 av_auto 不写 nfo，见 AQ-P1-3） | 中 |
| `av115_library.safe_name(name)` | `:605,785,1368,2779` | `av115_library.py:15-23` | ✅ | 低 |
| `av115_library.write_library(...)` | `:605`（仅 import，未调用） | `av115_library.py:145-167` | ✅ 但多余 | 中（删/改签名即 ImportError） |
| `av115_pan.Pan115(cookie)` | `:606` | `av115_pan.py:30-47` | ✅ | 高（构造函数/依赖变化） |
| `Pan115.ensure_dir(path)` | `:702,706,710,2376-2383,2438` | `:49-60` | ✅（依赖 p115client `fs_mkdir`，`av115_pan.py:74-139`） | 高 |
| `Pan115.dir_id(path)` | `:2195,2198,2201,2204,2223,2227,2229,2236,2386,2405,2435,2762-2763` | `:141-167`（**v1.5.1 起带 `_dirs` 缓存**） | ⚠️ 语义已变 → **P0-1** | **高（已发生）** |
| `Pan115.list_children(id)` | `:667,678,2212 经 _walk` | `:208-223`（aps，返回 `fn/fid/fc/fs/pc` 归一化 dict） | ✅ | 高（字段名/分页变化 → `_item_fields` 全空） |
| `Pan115.move(file_id, dir_id)` | `:2219,2227,2237,2458` | `:225-226` | ✅ | 高 |
| `Pan115.add_magnet(url, dir_id)` | `:2388` | `:169-175`（内部再建 `P115Client(app="web")`） | ✅ | 中 |
| `av115_pan._file_id/_is_directory/_name/_size`（私有） | `:653,683` | `:409-424` | ✅ | **高**（下划线函数随时可改） |
| `plugin.Av115Scraper.cookie`（+ 其它插件回退） | `:529-554` | `__init__.py:141-142`；插件自身回退见 `:583-599` | ✅ | 低 |
| `cloud_paths` | `:583` | `__init__.py:97`；`_library_path:605-610` | ⚠️ 首行解析不同（AQ-P1-1） | 中-高 |
| `strm_root` | `:584` | `__init__.py:98`、表单 `:259` | ✅ | 低 |
| `play_base` / `play_base_lan` | `:585-589` | `__init__.py:99-121`、`av115_play.py:26-54` | ✅ 键在；语义不同（AQ-P1-6） | 中 |
| `strm_use_lan` | **未读** | `__init__.py:111-121` | ❌ 忽略 | 中（AQ-P1-6） |
| `refresh_emby` | **未读** | `__init__.py:134` | ❌ 忽略 | 中（AQ-P1-6） |
| `emby_library` | `:597` | `__init__.py:135` | ✅ | 低 |
| `min_size_mb` | `:599` | `__init__.py:137`（`_as_int` 容错） | ⚠️ 非法值崩溃（AQ-P2-7） | 低-中 |
| `offline_dir` | `:598`（读入未使用） | `__init__.py:141`、`_offline_save_path:602-604` | ❌ 死配置 | 低 |
| `MediaServers`（`host/apikey/type`） | `:556-578` | 插件走 `ModuleManager`（`__init__.py:693-725`）；结构见宿主 `app/schemas/system.py:25-41`、`types.py:211` | ✅ 结构匹配；语义不同（AQ-P2-3） | 中 |
| p115client：`P115Client(..., app="web")`、`clouddownload_task_list({page,page_size,stat})`、`fs_delete({fid,ignore_warn})` | `:620-623,2332,2471,2477` | 插件：`av115_pan.py:171-175`、`__init__.py:561` | ✅ 对 **0.0.9.7.2** 核对通过（web 变体 `client.py:7522` 支持 page/page_size/stat；`/rb/delete` `client.py:10891` 接受 `fid/ignore_warn`） | 中（未声明版本，跨插件 pin） |

### 3.2 升级 mp-115 会破坏 av_auto 的具体位置（按“先炸后静默”排序）

1. **已发生**：`dir_id` 语义从“现场 getid”（`6dab82c^:dir_id`）变成“进程内 `_dirs` 缓存”（`av115_pan.py:39,152-167`）→ `av_auto.py:2223` 恒真 → promote 恒失败（AQ-P0-1）。
2. 删/改名 `av115_library.find_existing|destination|safe_name|build_play_url|write_library` 任一 → `_import_plugin`（`:603-608`）ImportError → `_pan()` 失败 → `setup/status/submit/wait/clean/strm/promote/process/frames` 全部不可用（走 `_pan` 的命令；`_emby_*` 也会被 `_paths` 牵连）。
3. 删/改名 `av115_pan._file_id|_is_directory|_name|_size` → `_item_fields`（`:652-660`）抛 ImportError → `_walk` 崩 → clean/strm/promote/`_videos_in` 全崩。
4. 改 `list_children` 返回字段（`fn→n`、`fs→s`、`fc/fid` 等）→ `_item_fields` 得空名/0 大小：keeper 判定全 False → clean 报“没有达标正片”不搬不删（可诊断）；`_walk` 的 `id` 空 → move 跳过。
5. 改 `move/add_magnet/ensure_dir` 签名或异常类型 → promote/clean/submit 抛 TypeError/未捕获异常。
6. 改配置键（`cloud_paths/strm_root/play_base_lan/emby_library/min_size_mb`）或 MoviePilot 的 `systemconfig` 键名 `plugin.Av115Scraper`（宿主 `app/plugins/__init__.py:239-247`）→ `_paths` 静默回退默认值（含作者 LAN 地址），不报错（AQ-P2-7）。
7. 改 `av115_codes` 的 `labeled`/`_normalize` 规则 → av_auto 传入的 `code` 与插件目录名不一致 → 本文 AQ-P1-3/AQ-P2-1。

### 3.3 两仓重复与冲突（逐维度）

| 维度 | mp-115 | av_auto | 冲突/重复后果 |
| --- | --- | --- | --- |
| 离线目录 | `offline_dir`（默认 `/云下载`，`__init__.py:602-604`），`_add_offline` 落这里 | 写死 `/av-auto/_staging/<code>`（`STAGING_INBOX:57`），`offline_dir` 读了不用 | 两套离线目录互不感知；同一番号两条入口都提交 = 两份下载；插件 `_settle_downloads` 的 `search(code)` 可能把 av_auto 暂存文件捞进 `/AV`（AQ-P2-6） |
| 整理 | `_settle_downloads`→`relocate_code`：`search(code)` 全盘找、正片进 `cloud_paths[0]/<code>`、垃圾搬回 `/云下载`（`__init__.py:483-533`、`av115_pan.py:177-244`） | `clean`：`_walk` inbox、keeper 搬进 `/av-auto/<code>`、其余 `fs_delete` 删除（`:2427-2482`） | 目标目录、垃圾处理（搬回 vs 删除）、扫描方式（search vs walk）、keeper 规则（AQ-P1-2）全不同 |
| 正式库 | `cloud_paths[0]/<code>`（`_library_path` 跳注释） | `cloud_paths[0]/<code>`（首行原文，AQ-P1-1）→ P0 后本应写入这里 | 目标“同一个”，但解析不同、promote 不可用 |
| strm 目录 | `/media/AV/<演员>/<labeled>/`（刮削成功且有演员时；失败则扁平 `<labeled>/`）+ `movie.nfo` + 封面（`av115_scan.py:291-310`） | `/media/AV/<code>/`（`destination(...,"")`，`:2205-2206`），只有 strm | 同番号两套目录/两份 strm → Emby 重复条目（AQ-P1-3） |
| strm URL | `choose_strm_template(play_base, play_base_lan, strm_use_lan)`（`av115_play.py:48-54`） | `play_base_lan or play_base`（`:585-589`），忽略 `strm_use_lan` | 同一文件被两方写出不同 URL（AQ-P1-6） |
| Emby 刷新 | `ModuleManager`→`_Emby__refresh_emby_library_by_id` 或 `refresh_root_library`（`__init__.py:693-725`，`Recursive=true`） | 直接 REST：`/Library/Media/Updated` + `/Items/{libraryId}/Refresh`（`:2151-2188`） | 两套刷新、无共享锁；都不强制重刮已存在条目（AQ-P2-4）；av_auto 忽略 `enabled`/多实例（AQ-P2-3） |
| 番号规则 | `av115_codes`（BAD_PREFIXES、zfill、tag、FC2 归一、无码格式） | `CATALOG_CODE_RE`（`:90-92`）+ `_norm_code`（只 upper） | 矩阵不一致（AQ-P2-1），跨阶段目录名对不上 |
| 状态/可观测 | `tasks`（50 条）、`last_run`、后台 20s 轮询（`__init__.py:460-533,612-623`） | 无状态；前台 `wait`；失败只打印 | av_auto 不可恢复/不可展示；与插件任务表不共享（AQ-P2-2） |
| 调用形态 | MoviePilot 进程内插件 + HTTP API（`/task`/`/run`/`/status`） | 容器内独立 CLI + 裸 sqlite 读配置 + 私有 import | 两套入口、两套配置读取、两套错误处理 |

**结论**：当前不是“一个负责采集、一个负责审核”的清晰分工，而是“两套完整采集 + 两套整理 + 两套 strm + 两套 Emby 刷新”。只要 mp-115 的定时扫描还开着，av_auto promote 过的番号必然被插件再整理一次，重复条目是结构性的；若希望短平快收敛，最小改动是：**让 av_auto 不再自己写正式库/正式 strm（只做到 `/av-auto` + 审核），入库统一交给 mp-115 的 `/task`/`/run`；或反过来停掉 mp-115 的扫描与整理，把它的 `/redirect` 只当播放服务保留。**

### 3.4 谁能触发 MetaTube 重刮

- MetaTube 是 Emby 的元数据 Provider，只有 **Emby 扫描/刷新条目** 时才会被调用；av_auto 与 mp-115 都不直接调用它。
- 两条链都发“库级刷新”（mp-115 `Recursive=true`；av_auto `Recursive=true + MetadataRefreshMode/ImageRefreshMode=Default + ReplaceAllMetadata=false + Reload=true`）。**新文件**会被 Emby 加入并走 Provider（MetaTube 有机会刮）；**已存在条目**在 `Default/false` 下不会被完全重刮。
- av_auto 不写 nfo/封面：它把元数据责任完全交给 MetaTube，只有在 MetaTube Provider 启用、能按 `<code>.strm`/目录名识别番号时才成立；mp-115 写 nfo（`uniqueid javbus/fc2`），Emby 的 NFO 读取通常优先于 Provider，可能反而让 MetaTube 只补图——这正是用户目标里最容易“看起来刷新了但没刮到”的地方（`00-F4`、`04-D-P1-02` 相邻）。**结论：两仓一共能触发的是“Emby 库刷新”，不是“MetaTube 重刮”；重刮条件需真机验证。**

## 4. 已确认正常（逐项核对）

1. **当前契约匹配**：对 v1.5.8 实测 `import av115_library/av115_pan` 成功；`build_play_url/destination/find_existing/safe_name/write_library` 签名与 av_auto 调用完全一致；`Pan115.ensure_dir/dir_id/list_children/move/add_magnet` 存在（`inspect` 见附录）。
2. **配置来源正确**：`plugin.Av115Scraper` 是 MoviePilot `_PluginBase.update_config` 写入的键（宿主 `app/plugins/__init__.py:239-247`）；`cloud_paths/strm_root/play_base(_lan)/emby_library/min_size_mb/offline_dir/cookie` 在 v1.5.8 `init_plugin`（`:84-147`）里都有定义。
3. **p115client 0.0.9.7.2 API 用法正确**：`P115Client(cookie, app="web")` 的 `clouddownload_task_list`（web 变体，`client.py:7522-7555`）支持 `page/page_size/stat`（`stat<=0`=全部，与 `_list_tasks` 的 `stat:0` 一致）；`fs_delete` web 变体走 `/rb/delete`（`client.py:10891-10926`）接受 `fid/ignore_warn`（与 `:2471/:2477` 一致）；`clouddownload_task_add_url` 接受 `url/wp_path_id`（与 `av115_pan.py:169-175` 一致）。**但版本未声明、未 pin（跨插件依赖）**。
4. **Emby 配置结构匹配**：`MediaServers` 的元素结构（`type/config.host/config.apikey`）与宿主 `MediaServerConf`（`app/schemas/system.py:25-41`）一致；`_emby_auth` 能读对键（多实例/`enabled` 问题见 AQ-P2-3）。
5. **URL 不外泄（入口脚本）**：`--url` 从 ssh argv 剥离、写本地/远端 600 文件、远程 `--url "$URI"`、退出清理；`Ed2kUrlForwardTests`（`tests/test_av_auto_recover.py:559-721`）实测覆盖 ed2k `|`、`--url=`、双 `--url`、docker 失败仍 apply、stdout/ssh log/临时文件无 URI。
6. **ENOSPC 分类与 pending 格式**：`classify_enospc`（`:227-309`）区分 full/reservation/stale_floor/not_full/unknown；`_queue_fs` 的 kind/path/data 格式与 apply 行为有测试（`tests/test_av_auto_recover.py:128-404`）；`bin/av_auto` 在 python 非 0 后仍调用 `av_auto_apply_fs`（`:308-319`，测试 `test_apply_fs_still_runs_when_process_exits_nonzero`）。
7. **promote 的防覆盖意图正确**：搬家前检查 `dst_path` 已存在就拒绝（`:2198-2199`），`--dry-run` 只打印不写（`:2208-2213`）；`clean` 在 inbox 无达标正片时“不搬不删，便于排查”（`:2444-2449`），空 inbox 重跑成功（`CleanRerunTests`）。
8. **抽帧/审核段的入口衔接**：`process` 只在 `clean→strm→frames→review→grok` 内工作，从不交磁/刮削/promote（`:2882-2949`）；`promote` 不在 process 内被自动调用，符合“显式 promote”声明。
9. **测试基线**：60 用例 / 58 过 / 2 个缺 ffmpeg 的环境错误（本报告实测）。

## 5. 待确认（需真机/生产数据）

1. `/config/plugins/av115scraper` 在生产里是**手工副本、软链，还是根本没有**（若没有，所有 `_pan()` 命令一开始就 ImportError）；插件升级后这份副本是否同步。
2. 生产 `p115client` 版本与 `clouddownload_task_list` 真回包字段（`status`/`status_text`/`percentDone`/`wp_path_id`/`file_id`/`tasks` 嵌套）；`_task_status_bucket`（`:2284-2313`）对真回包的判定。
3. Emby 容器看到的路径是否就是 `/media/AV/...`（即 `/Library/Media/Updated` 的 Path 是否有效）；Emby 多实例/`enabled` 状态。
4. Emby 的 NFO 与 MetaTube 元数据优先级、`Items/{id}/Refresh` 的 `MetadataRefreshMode/ReplaceAllMetadata/Reload` 在真实 Emby 版本上是否会触发 MetaTube 重刮（对应 `00-F4`）。
5. 115 `move` 后目录 id/列表的可见延迟 vs `_promote_one` 的 `time.sleep(0.8)`（`:2222`）——过短会误判失败并触发无谓 rollback（P0 修复后仍需关注）。
6. 115 接口是否会发生跨域 302（决定 `Pan115` client 级 Cookie + `follow_redirects=True` 是否泄漏 cookie）。
7. 仓外“批次调度器”是否真的存在、它是否调用 `av_auto promote`、是否在 promote 前检查 `review.json`（决定 AQ-P1-5 是缺口还是已闭环，见 `00-F5/F8`）。
8. 宿主侧 `av_auto_apply_fs` 的实现（是否与容器内 `apply_pending_queue` 并发、是否只处理 `/media`/`/mnt/user/media`）——不在本仓，`00-F8`。

## 6. 附录：复现方法与命令

- 测试基线：`cd /workspace/av-review-main && python3 -m unittest discover -s tests -v`（60/58 过/2 缺 ffmpeg）。
- `/tmp/avauto-audit/t_promote.py`：按 `av115_pan.Pan115.dir_id/_child_dir` 的缓存语义实现伪 Pan115，桩掉 `_paths/_item_fields/_emby_refresh`，跑 `_promote_one("ABP-073")` → 复现 P0（输出见 AQ-P0-1）。
- `/tmp/avauto-audit/t_paths.py`：构造 `cloud_paths` 注释/空行/正常三种配置，打印 `_paths()` 结果 → 复现 AQ-P1-1。
- `/tmp/avauto-audit/t_rules.py`：`_is_keeper` vs mp-115 `_is_video` 矩阵 + 非法 `min_size_mb` → 复现 AQ-P1-2、AQ-P2-7。
- `/tmp/avauto-audit/t_codes.py`：`CATALOG_CODE_RE` vs `av115_codes.parse_av` 矩阵 → 复现 AQ-P2-1。
- p115client 0.0.9.7.2 离线核对：从 PyPI 取 wheel（`p115client-0.0.9.7.2-py3-none-any.whl`）解压后查 `p115client/client.py:7522-7555`（`clouddownload_task_list`）、`:10891-10926`（`fs_delete` web）、`:7170-7200`（`clouddownload_task_add_url`）。
- 契约核对：`sys.path.insert(0,"/workspace/mp-av/plugins.v2/av115scraper")` 后 `inspect.signature` 打印 5 个 `av115_library` 函数 + `Pan115` 方法/私有函数存在性（全绿）。
- 未执行：任何 `bin/` 下的 ssh/docker 命令、任何对 115/Emby/Unraid 的连通性测试；被审仓库只读，未修改。
