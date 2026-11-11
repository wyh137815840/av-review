# 00 · 跨模块问题（Lead 复核）

审计对象：`wyh137815840/mp-115`（`Av115Scraper` v1.5.8，main `3394c3f`），宿主源码 `jxxghp/MoviePilot` v2.15.6。

本文只放「不属于单一模块、或由 Lead 亲自复现/核对」的问题。5 个模块的详细清单见 `01`–`05`。

## 环境与基线

- 容器 python3.11；仓库本身没有 `requirements.txt`，测试需要 `httpx`（本次用 apt 的 `python3-httpx` 补齐）。
- `python3 -m unittest discover -s tests`：**51 用例，50 通过，1 跳过**（`test_av115_fc2.Fc2LiveTest` 是联网用例，默认跳过）。
- 版本一致性：`package.v2.json` 1.5.8 == `plugin_version` 1.5.8 == 最新提交标题里的 v1.5.8。
- 局限：**本文（00）**没有真实 115/Emby 凭据、没有线上端到端实测；JavBus/FC2 解析结论基于静态代码 + fixture。`03` 另做了限量线上抽查（2026-11-11 18:11–18:21 UTC，18 条 URL 记录在其附录 A）。宿主为 `blob:none` 精简检出（980 个文件，无前端静态资源），与本机无关的宿主细节标「待确认」。

## Lead 交叉核实记录

- **P0 复核通过**：`02-P0-1`（aps 分页只用 `len(page) < 500` 判终止）由 Lead 用 `httpx.MockTransport` 独立复现——按 aps 文档「最多可见前 1200+n 条、越界回绕」建模：目录 2000 项时只返回 **1200 项、静默漏 800**；若服务端在越界后仍返回满页，则 offset 序列 0/500/1000/1500… **无限循环**。复核脚本与结论与 `02` 一致，故本报告保留 P0 定级。
- **更正 Lead 自己的一条误判**：初稿 `L4` 曾写「宿主没有 `_Emby__refresh_emby_library_by_id`」——错。宿主源文件里是 `def __refresh_emby_library_by_id`（`/workspace/moviepilot/app/modules/emby/emby.py:577`，类名 `Emby` 改写后即 `_Emby__refresh_emby_library_by_id`），**当前宿主可用**；`L4` 已改为「私有方法依赖 + 失败文案误报」的 P2。这条也印证了 `04` 的核对结论。
- **与 `04` 相互印证**：`04` 用 MoviePilot-Frontend v2.15.6 的 release `dist.zip` 实测存在 `dist/plugin_icon/Moviepilot_A.png`，可关闭 `05-E-P2-10` 的「icon 可能 404」待确认项；插件侧 `plugin_icon = "Moviepilot_A.png"` 在该版本能正常显示。
- **与 `05` 相互印证**：`05` 用 stub 宿主复现了「`enabled=False` 仍提交离线」「`dry_run=True` 仍搬移云盘」；本文 `L6` 的线程重入、`L10` 的目标目录固定，与 `A`/`B` 的独立复现一致。
- **仍需真机确认**（三份报告都标了「待确认」）：115 aps 在超 1200 项时的真实回包形态、`/2.0/ufile/search` 的字段风格、P115StrmHelper 的 cookie 键名、p115client 版本与 `download_url(app=)` 签名、详情页 Vuetextarea 的提交方式。

## P1

### L1 · 提交「纯番号」返回成功，但没有任何消费者

- 证据：`plugins.v2/av115scraper/av115_intake.py:52-54` 把 `SSIS-001` 识别成 `kind="code"`；`plugins.v2/av115scraper/__init__.py:443-458`（`_enqueue`）照常入队并返回 `ok: true`。但后续只有离线链路：`__init__.py:467-481`（`_flush_offline`）只处理 `is_offline_task`，`av115_intake.py:34-41` 只认 `magnet`/`ed2k`；扫描链路 `__init__.py:653-691` 只扫配置里的 115 目录，不会按任务番号去搜索。全仓库没有任何代码消费 `kind == "code"` 的任务。
- 影响：README 第「使用」节和 `/task` API 都写「可以提交番号、磁力或 ed2k」。用户或机器人提交 `SSIS-001` 会看到收下成功，然后什么都不会发生（任务永远停在 `queued`）。这是行为与文档直接冲突。
- 建议：三选一并写进 README——(a) `kind=code` 时在已配置 115 目录里按番号搜索文件/目录再走现有 rename+刮削流程；(b) 直接拒绝纯番号，返回明确错误；(c) 收下后广播/日志明确「仅记录，不会下载」。
- 验证：`POST /api/v1/plugin/Av115Scraper/task`，正文 `{"text":"SSIS-001"}`，观察任务列表与日志——不会有任何后续动作。

### L2 · `p115client` 是硬依赖，但仓库没有声明，也没有降级

