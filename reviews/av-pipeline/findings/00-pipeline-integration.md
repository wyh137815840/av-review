# 00 · 两仓 AV 链路整体分析（Lead）

分析对象：
- **仓库 A** `/workspace/mp-av` = `wyh137815840/mp-115` 的 MoviePilot V2 插件「115 AV刮削」v1.5.8（`main@3394c3f`）。
- **仓库 B** `/workspace/av-review-main` = `wyh137815840/av-review` `main@8337582`（`av_review` 审核 CLI + `scripts/av_auto.py` 采集/审核通路 + `scripts/fmp4_http.py` 抽帧实现）。
- 宿主：MoviePilot v2.15.6（源码 `/workspace/moviepilot`）、Emby（Unraid `192.168.1.9:8096`）、findav MariaDB、Magpie LLM 网关；运行位置见各仓 README。
- 方法：逐行静态审计 + 单测基线（mp-115：51/50 过/1 skip；av-review：60/58 过/2 个缺 ffmpeg 环境错误）+ 4 名审计员分册并行 + Lead 交叉复核。分册：`01`（av_review）、`02`（av_auto 采集段）、`03`（av_auto 审核段）、`04`（fmp4/测试/部署）。

> **一句话结论：两条链路各自的「采集 → 整理 → strm → Emby」和「审核」都能跑，但它们不是一条闭环流水线，而是「仓库 A 的插件管线」+「仓库 B 里再长出来的另一条采集管线 + 两套审核」；最大的结构性缺口是 ①两条管线会写同一个正式库和同一个 Emby 库、strm 目录约定还不一致；②审核结果没有下游动作（promote/交磁/标记/删除全靠人工或仓外调度器）；③`av_auto` 直接 import `mp-115` 私有模块并读它的插件配置，`mp-115` 一升级就可能断。**

## 0. 三个 P0（Lead 复核后）

> 下面三条由分册发现，Lead 已分别复核代码/复现：**它们互相独立，且都只在前置条件成立时触发**，但任意一条都足以让「用户描述的整条链路」在对应环节停摆。

| 编号 | 结论 | 触发条件 | 证据/复核 | 分册 |
| --- | --- | --- | --- | --- |
| P0-A | `av_auto promote` **恒失败并反向撤销搬家**：刚搬进 `/AV` 的整夹被立刻搬回 `/av-auto`，正式 strm 与 Emby 刷新永远不执行 | 生产 `/config/plugins/av115scraper` 是 mp-115 **≥v1.5.1**（`Pan115._dirs` 缓存生效）。按被审 v1.5.8 代码**必然触发**；只有生产插件是 ≤v1.5.0 旧副本时才不触发（待确认） | `scripts/av_auto.py:2195` 缓存 `dir_id(src)`，`:2223` 用 `_id_ok(pan.dir_id(src_path))` 再读同一缓存恒真 → `:2225-2229` 回滚；Lead 复核 `mp-115 av115_pan.py:141-167` 的 `_dirs` 缓存确实不随 move 失效 | 02 |
| P0-B | `av_review` 在 **Python < 3.12** 下 `review`/`daily`/API `/review` 全部 **SyntaxError 无法启动**（catalog/health 不受影响） | 目标机 `/usr/bin/python3` 是 3.10/3.11（Ubuntu 22.04 默认 3.10；本容器 3.11.2 实测必现）。3.12+ 的 PEP 701 允许该语法，不触发 | `av_review/vision.py:561,566` f-string 表达式内含 `\|`；Lead 实测 `python3 -m py_compile av_review/vision.py` 报 `SyntaxError: f-string expression part cannot include a backslash` | 01 / 04 |
| P0-C | `av_auto` 审核段**假成功**：双模型调用全失败时仍写 `review.json`（`agreement=agree`、`consensus=uncertain`）并返回 0，`_complete_review` 只看模型键不看 `ok`；重入永久 SKIP | Magpie 故障（5xx/429/超时/Key 失效）且本片此前没有 `review.json` | `scripts/av_auto.py:1700-1716`（失败也记 uncertain）、`:1718-1727`（判成 agree）、`:1742-1745`（无条件写盘+返回 0）、`:2720-2734`（只查键）、`:2848-2857`（重入 SKIP）；Lead 逐行复核确认 | 03 |

> 三条 P0 的共同特征：**「文件存在」不等于「步骤成功」，「命令退出 0」不等于「业务完成」**——这也是整条链路当前最系统性的质量问题。

## 1. 两个仓库的实际职责（与用户描述对照）

| 用户描述 | 实际实现位置 | 状态 |
| --- | --- | --- |
| 仓库 A：提供接口创建 115 离线 | `mp-115` 的 `/api/v1/plugin/Av115Scraper/task`（form / API，`__init__.py:163-205`）、`_enqueue`→`_flush_offline`→`Pan115.add_magnet`；另有 `av_auto submit` 用 p115client 直提（`scripts/av_auto.py:2367-2397`） | ✅ 有两条入口（不是一条） |
| 仓库 A：离线完成后转移正式目录 | `mp-115` `_settle_downloads`/`relocate_code`：`/云下载` → `/AV/<番号>`（`__init__.py:483-533`、`av115_pan.py:177-202`）；`av_auto clean`：`/av-auto/_staging/<番号>` → `/av-auto/<番号>`（`scripts/av_auto.py:2427-2484`），再由 `promote` 搬到 `/AV/<番号>`（`:2191-2266`） | ✅ 两套整理路径 |
| 仓库 A：转 strm | `mp-115` `write_library` → `<strm_root>/<演员>/<番号>/` + `movie.nfo`/封面（`av115_library.py:57-61,145-167`）；`av_auto promote` → `find_existing(...) or destination(strm_root, code, "")`，即 `<strm_root>/<番号>/`，只写 strm（`scripts/av_auto.py:2205-2215`） | ⚠️ 两套目录形状不一致 |
| 仓库 A：通知 Emby 调用 MetaTube 刮削 | `mp-115` `_refresh_emby_library` 走 MoviePilot 的 EmbyModule（定向刷新或刷新全部，`__init__.py:693-725`）；`av_auto _emby_refresh` 直接 Emby REST：`Library/Media/Updated` + `Items/{id}/Refresh`（`MetadataRefreshMode=Default`、`ReplaceAllMetadata=false`、`Reload=true`，`scripts/av_auto.py:2151-2191`）。两者都没直接调用 MetaTube；触发刮削依赖 Emby 的元数据读取优先级与插件配置 | ⚠️ 两种刷新语义，MetaTube 是否真被触发需真机确认 |
| 仓库 B：根据数据库里已有内容做 AV 审核 | `av_review review`：从 Unraid 快照 `av_items_raw.json` 取 Emby 条目，按 strm→ffprobe 抽帧 180s/900s/中后段，双模型（DeepSeek-flash + gpt-6-luna）一致/分歧，可 `--grok-review`（`av_review/vision.py:615-705`）；`av_review catalog`：findav `av_title` 与 Emby 按番号对照，输出「库外候选短名单」（`av_review/catalog.py:281-374`）；`av_auto frames/review/grok-review` 另有一套「入库前」抽帧终审 | ✅ 有三套审核/判断：库内复查、库外候选、入库前终审 |

**结论：仓库 B 不只是「审核」，它还包含了第二套采集/整理/strm/Emby 刷新（`av_auto`），并直接复用仓库 A 的代码与配置。** 用户描述里的两个「一个……另一个……」在代码里实际是「A 一个完整管线」+「B = 另一条完整管线 + 两个审核器」。

## 2. 端到端现状（文字版数据流）

```
入口1: MoviePilot 插件页/API (/task)
        └─ mp-115 任务队列 → /云下载 → 离线完成 → relocate 到 /AV/<番号>
           → mp-115 扫描（重命名、刮 JavBus/FC2）→ <strm_root>/<演员>/<番号>/{strm,nfo,poster}
           → MoviePilot EmbyModule 刷新 → Emby 扫描 → (可能) MetaTube 刮削

入口2: bin/av_auto submit --code X --url ...
        └─ p115client → /av-auto/_staging/X → wait → clean → /av-auto/X
           → staging strm (/media/av-auto/X) → frames → 双模型 review (→ grok-review)
           → [人工/仓外调度器] promote → /AV/X + /media/AV/X（只 strm）→ Emby 刷新

审核器1: av_review daily/review（Emby 已入库）→ av_review_vision_*.json/md → profile（无人消费）
审核器2: av_review catalog（findav DB vs Emby）→ av_catalog_missing_latest.md（无人消费/仅人工）
审核器3: av_auto review（staging 新片）→ review.json / grok_review.json → 仓外调度器/人工决定 promote
```