- 证据：仓库没有 `requirements.txt`。运行期有 3 处 `from p115client import P115Client`：`plugins.v2/av115scraper/av115_pan.py:77`（`_web()`，建目录/getid 用）、`av115_pan.py:171`（`add_magnet` 离线提交）、`__init__.py:561`（`_download_url` 播放跳转）。失败时分别是 `RuntimeError`/`ImportError`/502。
- 佐证：单元测试通过 `sys.modules["p115client"] = SimpleNamespace(...)` 伪造（`tests/test_av115.py:154-155`、`243`），所以测试全绿也发现不了这个缺失。
- 影响：新装 MoviePilot 如果没装带 `p115client` 的插件（如 P115StrmHelper / P115StrgmSub 之类），建目录、离线提交、`/redirect` 播放会全部失败；而失败只在第一次真正运行时暴露。当前生产能用，是因为环境里恰好有别的插件装了它。
- 建议：加 `requirements.txt`（声明最低版本）并在 README「安装」里写明；或把 `fs_mkdir`/`getid`/`download_url` 改成和现有 `Pan115` 一样的 httpx 直接调用，去掉这个跨插件依赖。
- 验证：干净环境 `python3 -c "import p115client"` 失败；起一个只装本插件的 MoviePilot，添加磁力/打开 strm 会报错。

### L3 · 识别不出番号的磁力/ed2k 任务会永久卡在 `offline`

- 证据：`av115_intake.py:65-67`（`_code_of`）对解析不出番号的链接返回 `code=""`；`__init__.py:487-493`（`_settle_downloads`）过滤条件包含 `and task.get("code")`，`code` 为空的任务从此不再被整理，也不会进入扫描。
- 影响：链接提交成功、115 也下完了，文件永远留在 `/云下载`，不整理、不刮削、列表里一直显示 `offline`。README 没有说明这种输入不支持。
- 建议：`add_magnet` 后回读 115 离线任务名/文件名补 `code`；或对 `code` 为空的离线任务兜底扫描 `/云下载` 新完成的文件；至少在消息里明确提示「此任务不会自动整理」。
- 验证：提交一个文件名没有番号的磁力（如 `magnet:?xt=urn:btih:...&dn=movie`），等下完后观察任务状态与 `/云下载`。

## P2

### L4 · Emby 定向刷新依赖宿主私有方法（当前版本可用，跨版本脆弱，且失败文案会误报）

- 证据：`__init__.py:715-722` 通过 `getattr(server, "_Emby__refresh_emby_library_by_id", None)` 调私有方法；该方法在宿主 v2.15.6 确实存在（源文件里写作 `def __refresh_emby_library_by_id`，位于 `/workspace/moviepilot/app/modules/emby/emby.py:577`，类内名字改写后即 `_Emby__refresh_emby_library_by_id`），所以**当前宿主功能正常**。
- 风险：这是私有方法名依赖，宿主改版会直接让分支静默失效；而 fallback 分支（`:720-722`）无论「库里没有这个媒体库」还是「库存在但私有方法没了」，都回同一句「没有找到名为 X 的媒体库，已通知刷新全部媒体库」——后者会误导排查。另外 `get_instances()` 是遍历全部 Emby 实例，缺失时会逐个 `refresh_root_library()`。
- 建议：改用公开 API（`refresh_library_by_items` 传目标库 item，或直接走 Emby REST `/emby/Items/{id}/Refresh`）；把「找不到库」与「找不到刷新方法」分成两种文案。

### L5 · 定时任务没有时区，依赖进程本地时区

- 证据：`__init__.py:207-223` 用 `CronTrigger.from_crontab(self._cron)`，未传 `timezone`；宿主调度器是 `BackgroundScheduler(timezone=settings.TZ, ...)`（`/workspace/moviepilot/app/scheduler.py:518`）。CronTrigger 未带时区时按进程本地时区解释，容器 `TZ` 与 `settings.TZ` 不一致时执行时间会偏移。
- 建议：`CronTrigger.from_crontab(self._cron, timezone=settings.TZ)`，并用 `settings.TZ` 校验表达式；非法时打日志并回退为空。

### L6 · 每次保存配置都可能再起一个离线监视线程

- 证据：`__init__.py:148-154` 每次 `init_plugin` 都无条件 `threading.Thread(target=self._watch_offline, ...).start()`；旧线程靠 `previous.set()` 通知退出（`__init__.py:84-90`），但它只有在当前这轮 `_flush_offline`（可能包含 2.5s 间隔的 115 请求与移动）结束后才会看到取消。窗口期内两个线程会同时 `_push_offline` 同一批任务，并各自循环。另外插件 `enabled=False` 时线程也照跑。
- 建议：线程只启动一次（用实例标记/线程名判断），循环里检查 `self._enabled`；`stop_service` 里 join/置空。

### L7 · 任务表无去重、更新会「复活」已截断任务