## 3. 结构性发现

### F1（P1）两条采集/整理/strm 管线并存，写同一个正式库

- 证据：mp-115 的 `_settle_downloads` 直接 `relocate_code(code, library, cloud_id, ...)` 把正片移进 `/AV/<番号>` 并触发扫描刮削（`plugins.v2/av115scraper/__init__.py:483-533`）；`av_auto promote` 直接把 `/av-auto/<番号>` 整夹 `pan.move` 到 `/AV/<番号>` 并写 strm、刷新 Emby（`scripts/av_auto.py:2191-2266`）。两边都以 `<strm_root>`（mp-115 配置的 `strm_root`）作为正式 strm 根（`scripts/av_auto.py:583-601`）。
- 影响：同一 `/AV` 库有两条写入路径、两套失败恢复；同一番号先经 A 或先经 B 会产生不同元数据/strm 结果；两条线并发时对 115 的 move/mkdir 与 Emby 刷新没有共享锁。
- 建议：先定「唯一权威管线」。要么 mp-115 只保留「离线接口 + 正式库整理 + 刮削」，av_auto 只做「审核 + 调 mp-115 接口/promote API」；要么反过来让 av_auto 成为整理入口，mp-115 退回「115 离线 HTTP 接口」。

### F2（P1）`av_auto` 对 mp-115 的私有耦合：升级即断

- 证据：`scripts/av_auto.py:49` `PLUGIN_DIR = Path("/config/plugins/av115scraper")`；`:603-609` `sys.path.insert` 后 `from av115_library import build_play_url, destination, find_existing, safe_name, write_library`、`from av115_pan import Pan115`；`:653,683` 还 import 了 `_file_id/_is_directory/_name/_size` 这些下划线私有函数；`:517-527` 直接读 MoviePilot `user.db` 的 `systemconfig` 表 `plugin.Av115Scraper`，`:581-601` 用其中的 `cloud_paths`/`strm_root`/`play_base(_lan)`/`emby_library`/`offline_dir`/`min_size_mb`。
- mp-115 侧对应位置：`av115_library.py:26/57/145`、`av115_pan.py:409-421`、`__init__.py:584-610`。我的 mp-115 评审已经指出这些模块没有版本声明、`p115client` 依赖未声明（`reviews/mp-115-v1.5.8/findings/00-lead-crosscut.md` L2）。
- 路径本身也有假设：MoviePilot v2 的插件安装目录是 `ROOT_PATH/app/plugins`，`/config/plugins` 是 `PLUGIN_DATA_PATH`（分册 02 已核对宿主源码 `app/core/config.py:949-953`、`app/helper/plugin.py`）；`/config/plugins/av115scraper` 能 import 说明生产额外挂了本地插件副本，但仓库没有声明/同步机制（待确认生产具体挂载方式）。
- 影响：mp-115 改函数签名/搬模块/改配置键（哪怕只是重命名私有函数）→ av_auto 在运行时 `ImportError`/`TypeError`/行为错误；两个仓库没有共同版本约束，生产环境靠人工同步。
- 建议：mp-115 提供稳定的公开接口（如 `av115_pan` 只导出 `Pan115` 的公开方法、`av115_library` 提供 `write_strm` 公开 API；或者 av_auto 通过 HTTP 调 mp-115 插件 API/独立小服务）；给两仓加 `requirements`/版本兼容声明；av_auto 启动时做一次「契约自检」（import 后检查函数签名/配置键，失败给明确错误）。

### F3（P1）strm 目录形状不一致：`<演员>/<番号>` vs `<番号>`

- 证据：mp-115 `destination()` 在有人演员时返回 `strm_root/actress/labeled`（`av115_library.py:57-61`），`_handle_video` 在 `existing is None` 时按 `movie.actresses[0]` 建目录（`av115_scan.py:291-295`）；`av_auto promote` 明确调用 `destination(paths["official_strm"], code, "")`，`actress` 传空 → `strm_root/<番号>`（`scripts/av_auto.py:2205-2215`）。
- 影响：先 av_auto promote 的番号没有 `movie.nfo`，mp-115 后续扫描 `find_existing()` 以 `movie.nfo` 为判据找不到（`av115_library.py:37-54`），会再建 `<演员>/<番号>/` → 同一番号出现两个目录、两份 strm，Emby 可能重复条目或命中旧 strm；反过来先 mp-115 扫描过的番号，av_auto promote 会复用 `find_existing` 结果（因为找到 nfo），行为又不同。
- 建议：统一目录约定（推荐 mp-115 的 `<演员>/<番号>` 为准，av_auto 的 promote 复用 `find_existing` 或显式指定演员目录）；或者明确规定「av_auto promote 也只写 strm 到 mp-115 的现有目录，不新建扁平目录」，并在 av_auto 里先跑一次 `av_auto strm`/`mp-115 scan` 的兼容检查。

### F4（P1）Emby 刷新有两种语义，MetaTube 触发条件未验证

- 证据：mp-115 用 `ModuleManager` 找 EmbyModule，优先私有方法 `_Emby__refresh_emby_library_by_id`（仅 `Recursive=true`），否则 `refresh_root_library()`（`plugins.v2/av115scraper/__init__.py:693-725`）；av_auto 走 Emby REST `/Library/Media/Updated` + `/Items/{id}/Refresh`，参数 `MetadataRefreshMode=Default`、`ImageRefreshMode=Default`、`ReplaceAllMetadata=false`、`Reload=true`（`scripts/av_auto.py:2151-2191`）。
- 差异：av_auto 的刷新更接近「通知 Emby 重新识别/刮削」；mp-115 的私有刷新只对库做递归刷新，且 mp-115 自己写 `movie.nfo`——**如果 Emby 配置为优先读 nfo，MetaTube 可能不会为这些条目重新抓元数据**。用户目标里的「通知 emby 调用 metatube 刮削」在 mp-115 这条链上需要真机确认。
- 建议：确定 MetaTube 在 Emby 中的元数据优先级与 NFO 插件开关；若希望 MetaTube 刮削，应由插件只写 strm、不写 nfo（或把 nfo 的 uniqueid 改成 MetaTube 能识别的格式），或者明确 nfo 就是权威、MetaTube 只负责补图；把两种刷新统一成同一套参数并写进 README。

### F5（P1）审核结果没有下游动作：审核是「决策支持」，不是闭环闸门

- 证据：`av_review` 结果只 `publish_pair` 到 `/mnt/user/appdata/findav/profile/av_review_vision_*.{json,md}`（`av_review/vision.py:702`、`av_review/util.py:216-228`）、`av_catalog_missing_latest.*`（`av_review/catalog.py:363-368`）；仓库内没有任何代码读它们做动作（grep 只有生成、没有消费；README 明确「不自动交磁/入库」，`README.md:6`）。`av_auto` 的 `review.json`/`grok_review.json`（`scripts/av_auto.py:1742-1760`）也只被 `grok-review` 读；`promote` 需要显式命令（`cmd_promote`，`:2268-2280`），`bin/av_auto.README.md:74` 明说「批次调度器不在这个仓库里」。
- 补充证据（分册 01/03 具体锚点）：`av_review` 的 `--grok-review` 实际是死功能（`vision.py:450-459` 的 item_out 不带 `image_files`，`vision.py:599-608` 的 `maybe_grok` 永远「无图可复审」）；`av_auto` 的 `promote` 完全不读 `review.json`（`scripts/av_auto.py:2191-2280`），`process` 的退出码也不体现 pass/fail/uncertain（`03 P1-2`）。
- 影响：整条链最关键的「审核结论 → 下一步动作」（pass 的 promote、fail/uncertain 的归档/删除/重新交磁/标记 DB）没有仓库内实现；如果期望「自动审核并自动处理」，链路断在这里；如果期望人工决策，则现有设计成立，但需要文档写明「结果是给人看的」。
- 建议：明确产品边界并落地其中一种：(a) 只做决策支持：README 写明谁在什么时机读 `av_catalog_missing_latest.md`/`review.json`，给出 SOP；(b) 加一个「审核结果 → 动作」的独立入口（如 `av_auto apply-review --code X` 按 review.json 决定 promote/留在 staging/归档），并把它写进 cron/调度器；同时给 `av_review` 的 pass/fail 结果预留「可被下游查询」的稳定文件名/字段。

### F6（P1）数据源是离线快照，四方可能漂移