- 证据：`__init__.py:443-458` 不去重；`__init__.py:460-465` 只加载列表；`__init__.py:612-623` 更新时找不到 id 就 `insert(0, task)`。任务表只留 50 条（`:457`、`:623`）。
- 影响：同一链接重复提交会生成多条任务并重复提交离线；超过 50 条时，正在跑的旧任务可能被保留逻辑重新插回、把别的任务挤掉，列表状态混乱。
- 建议：按规范化文本去重；更新找不到时只记日志不插入；任务完成/失败后保留策略与数量上限解耦。

### L8 · 多个 115 目录存在包含关系时，同一文件被扫描两次

- 证据：`av115_scan.py:181-193` 对每个 root 全量收集再汇总，`counts[parent]` 按出现次数累加；`av115_scan.py:358-362` 用 `counts.get(parent, 0) != 1` 判定「单文件文件夹才改名」。若用户同时配置 `/AV` 和 `/AV/有码`，`/AV/有码/SSIS-001/xxx.mp4` 会被收集两次：`counts=2` 导致文件夹不改名，还会重复刮削、重复写 strm。
- 建议：先剔除被其它 root 包含的 root，或按 `video.path` 去重后再统计。

### L9 · 目录列举失败会中止整个 root 的扫描

- 证据：`av115_scan.py:140-143`：`list_dir` 一次异常就 `return found, f"列出 ... 失败"`，把该 root 已找到的结果一起丢掉，后续目录不再遍历。
- 影响：115 风控/网络抖动时，一次失败的 500 页里就能让本轮扫描大量漏文件，且只体现在 `failed+1`。
- 建议：把错误记到 `items` 后继续（跳过该目录），或按目录隔离失败重试。

### L10 · 离线整理目标固定用 `cloud_paths` 第一行

- 证据：`__init__.py:605-610`（`_library_path`）取 `cloud_paths` 第一个非注释行作为整理根；`__init__.py:509` 所有番号都移到这里。README 示例是 `/AV/有码`、`/AV/FC2` 两个目录，但离线完成的 FC2 也会被放进第一个目录。
- 建议：新增独立「离线整理根目录」配置，或按 `kind` 选择目标目录。

### L11 · 失败的离线任务不再重试（除一个历史特例）

- 证据：`av115_intake.py:34-41`：只有 `status=="failed"` 且 `message` 含 `"_get_id"` 的任务才会被重新提交，其它失败（网络抖动、115 限流、cookie 过期）永远停在 `failed`。
- 建议：加 `attempts` 计数与退避重试（例如 3 次），失败原因分类；cookie/权限类失败不重试但要在详情页提示。

### L12 · 整理完成先置 `packed` 再起扫描，扫描失败不回滚

- 证据：`__init__.py:505-533`：先 `task["packed"]=True`、`status="ready"`，再 `threading.Thread(target=self.run, roots=ready, wait=True)`。扫描线程若抛异常/被取消，任务已不可能再被整理或重扫（`packed` 过滤掉）。
- 建议：扫描结束后把结果回写任务（成功/失败），失败时清 `packed` 允许下一轮重试。

### L13 · `ScanResult.ok` 实际上恒为 `True`，调用方不能用它判成功

- 证据：`av115_scan.py:216-217`：`if result.failed and not result.error: result.ok = True`——`failed>0` 时反而把 `ok` 设为 `True`；`ok` 初始就是 `True`。只有「没有配置/取消」时才是 `False`。
- 影响：`/status`、`last_run`、`run()` 返回值里的 `ok` 无法表达「本轮有失败」，前端/日志只能看 `failed` 计数。
- 建议：明确语义：`ok = (not error and failed == 0)`，或删掉该字段只保留计数。

## 已确认正常（Lead 亲自核对）

- **MoviePilot V2 契约**：`save_data/get_data/update_config` 均在 `_PluginBase`（`app/plugins/__init__.py:239/269/294`）；`get_api` 支持 `auth=apikey|bear` 与 `allow_anonymous`（`app/api/endpoints/plugin.py:157-170`）；`get_service` 的 `trigger/func/kwargs` 直接交给调度器 `add_job`（`app/scheduler.py:1359-1374`）；`get_form/get_page` 签名与返回结构一致。
- **`/redirect` 的 17 位 pickcode 校验**与 115 pickcode 实际形态一致（17 位字母数字，见 faststrm 路由文档的同类校验）；测试里的 18 位 `abc123pickcodeXYZ` 只是假数据。
- **dry_run 默认开**，且 `run_scan` 在改名前检查、`_execute` 在 dry_run 时不刷新 Emby（`__init__.py:687-690`）。
- **版本/索引**：`package.v2.json` 与类字段版本、描述、作者一致；`system_version: ">=2.0.0,<3"` 与宿主 v2.15.6 匹配。
- **测试基线**：51 用例 50 过 1 跳过；模块可 `py_compile`。
- README 与实现的常规规则一致：<300MB 与预告/sample/trailer/preview 跳过、已有 `movie.nfo` 只更新 strm、目标已存在不覆盖、扫描根不改名、一个文件夹多部正片只改文件名（单 root 前提下）。