- 证据：`av_review` 从 Unraid 拉 `av_items_raw.json`（`av_review/inventory.py:16-30`、`av_review/catalog.py:100-130`），从 findav MariaDB 只读 `av_title`（`av_review/catalog.py:37-53`），并可选补充 `/mnt/user/media/AV` 的 strm 文件名（`:132-150`）。`av_title.emby_item_id` 目前全空（`av_review/catalog.py:214` 写明），所以对照只能按番号。
- 影响：Emby 新增条目/改名/删除、findav 入库、本地 strm 更新都会让快照过期；`av_review` 的「已入库复查」和「库外候选」可能基于旧数据；`av_items_raw.json` 由仓外进程生成，仓库内无生成器/校验。
- 建议：把快照生成纳入仓库（或至少记录生成时间/来源与校验），`av_review` 增加 `--refresh-snapshot` 与新鲜度检查；给 `av_title.emby_item_id` 回填（可用 Emby 的番号匹配结果写回）以减少按番号字符串匹配的误差。

### F7（P2）四套番号解析器：规则不一致

- 位置：mp-115 `av115_codes.py`（有码/FC2/无码 + `-C`/`-UC` 语义）；`av_auto` `CATALOG_CODE_RE`/`_norm_code`（`scripts/av_auto.py:105-107`、`:818-825`）与 `bin/av_auto` 的 bash regex；`av_review` `CODE_RE`/`norm_code`/`extract_code_from_text`（`av_review/util.py:15-21,58-90`）；`catalog.py` 的 `_ssh_mariadb_tsv` 与匹配逻辑。
- 风险：例如 mp-115 会把 `SSIS-001-C` 记成带 `-C` 的正式名，而 av_review 的 `CODE_RE` 在提取时可能把 `-C` 当后缀折叠；FC2 / 无码格式在四处各有写法；「番号 → 目录名」跨阶段可能对不上。需要真机用一批样本对比四套解析结果。
- 建议：抽一个共享的 `code_utils`（或至少维护一份跨仓对照用例表），四个位置用同一套规则与测试；mp-115 发布时带一份「番号规则版本」。

### F8（P2）三处代码副本 + 无 CI：运行时可能和仓库不一致

- 证据：`README.md:3-4` 明确代码在 grokbot-vm `/home/yuanhao/av-review`，镜像副本在 Unraid `/mnt/user/appdata/findav/bin/av-review/` 与 `/home/yuanhao/agent-xfer/av-review/`；`scripts/av_auto.py` 实际运行在 moviepilot 容器 `/config/scripts/av_auto.py`（`bin/av_auto:215`）。仓库无 CI，`av_review/__init__.py` 只有 `__version__ = "1.0.0"`，`av_auto` 无版本号。
- 影响：生产实际跑的可能是旧副本；改仓库不会自动生效；没有测试门禁。审计只能覆盖仓库快照，不能保证部署一致。
- 建议：加最小 CI（`python -m unittest discover -s tests`）+ 发布/同步脚本（带 hash/版本）、给 `av_auto` 加 `--version` 与部署自检（报告代码 hash、依赖版本、MP 插件版本）。

### F9（P2）运行时状态耦合：直接读 MoviePilot DB 与密钥文件

- 证据：`av_auto` 用 `sqlite3` 只读打开 MoviePilot `/config/user.db` 的 `systemconfig` 表取 `plugin.Av115Scraper`、`MediaServers`（`scripts/av_auto.py:517-580`）；`_load_cookie` 还依次回退到 `Anime115/P115Disk/P115StrmHelper/P115StrgmSub`（`:529-554`）；密钥来自 Unraid 上的 `emby.env`/`connection.env`/Magpie `caller-keys.json`（`av_review/secrets.py`、`av_review/constants.py:31-35`）。
- 风险：MoviePilot 升级/配置键改名/DB 结构变化即断；密钥文件路径是硬编码的单机部署假设；`user.db` 是运行中 SQLAlchemy 库，只读打开通常安全但仍有 WAL/锁假设。
- 建议：在 README 写明这是「单机专属部署耦合」；给 av_auto 加配置校验与明确错误；长期可改为调 MoviePilot API 或独立配置。

### F10（P2）并发写 115/Emby 无共享锁

- 证据：mp-115 的 `_watch_offline` 每 20s 写 115（`__init__.py:467-475`）；av_auto 的 `submit/clean/promote` 也写 115；两套都没有跨进程锁。`av_review` 的 `busy.py` 只检测识图进程（`BUSY_PATTERNS`，`av_review/constants.py:90-100`），不认识 mp-115/av_auto 的 115 操作。Emby 刷新也是两套。
- 影响：同时跑可能触发 115 风控、目录 move 竞态、Emby 重复刷新；日跑 cron（av_review 19:30）与 mp-115 定时扫描/av_auto promote 可能叠在一起。
- 建议：加跨进程互斥（文件锁/状态文件），或至少在文档里约定「同一时间只跑一条写 115 的管线」；把 av_review busy 检测扩展到 av_auto/mp-115 的运行标记。

## 4. 完成度评分（对照用户描述）

| 目标 | 完成度 | 说明 |
| --- | --- | --- |
| A：接口创建 115 离线 | 90% | 插件 API + form 可用；另有 av_auto 直提。存在依赖未声明（`p115client`）与 API 文案问题（见 mp-115 评审 P1） |
| A：离线完成 → 正式目录 | 80% | 两套实现都可用；但 mp-115 侧有 P0 分页/搬错/悬挂问题（见 `reviews/mp-115-v1.5.8`），av_auto 侧有回滚/幂等风险（见 `02`） |
| A：转 strm | 75% | 能写；目录形状两套不一致（F3），nfo 归属未统一 |
| A：通知 Emby → MetaTube 刮削 | 60% | 刷新已实现（两种语义）；MetaTube 是否被触发未验证（F4） |
| B：基于数据库已有内容做 AV 审核 | 80% | `av_review` 已实现库内复查/库外候选；依赖仓外快照与外部识图流程，结果无下游动作（F5/F6） |
| B：审核后的决策/动作闭环 | 20% | 需要人工或仓外调度器；仓内只有报告与 `promote` 命令（F5） |
| 两仓协同（接口/数据/锁/版本） | 30% | 单向私有 import + 读配置；无版本约束、无共享锁、无统一番号/目录规范（F1/F2/F7/F8/F9/F10） |

## 5. 建议的实施顺序（架构层面）

1. **先定权威边界**（F1/F2）：建议 mp-115 负责「离线接口 + 正式库整理 + 刮削 + Emby 通知」，av_auto 只负责「审核 + 显式 promote + 审核结果动作」，两边用稳定的公开接口/HTTP 调用而不是私有 import；若短期内不改，至少给 av_auto 加契约自检 + 固定 mp-115 版本。
2. **统一 strm/目录规范**（F3）：统一 `<演员>/<番号>` 或 `<番号>` 一种，`promote` 走同一 `write_library` 并写/复用 nfo；明确「谁写 nfo、谁触发 MetaTube」。
3. **补审核→动作闭环**（F5）：明确人工 SOP，或加 `apply-review` 入口 + 调度器（不在本仓则写契约）；把 av_review 的结论落成可查询的稳定文件格式。
4. **验证 MetaTube 链路**（F4）：用真机确认 Emby 的 NFO/MetaTube 优先级与刷新参数效果，再决定 mp-115 是否要停止写 nfo。
5. **工程加固**（F7/F8/F9/F10）：统一番号规则、加 CI/版本/部署自检、加跨进程锁、记录快照新鲜度。
6. 两项评审里的 P0/P1（mp-115 的 aps 分页、`p115client` 依赖等）按 `reviews/mp-115-v1.5.8/README.md` 的修复顺序执行。

## 6. 待确认（需要真机/生产数据）

- Emby 的 NFO 与 MetaTube 元数据优先级；`Items/{id}/Refresh` 参数对已存在条目是否真的触发 MetaTube 重刮。
- `av_items_raw.json` 的生成者与新鲜度；findav `av_title.emby_item_id` 何时能回填。
- 115 aps 分页在 >1200 项时的真实行为（影响 F1 中 mp-115 那半条）。
- 仓外「批次调度器」的实际实现（决定 F5 是缺口还是已闭环）。
- 三处代码副本与生产运行版本是否一致（用文件 hash/版本自检确认）。

## 7. 分册

- `01-av-review-core.md`（av_review 库内审核链路）
- `02-av-auto-acquire.md`（av_auto 采集段与 mp-115 耦合）
- `03-av-auto-review.md`（av_auto 审核段与 av_review 的重复）
- `04-fmp4-tests-eng.md`（fmp4/测试/入口/部署/文档）
