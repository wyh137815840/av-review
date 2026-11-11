# 01 · 番号 / 磁力 / ed2k 入库链路与任务状态机审计

- 审计对象：`/workspace/mp-av`（wyh137815840/mp-115 的 clone），插件 `Av115Scraper` v1.5.8，HEAD `3394c3f`（`Create 115 directories with webapi fs_mkdir (v1.5.8) (#4)`），工作区干净。
- 审计范围：`plugins.v2/av115scraper/av115_codes.py`、`av115_intake.py` 全文；`__init__.py` 的 `get_api`（`/task` POST/GET、`/run`、`/status`）、`_accept_text` / `_enqueue` / `_load_tasks` / `_update_task` / `_flush_offline` / `_push_offline` / `_settle_downloads` / `_watch_offline` / `_offline_save_path` / `_library_path` / `init_plugin` / `run`；`tests/test_av115.py` 相关覆盖；宿主契约对照 `/workspace/moviepilot`。
- 方法：
  1. 只读静态审计上述文件（含 `av115_scan.py` 中 `choose_code` 调用点、`av115_pan.py` 中 `add_magnet` / `relocate_code` 被调语义）。
  2. 跑基线单测：`cd /workspace/mp-av && python3 -m unittest discover -s tests -v` → **Ran 51 tests, OK (skipped=1)**（skip 为 `AV115_FC2_LIVE` 未开）。
  3. 复制插件到 `/tmp/audit-a/av115scraper`（不改被审仓库），用 `av115_codes` / `av115_intake` 直接跑 60+ 组输入，核对 README 规则。
  4. 用 `/tmp/audit-a/harness.py`（stub 掉 `app.*` / `fastapi` / `Pan115`，内存版 `save_data`/`get_data`）实例化 `Av115Scraper`，复现 8 个状态机/线程场景；`/tmp/audit-a/race.py` 压测 watcher 线程泄漏；`/tmp/audit-a/disabled.py` 复现禁用态仍提交离线。未联网、未请求真实 115/JavBus。
  5. 对照宿主源码：`app/api/endpoints/plugin.py`（插件 API 注册/鉴权/配置保存）、`app/core/plugin.py`（插件加载/热重载/运行态插件集）、`app/core/security.py`（`verify_token`/`verify_apikey`）、`app/db/plugindata_oper.py` + `app/db/models/plugindata.py` + `app/db/__init__.py`（`save_data`/`get_data`/JSON 列/线程会话）。

## 总体结论

**没有发现 P0**：没有会破坏已入库云盘/本地库数据、或造成鉴权/安全问题、或让整个插件不可用的缺陷（P1-2 的「丢任务」是提交队列层面的静默丢弃，尚未触及已入库数据；若 Lead 认为「提交内容静默丢失」本身就是 blocker，可升级，见 P0 节说明）。但这条「提交 → 离线 → 搬移 → 扫描」链路的**任务状态机是不闭合的**：`kind=code` 的任务完全没有人消费；任务只增不减、按 50 条硬截断且静默丢弃；无番号与重复提交的离线任务会永久停在 `offline`；禁用插件后后台仍会提交离线并搬移云盘文件。磁力校验过松、番号识别与 README 大体一致但存在 glued `-UC` 与 `CHS/CHT` 漏标、以及 `part-01` / `sample-01` 一类误判。测试只覆盖了入口纯函数，对 `__init__.py` 的状态机、线程、API 入口**零覆盖**。

结论速览：

| 审计项 | 结论 |
| --- | --- |
| 有码 `SSIS-001` / `ssis001` / `ABP-123C` | ✅ 与 README 一致 |
| 字幕 `-C` / 破解 `-UC` | ⚠️ 基本形式一致；glued `-UC`、`-CHS/-CHT` 漏标 |
| FC2 `FC2-PPV-1234567` | ✅ 一致（另有 FC2PPV / fc2 ppv 变体） |
| 无码 `HEYZO-1234` / `010115-001` / `010115_001` | ✅ 一致（但仓库没有任何用例覆盖） |
| 文件名无番号时用文件夹名 | ✅ 由 `choose_code` + `av115_scan.py:223` 实现 |
| ed2k 校验/规范化 | ✅ 大小写、额外 `h=` 字段、`%40` 解码、31 位哈希拒收均正确 |
| 磁力校验 | ❌ 仅 `magnet:\?` 子串；不含 infohash 校验；URI 嵌在文字中时整行原样提交 |
| `kind=code` 任务能否被执行 | ❌ 无任何消费者，永远 `queued` |
| 离线任务终态 | ⚠️ 有番号可到 `ready`；无番号 / 重复提交永久 `offline`；异常不落 `failed` 且无限重试 |
| 任务列表只留 50 条 | ❌ 单次提交 >50 条会静默丢弃最早的任务（返回 `accepted` 仍含全部） |
| `_watch_offline` 热重载泄漏 | ✅ 未泄漏（旧线程被取消事件正确终止，实测 5 连发仅 1 个、stop 后 0 个）；⚠️ 禁用态仍会起线程并工作 |
| `_intake_lock` 并发 | ⚠️ 读-改-写本身在锁内；但懒初始化存在理论竞态窗口 |
| dry_run 语义 | ❌ 预览模式下离线提交、云端搬移、整理后扫描全部照跑 |
| 宿主 API/auth/save_data 契约 | ✅ 注册、`bear` 兼容 API Key、JSON 列、线程级 session 均无问题 |

---

## P0

无。

> 说明：P1-1（提交番号后没有任何执行路径）和 P1-2（>50 条静默丢任务）是本次最接近 P0 的两条。若评审口径把「README 头号能力『提交番号』100% 不生效」或「用户提交内容静默丢失」视为 blocker，可由 Lead 将 P1-1 / P1-2 升级为 P0；本报告按「不损坏已有数据、不阻断其它输入类型」定为 P1。

---

## P1

### P1-1 提交番号（`kind=code`）后没有任何消费路径，任务永远停在 `queued`

- **证据**：
  - `/workspace/mp-av/plugins.v2/av115scraper/__init__.py:443-458` `_enqueue` 对所有 `kind` 一视同仁写入 `status="queued"`；`/task` POST（`__init__.py:407-414`）与 `pending_text`（`__init__.py:143-147`）都走 `_accept_text`。
  - 唯一处理任务的地方是 `__init__.py:477-481`：
    ```python
    def _flush_offline(self) -> None:
        for task in list(self._load_tasks()):
            if is_offline_task(task):      # av115_intake.py:34-41 只认 magnet/ed2k
                self._push_offline(task)
        self._settle_downloads()
    ```
    而 `_settle_downloads`（`__init__.py:487-493`）又用 `is_offline_kind(...)` 过滤。
  - 全仓库 `_load_tasks` 调用点只有 `__init__.py:440,455,478,488,616,768`，没有任何一处按 `status=="queued" and kind=="code"` 执行扫描/入库；`run()`（`__init__.py:625-691`）只按 `_cloud_paths` 或 `only` 参数扫描，不读任务列表。
  - 复现（`/tmp/audit-a/harness.py::scenario_code_task_never_processed`）：提交 `SSIS-001` 后连续 3 轮 `_flush_offline()`，任务 `status=queued`、`message=None`，`Pan115` 零调用。
- **影响**：README:40 与详情页文案（`__init__.py:314-319`）都把「提交番号」和「提交磁力/ed2k」并列，`/task` 对纯番号也返回 `{"ok": true, "accepted":[...]}`，但番号永远不会被扫描、改名、刮削。用户看到「已收下的任务：queued SSIS-001」后一直等不到结果，且没有任何错误提示。
- **建议修复**：二选一，并同步 README/页面文案：
  1. 在 `_flush_offline`（或新增 `_process_code_tasks`）里对 `kind=="code"` 的 queued 任务执行入库：把 `code` 加入下一次 `run(roots=...)` 的目标，或按番号在 `_cloud_paths` 中检索后只扫该目录；成功后把任务置为 `ready`，失败置 `failed`。
  2. 如果产品上番号只是「待办清单、需手动跑扫描」，则 `/task` 只接受 magnet/ed2k 或对 code 返回 `ok=false` + 明确提示，且 `queued` 不应当作成功状态展示。
- **可验证方式**：`/tmp/audit-a/harness.py` 场景 S2；或单测：构造 `kind=code/status=queued` 任务，调用 `_flush_offline()` 后断言状态从 `queued` 转移（当前实现会失败）。

### P1-2 任务列表硬截断 50 条，单次提交 >50 条会静默丢弃最早的任务

- **证据**：
  - `__init__.py:454-457`：
    ```python
    with self._intake_lock:
        tasks = self._load_tasks()
        tasks.insert(0, task)
        self.save_data("tasks", tasks[:50])
    ```
  - `_accept_text`（`__init__.py:416-437`）逐行 `_enqueue` 并逐条 append 到 `accepted`，最终返回 `ok=True`、`accepted` 含全部 60 条；被丢弃的 10 条既不在存储里、也不在 `rejected` 里。
  - 复现（S1）：`_accept_text` 提交 `SSIS-001 … SSIS-060` → `accepted=60`、`stored=50`，`lost=[SSIS-001…SSIS-010]`（保留的是最新的 50 条）。
  - 宿主侧无其它持久化：任务只存在 `save_data("tasks", ...)`（`app/db/plugindata_oper.py:15-27`，JSON 列整表覆盖）。
- **影响**：批量粘贴/机器人转发 >50 条时，最早提交的任务被静默丢弃且永远不会被提交到 115；调用方从返回体看不出丢失。用户侧无任何日志（`__init__.py:429` 只记 `len(accepted)`）。对离线链路意味着「以为都在下，实际少了一截」。
- **建议修复**：
  - 把「执行队列」与「展示列表」分开：任务全量持久化（或至少保留未终态任务，不参与截断），只对 `GET /task` / `_page_text` 做 50/20 条展示裁剪；
  - `_enqueue` 改为批量插入（一次 load + 一次 save），并在 `_accept_text` 返回值里加 `truncated`/`dropped` 字段、写 warning 日志，让调用方知道丢了多少；
  - 若坚持硬上限，超出时应把任务判 `rejected` 并返回错误，而不是假装成功。
- **可验证方式**：S1；或单测：`_accept_text("\n".join(f"SSIS-{i:03d}" for i in range(60)))` 后断言 `len(_load_tasks())==60`（当前为 50）。

### P1-3 无番号的磁力/ed2k 被接受后永远停在 `offline`：不搬移、不刮削、也无失败提示

- **证据**：
  - `av115_intake.py:48-51`：磁力/ed2k 只要匹配就 `_accepted(...)`，`code` 可以为空（`_code_of` 识别不到时返回 `""`）；`__init__.py:407-437` 不问 `code` 直接 `_enqueue`。
  - `__init__.py:487-493` 的 `_settle_downloads` 过滤条件含 `and task.get("code")`，因此 `code==""` 的离线任务**永远不会进入整理流程**；
  - `is_offline_task`（`av115_intake.py:34-41`）只处理 `queued`/特定 `failed`，不会处理 `offline`，所以任务也永远不会再被推送或改状态。
  - 复现（S3）：提交 `ed2k://|file|no code here.mp4|…` → 返回 `accepted kind=ed2k code=''`；`_flush_offline()` 后 `status=offline`、`message=已添加到 115 离线下载`；再跑 3 轮仍是 `offline`，`relocate` 零调用。
  - 现有测试反而固化了这个行为：`tests/test_av115.py:71-74` 断言裸磁力 `code == ""` 且被接受。
- **影响**：115 离线下载会正常开始，但下载完成后插件不把正片搬进片库、也不刮削；任务长期显示 `offline 已添加到 115 离线下载`，用户误以为在等待下载。文件留在 `/云下载`，插件永远不会处理它。
- **建议修复**：在 `classify_line` / `_accept_text` 阶段对 `kind in ("magnet","ed2k") and not code` 的任务返回 `rejected`（错误「链接里识别不到番号」），或至少在 `_push_offline` 时置 `status="failed"` 并给出明确 `message`；`_settle_downloads` 不应静默跳过，应在 `_page_text`/`GET /task` 中提示「无番号，不会自动整理」。
- **可验证方式**：S3；单测断言无 code 的离线任务不会以 `ok=true` 被接受，或至少最终状态不是 `offline` 悬挂。

### P1-4 重复提交无幂等：重复任务永远停在 `offline`，并且每 20 秒重新请求 115

- **证据**：
  - `_enqueue`（`__init__.py:443-458`）不按 `text`/`code` 去重，同一 ed2k 提交两次得到两个任务。
  - 两个任务都会 `_push_offline` 成功（115 对重复链接返回「已存在」时 `_offline_message` 视为成功，`__init__.py:901`）。
  - `_settle_downloads`（`__init__.py:505-521`）逐个任务调用 `pan.relocate_code(code, ...)`；`av115_pan.py:177-196` 的 `relocate_code` 用 `search(code)` 找文件并**跳过 `identity == code_id`（`av115_pan.py:188`，片库里的同名番号目录）**，所以第一个任务把正片搬进 `/AV/ADN-396` 之后，第二个任务只剩「片库同名目录」和空的云下载目录，只能得到 `(0,0,0)` → `moved + already <= 0` → 状态被写回 `offline`、`message="115 还在下载，正片还没出现"`。
  - 该任务带 `code`、非 `packed`、状态 `offline`，于是**每一轮 `_settle_downloads` 都会重新执行** `ensure_dir`（`av115_pan.py:49`）+ `search`（`:186`）+ `list_children`（`:191`）（见 `av115_pan.py:181-191`），每轮都有真实 115 请求。
  - 复现（S4）：同一 ed2k 提交 2 次，4 轮 settle 后任务 1 `status=ready packed=True`，任务 2 `status=offline packed=None`，`relocate` 被调用 7 次、`ensure` 8 次（含首次 flush 的两条）。
- **影响**：普通「点了两次加入任务」或机器人重发就会产生永久悬挂任务 + 对 115 的无限轮询（结合 `_GAP_SECONDS=2.5` 的节奏，存在风控风险）；页面/接口里任务永远显示「还在下载」。
- **建议修复**：
  - `_enqueue` 按规范化后的 `text`（magnet 可归一 infohash；ed2k 归一 hash+大小）去重，重复提交返回已有任务 id，不新建；
  - `_settle_downloads` 增加「已由其它任务处理」的判定：同一 `code` 已存在 `status=="ready"` 的任务时，直接把新任务置 `ready`（或 `failed` + 说明「已被任务 X 处理」），不要无限轮询；
  - 给 `_settle_downloads` 加退避/最大尝试次数（见 P2-2、P2-8）。
- **可验证方式**：S4；单测：两个同 text 任务 + fake `relocate_code` 依次返回 `(1,0,0)`、`(0,0,0)`，断言第二个任务不会永远停在 `offline`。

### P1-5 插件禁用后 `_watch_offline` 仍启动并提交 115 离线、搬移云盘文件

- **证据**：
  - `__init__.py:154` 无条件起线程：`threading.Thread(target=self._watch_offline, name="av115-offline", daemon=True).start()`；`_watch_offline`（`__init__.py:467-475`）与 `_flush_offline`/`_settle_downloads` 全程不看 `self._enabled`。
  - 宿主 `app/core/plugin.py:103-114`（`start`）对**所有已安装插件**实例化并调用 `init_plugin(...)`，只在 `get_state()` 为真时才启用事件处理器——即插件禁用状态下也会起 watcher；配置保存走 `app/api/endpoints/plugin.py:976-990`，只调 `plugin_manager.init_plugin(...)`（同实例，不 `stop`），于是每次保存都会重新起 watcher。
  - 运行态插件集与启用状态无关：`app/core/plugin.py:1326-1330` `get_running_plugin_ids` 返回 `_running_plugins` 全部 key，插件 API 也照常注册（`app/api/endpoints/plugin.py:145-180`），所以禁用后 `/task` 仍可提交并被 watcher 执行。
  - 复现（`/tmp/audit-a/disabled.py`）：先塞入一条 queued ed2k，再 `init_plugin({"enabled": False, "cookie": "...", ...})`；`get_state()` 返回 False，但 0.5s 内 `Pan115.ensure_dir`/`add_magnet` 被调用，任务被改成 `offline` 并进入 settle。
  - 对比：前台入口 `api_run`（`__init__.py:361-365`）明确检查 `if not self._enabled: return {"ok": False, "error": "插件未启用"}`——前后台启用语义不一致。
- **影响**：用户关闭插件（通常意图是「停止一切动作」）后，后台仍每 20 秒读任务、向 115 提交离线下载、把云盘文件在 `/云下载` 与 `/AV` 间移动，并在完成后起扫描线程；MoviePilot 重启后禁用插件也会自动续跑队列。属于「禁用不生效」的行为缺陷，且有云盘写操作。
- **建议修复**：`_watch_offline` 循环内先判断 `getattr(self, "_enabled", False)`（或 `self.get_state()`），未启用时只等待不执行；`_flush_offline` 入口同样加守卫；`api_add_task`/`api_list_tasks` 在禁用时按需返回明确错误或只读；`stop_service` 保持置 `_cancel` 的现状。
- **可验证方式**：`/tmp/audit-a/disabled.py`；或单测：`_enabled=False` 时调用 `_flush_offline` 断言 `Pan115` 零调用。

---

## P2

### P2-1 `dry_run=True`（只预览）时离线提交与云端搬移照跑，与 README 的预览语义冲突

- **证据**：`_flush_offline`/`_settle_downloads`（`__init__.py:477-533`）完全不读 `self._dry_run`；`_dry_run` 只在 `_execute` 里传给 `ScanOptions`（`__init__.py:669`）。复现（S6）：`_dry_run=True` 下提交 ed2k → `add_magnet` 被调用、任务转 `offline`；`_settle_downloads` 仍调用 `relocate_code` 并把任务置 `ready`，随后用 `run(roots=[...])` 起扫描线程。
- **影响**：README:46 承诺「保持只预览先跑一遍……此时不改 115，也不写本地文件」，表单默认 `dry_run=True`（`__init__.py:94`、`get_form` 默认 `True`）。首次使用者提交磁力/ed2k 时，115 里会发生离线下载+目录移动，与「只预览」预期相反（虽然整理逻辑本身是可逆的 move，不是删除）。
- **建议修复**：明确产品语义并落地：若预览模式应只读，`_flush_offline`/`_settle_downloads` 在 `_dry_run` 时只更新任务 `message`（如「预览模式：未提交 115」）而不调 `Pan115`；若提交动作本就不受 dry_run 约束，则 README 与表单文案要写清「只预览只影响扫描/改名/写文件，不影响提交离线」。
- **可验证方式**：S6；单测断言 `_dry_run=True` 时 `add_magnet`/`relocate_code` 不被调用。

### P2-2 settle 阶段异常不落 `failed`、无退避、无限重试

- **证据**：`__init__.py:523-526`：
  ```python
  except Exception as exc:
      task["message"] = str(exc)
      logger.warning("115 AV刮削整理失败：%s", exc)
  self._update_task(task)
  ```
  状态保持 `offline`/`ready` 不变，下一轮（`_watch_offline` 每 20s，`__init__.py:474`）再次进入 `_settle_downloads`。复现（S5）：`relocate_code` 抛 `RuntimeError("115 风控 405")`，连续 3 轮后任务仍 `offline`、`message=115 风控 405`，且没有失败终态。
- **影响**：永久性错误（片库目录不可写、cookie 失效、115 风控）会以 20s 周期无限重试，任务无法进入可诊断的 `failed`，也没有通知；`GET /task`/页面只能看到反复被覆盖的异常文本。
- **建议修复**：区分可重试（下载中/临时网络）与不可重试（鉴权、权限、参数）错误；不可重试置 `status="failed"`；可重试记 `attempts`/`next_try_at`，指数退避（如 1min→5min→30min，上限后 `failed`）。
- **可验证方式**：S5；单测让 fake `relocate_code` 抛异常，断言任务最终 `failed` 且重试次数有上限。

### P2-3 磁力校验只有 `magnet:\?` 子串；URI 嵌在文字里会整行原样提交给 115

- **证据**：`av115_intake.py:10` `_MAGNET = re.compile(r"magnet:\?", re.IGNORECASE)`，`av115_intake.py:48-49` 命中即 `_accepted("magnet", raw, ...)`，`text` 是整行原始输入（`__init__.py:542` 又把 `task["text"]` 原样交给 `add_magnet`）。实测：
  - `"看看这个 magnet:? 不是链接"` → `kind=magnet`、`code=""`、`text` 为整句 → 会被提交给 115 并失败（叠加上 P1-3 永远悬挂）；
  - `"下载 ed2k://|file|ADN-396.mp4|…|/ 谢谢"` → `kind=ed2k`，`text` 带前置中文与尾部「谢谢」，整行提交 115；
  - `"magnet:?xt=urn:btih:xyz"` → 接受（不做 40/32 位 infohash 校验）；
  - 对照 ed2k（`av115_intake.py:12-15`）是结构化校验，31 位哈希会被正确判 `invalid`，两者严格度不对称。
- **影响**：机器人/聊天转发（番号+链接混排、带前后缀）会把非 URL 文本提交给 115，形成失败或悬挂任务；任意含 `magnet:?` 的文本都能进入离线队列。
- **建议修复**：`classify_line` 里用正则从原始行**提取** `magnet:\?[^\s]+` / `ed2k://\|file\|[^|]+\|\d+\|[0-9a-fA-F]{32}\|[^\s]*` 作为 `text`，提取失败再判 `invalid`；磁力至少校验 `xt=urn:btih:` 的 32/40 位 hex 或 base32；`_ed2k_filename` 同样基于提取结果。
- **可验证方式**：直接调用 `classify_line` 断言：嵌文字行返回的 `text` 是纯 URI；`magnet:?` 无 infohash 返回 `invalid`（现有用例只覆盖了合法形式，见 `tests/test_av115.py:65-103`）。

### P2-4 `_intake_lock` / `_url_lock` 懒初始化竞态（待确认概率）

- **证据**：`__init__.py:444-445`、`__init__.py:613-614` 均为
  ```python
  if not hasattr(self, "_intake_lock"):
      self._intake_lock = threading.Lock()
  ```
  两个线程（如 watcher 首次 `_settle_downloads`→`_update_task` 与 API 首次 `_enqueue`）若同时通过 `hasattr`，会各自创建一把锁，随后分别 `with` 不同的锁，读-改-写不再互斥，存在丢任务更新。`_download_url` 的 `_url_cache`/`_url_lock`（`__init__.py:565-567`）同模式（后果较轻：丢缓存）。
- **影响**：首次并发调用窗口内可能丢失 `save_data("tasks")` 的更新（后写覆盖先写）；未做线程级稳定复现（窗口极窄），列为待确认。
- **建议修复**：在 `__init__`（或 `init_plugin` 里 `if not hasattr`）一次性创建两把锁，删除方法内懒初始化。
- **可验证方式**：代码审阅即可确认；如需复现可在线程 trace 中卡在 `_enqueue` 建锁行后交错执行，成本较高，可不做。

### P2-5 `_update_task` 在 id 未命中时会把任务插回头部，导致被截断的任务"复活"并再挤掉一条

- **证据**：`__init__.py:615-623`：找到 id 用新值替换，`else: tasks.insert(0, task)`，最后 `tasks[:50]`。复现（S8）：先存 40 条、再存 20 条使最早的 `SSIS-001` 被截断，随后对它调 `_update_task` → 该任务被插回头部，另一条任务被挤出列表。
- **影响**：`_settle_downloads` 在慢速 115 请求期间持有的任务快照若已被新提交挤出 50 条，处理完后会被重新插回，再次挤掉别的任务；任务集合在并发下不稳定。
- **建议修复**：`_update_task` 对未命中的 id **不插入**（只记日志），插入只由 `_enqueue` 负责；配合 P1-2 的「执行队列不截断」一起修。
- **可验证方式**：S8。

### P2-6 番号识别边界：glued `-UC` 漏标、`CHS/CHT` 漏标、非番号文本误判

- **证据**（`av115_codes.py`）：
  - 字幕有 glued 规则 `_GLUED_C`（`av115_codes.py:61`，`\dC(?:\.[A-Z0-9]+)?$`），驱动 `ABP-123C` → `ABP-123-C`；但破解只有 `_LONE_UC`（`av115_codes.py:58`，要求前后非字母数字），`ABP-123UC` 实测 → `AvCode(code='ABP-123', tag='')`，丢失 `-UC`。README:53 说「破解记为 -UC」，实现只覆盖 `ABP-123-UC`/`ABP-123 UC`。
  - `_BAD_PREFIXES`（`av115_codes.py:9-45`）含 `CHS/CHT/CHN`，但 `detect_tag`（`av115_codes.py:77-86`）不把 `-CHS/-CHT` 识别成 `-C`：`SSIS-001-CHS.mp4`、`SSIS-001-CHT.mp4` 实测 tag 为空。
  - `_STANDARD`（`av115_codes.py:57`）只排除 `_BAD_PREFIXES` 中的词，实测 `part-01.mkv` → `code=PART-001`、`sample-01.mkv` → `SAMPLE-001`、`s-cute 1234.mp4` → `CUTE-1234`；这些文本会被当作番号任务接受。
- **影响**：字幕/破解标记在常见命名下不一致，导致目标文件名/片库目录名与预期不符；非番号文本被误收为 code 任务（叠加 P1-1 后是无害悬挂，但仍污染任务列表）。
- **建议修复**：仿照 `_GLUED_C` 增加 glued `UC` 规则（`\dUC(?:\.\w+)?$` 等）；`detect_tag` 增加 `-CHS/-CHT/_CHS` → `-C`；`_BAD_PREFIXES`/黑名单补充 `PART`、`SAMPLE`、`TRAILER`、`PREVIEW`、`DISC` 等常见非片商词，或对 code 前缀做白名单（片商表）；若担心误伤，可将误判样本加入单测固化。
- **可验证方式**：`python3 -c` 直接调用 `parse_av`；建议把上述 6 组输入写进 `tests/test_av115.py` 的 `test_filename_codes_come_from_parse_av` 邻近用例。

### P2-7 测试盲点：`__init__.py` 的状态机/线程/API 入口零覆盖；README 一半的番号规则没有用例

- **证据**：`tests/test_av115.py` 只 import `av115_intake`（第 13 行）与 `av115_pan.Pan115`（EnsureDirTests），没有任何用例 import `plugins.v2/av115scraper/__init__.py` 的 `Av115Scraper`（`grep -rn "Av115Scraper\|_enqueue\|_accept_text\|_flush_offline\|_settle_downloads\|_watch_offline" tests/` 只命中 `test_av115_play.py` 里的 URL 字符串，无类/方法调用）。缺失：
  - `-UC` 标记（无任何用例）、无码三种番号 `HEYZO-1234`/`010115-001`/`010115_001`（无任何用例）、`choose_code` 的文件名→文件夹回退（无用例，仅被 `av115_scan.py:223` 间接使用）；
  - 50 条截断、重复提交/幂等、`dry_run` 与离线/搬移的交互、无 cookie 路径、115 接口失败/异常路径、`_settle_downloads` 的终态、`_update_task` 未命中；
  - `/task` POST/GET 的请求体解析（`_read_body`/`_payload_text`）与 `/task` 两条同 path 路由的注册；
  - `is_offline_task` 的 `failed + "_get_id"` 重试分支虽有测试（`tests/test_av115.py:121-126`），但当前代码已不会再产生含 `_get_id` 的失败消息（`_get_id` 全仓库仅出现在这一处判断里，`av115_intake.py:40`），该重试分支实际是死逻辑。
- **影响**：本次 P1-1～P1-5、P2-1～P2-5 全部可在无测试保护下继续回归。
- **建议修复**：新增 `tests/test_av115_state.py`，用与 `/tmp/audit-a/harness.py` 相同的 stub 方式（`app.*`/`fastapi`/`Pan115` 替身）覆盖上面列表；至少先补 P1-2/P1-3/P1-4/P1-5 和 `-UC`/无码用例。仓库当前 `python3 -m unittest discover -s tests -v` 基线 51 用例可作回归门禁。
- **可验证方式**：`grep -rn "Av115Scraper\|_flush_offline\|_settle_downloads" tests/`；运行新增用例。

### P2-8 状态模型与文档不一致：`packed`/`settled` 不是 status、`settled` 无人读、扫描结果不回写任务、`ready` 之后无重试/清理

- **证据**：
  - 任务实际 status 只有 `queued`（`__init__.py:451`）、`offline`（`:511`/`:543`）、`failed`（`:546`）、`ready`（`:518`）四种；`packed`/`settled` 是布尔字段（`:516-517`），`settled` 在仓库内**没有任何读取方**。
  - `_settle_downloads` 在把任务置 `ready` 后另起 `run(roots=ready, wait=True)` 线程（`__init__.py:527-533`），但 `run` 的结果只写 `save_data("last_run", ...)`（`:636`/`:648`），**不回写任务**；若 `run` 因锁被占用 1800s 超时（`:628`）、被 `_cancel`（配置保存会取消，`:87-90`）或刮削异常，任务已 `packed=True/status=ready`，不会重试，也不会显示「已搬移未刮削」。
  - 无任何删除/清理任务接口，过期任务只能靠 50 条截断淘汰（`GET /task` 恒返回至多 50 条，页面只展示 20 条，`__init__.py:771`）。
- **影响**：状态语义只能靠页面文案猜；`ready` 是「文件已搬进片库」而非「已刮削/已出 strm」，但页面/接口不区分；搬移成功而扫描失败时用户无感知。
- **建议修复**：在任务上区分 `files_ready` / `scraped`；`_settle_downloads` 起扫描后用回调把结果写回任务；`run(roots=...)` 失败时任务保持可重试（如 `status="ready", scrape_pending=True` 并纳入下一轮）；补 `DELETE /task/{id}` 或 `POST /task/clear`（需 auth），并让 `GET /task` 支持分页而不是硬截断。
- **可验证方式**：代码审阅 + 直接构造 `run` 返回 `{"ok": False, "error": "已有任务在运行"}` 的场景，断言任务状态可区分。

### P2-9 `_load_tasks` 吞掉异常返回 `[]`，紧随其后的 `save_data` 会覆盖整表（待确认概率）

- **证据**：`__init__.py:460-465`：
  ```python
  def _load_tasks(self) -> list:
      try:
          tasks = self.get_data("tasks") or []
      except Exception:
          return []
      return tasks if isinstance(tasks, list) else []
  ```
  `_enqueue`/`_update_task` 在锁内调用它后**直接** `save_data("tasks", ...)`。若 `get_data` 抛异常或返回非 list（数据损坏/历史格式），读到的空列表会被立刻写回，原有任务全丢。
- **影响**：瞬时 DB 错误（宿主侧 `PluginData` 用 SQLAlchemy session，正常不抛；概率低）或历史非 list 数据会导致任务列表被清空，且无日志。属于「异常被静默吞掉后执行破坏性写」的反模式。
- **建议修复**：`_load_tasks` 不吞异常，或在 `_enqueue`/`_update_task` 中区分「确实为空」与「加载失败」——加载失败时拒绝写入并返回错误/重试；非 list 时打 warning 并备份原值。
- **可验证方式**：单测让 fake `get_data` 抛异常，断言 `_enqueue` 不会把 `save_data` 写成只剩 1 条（当前会）。

### P2-10 settle 轮询固定 20s 且每轮对每个任务重复 `ensure_dir`+`search`，请求密度高

- **证据**：`_watch_offline` 用 `cancel.wait(20)`（`__init__.py:474`），每轮 `_flush_offline` → `_settle_downloads`；`_settle_downloads` 每个 pending 任务调用 `pan.ensure_dir(self._offline_save_path())`（`:499`）与 `pan.relocate_code(...)`（`:509`），后者内部 `ensure_dir(library/code)`（`av115_pan.py:181`）、`search(code)`（`:186`）、`list_children`（`:191`）；`av115_pan.py:19` `_GAP_SECONDS=2.5` 会给每次 App 调用让路。在途任务每 20s 一轮，每轮每任务约 3-4 次 115 请求。
- **影响**：下载时间长（大文件常见几小时）+ 多个任务时，实际形成持续高频轮询，与 `av115_pan.py:18` 注释「全量扫描时宁可慢，避免 115 风控」的意图相反；单轮耗时也可能超过 20s（N 任务 × 多请求 × 2.5s）。
- **建议修复**：为离线轮询单独设较长间隔（如 5~10 分钟）并对每个任务记 `next_poll_at`；`_settle_downloads` 复用同一个 `Pan115` 实例减少 `ensure_dir` 重复；在途任务只在 `relocate` 真正可能完成时才调用 `ensure_dir(library/code)`（当前每轮都会顺手建空目录）。
- **可验证方式**：静态可确认；可在 fake `Pan115` 上计数一轮 settle 的调用次数并列出实测。

### P2-11 `_accept_text` 逐行同步写库，且 `/task` 是 `async` 端点里做同步 DB 读写

- **证据**：`__init__.py:407-414` `async def api_add_task(...)` 里直接调用同步 `_accept_text` → 每行 `_enqueue` 一次 `_load_tasks`+`save_data`（`:454-457`）。60 行 = 60 次 load + 60 次 save（宿主 `save_data` 是同步 SQLAlchemy，`app/db/plugindata_oper.py:15-27`）。
- **影响**：批量提交时阻塞事件循环、放大 P1-2 的截断窗口，也更容易与 watcher 的写入交错。
- **建议修复**：`_accept_text` 批量分类后一次读、一次写；端点用 `run_in_threadpool` 包住同步 DB 操作，或改为 `async_save_data`/`async_get_data`（宿主提供，见 `app/plugins/__init__.py:280-316`）。
- **可验证方式**：静态可确认；批量提交 60 行时统计 `save_data` 调用次数（当前 60 次）。

---

## 已确认正常（做过检查、结论正确）

1. **README 有码规则**：`SSIS-001`→`SSIS-001`、`ssis001`→`SSIS-001`、`ABP-123C`→`ABP-123-C`、`ABP123C.mp4`→`ABP-123-C`、`ssis-12`→`SSIS-012`（补零），与 `tests/test_av115.py:76-88` 一致；`ABP-123-UC`/`SSIS-001 UC`/`SSIS-001 破解`/`SSIS-001 中文字幕` 实测标记正确。
2. **README FC2 规则**：`FC2-PPV-1234567`、`FC2PPV4751586`、`fc2 ppv 3127193`、`FC2-1234567` 均归一为 `FC2-PPV-<数字>`，kind=`fc2`（`av115_codes.py:52-53,96-100`；`tests/test_av115_fc2.py:100-110`）。
3. **README 无码规则**：`HEYZO-1234`、`heyzo_1234`、`010115-001`、`010115_001`、`10mu-010115_01` 实测均识别且 kind=`uncensored`（`av115_codes.py:54-56,101-106`）；注意仓库无对应用例（见 P2-7）。
4. **文件名无番号时用文件夹名**：`choose_code(file, folder)`（`av115_codes.py:148-153`）文件名优先、同番号合并字幕标记；`random.mp4` + 文件夹 `ABP-123` 实测回退为 `ABP-123`；`av115_scan.py:223` 确实以 `choose_code(video.name, name_of(parent))` 调用。实测 `SSIS-001-C.mp4` + 文件夹 `SSIS-001` → 合并出 `-C`。
5. **ed2k 校验/规范化**：大小写（`ED2K://|FILE|...`）、哈希大小写、额外 `h=` 字段均通过；31 位哈希被判 `invalid`；文件名 `%40MTALL-061…` 会 `unquote` 后识别出 `MTALL-061`（`av115_intake.py:12-15,58-62`；`tests/test_av115.py:58-63`）。
6. **磁力/ed2k 走同一离线通道**：`is_offline_kind`（`av115_intake.py:29-31,16`）与 `Pan115.add_magnet` 原样提交 URI、`app="web"`（`av115_pan.py:169-175`；`tests/test_av115.py:143-172` 覆盖）；任务排队后由 `_flush_offline` 推送。
7. **宿主 API 注册/鉴权契约**：`get_api` 里 `/task` 两条同 path 不同 method 的条目可由宿主逐条 `app.add_api_route` 注册（`app/api/endpoints/plugin.py:150-178`）；`auth: "bear"` 使用 `verify_token`，而 `verify_token` 在无 JWT 时回退到 `X-API-KEY`/`apikey`/`token`（`app/core/security.py:254-293`，`oauth2_scheme_manual_error.auto_error=False` 于 `:28-31`），因此页面文案「请求头带 API 令牌」可正常工作，`/run`、`/status` 的 `apikey` 也正常；`Request` 参数注入由 FastAPI 处理。
8. **`save_data`/`get_data` 语义**：`PluginData.value` 是 JSON 列（`app/db/models/plugindata.py:21`），整表覆盖写（`app/db/plugindata_oper.py:15-27`）；同步 DB 使用 `ScopedSession`（`app/db/__init__.py:236`，thread-local），watcher 线程与 API 线程各自持有 session，列表 round-trip 与并发读写没有宿主侧问题。
9. **`_watch_offline` 热重载不会重复泄漏**：`init_plugin` 先 `previous.set()` 再换新 `_cancel`（`__init__.py:87-90`），旧 watcher 的 `cancel.wait(20)` 会立刻返回并退出；`stop_service`（`:356-359`）置位取消。实测（S7 + `/tmp/audit-a/race.py`，含 `sys.setswitchinterval(5.0)` 压测 5 次连续 `init_plugin`）：存活 watcher 始终 1 个，`stop_service` 后 0 个。**注意**：禁用态仍会起线程（P1-5），但这与「重载泄漏」是两件事。
10. **读-改-写不丢更新（正常并发路径）**：`_update_task` 在 `_intake_lock` 内重新 `_load_tasks` 再按 id 替换（`__init__.py:612-623`），`_enqueue` 插入后立即落盘，watcher/API 并发时不会互相覆盖（P2-4 的懒建锁窗口除外）。
11. **扫描并发保护**：`run` 用非阻塞锁，第二次触发直接返回「已有任务在运行」（`__init__.py:626-630`）；`api_run` 起线程前检查 `_enabled`（`:361-365`）。
12. **`/task` 请求体解析**：`_read_body`（`__init__.py:879-892`）兼容 JSON dict、非 JSON 纯文本、空 body；`_payload_text`（`:906-923`）支持 `{"text":...}`、`{"items":[...]}`、嵌套值；`api_add_task`（`:407-414`）另外追加 `?text=` 查询参数，实现与 `get_api` 描述一致（无测试覆盖，见 P2-7）。
13. **非番号文本不会误入离线通道**：`"不是链接"`、`"movie.2024.1080p.mkv"`、`"notes about ed2k"` 均判 `invalid`/`None`；`"# ..."` 注释行跳过（`av115_intake.py:44-47`；`tests/test_av115.py:88-102`）。注：`part-01` 等见 P2-6。

---

## 待确认 / 无法核实

1. **p115client 真实返回形状**：`Pan115.add_magnet` 返回 `P115Client.clouddownload_task_add_url(...)` 的 dict；`_offline_message`（`__init__.py:895-903`）以 `state`/`errno`/`error_msg`/`error` 判定成功。若该 API 成功时返回 `None`（非 dict）会被 `add_magnet` 归一成 `{}`，进而被判「115 拒绝了这条离线链接」——离线任务会误 `failed`。当前环境无 `p115client` 且按纪律不联网，无法验证真实返回。
2. **115 是否接受 ed2k 作为 `clouddownload_task_add_url` 的 url**：README/history v1.5.7 声称支持，测试用 fake 客户端，无真实接口验证。
3. **P115StrmHelper 的 Cookie 配置键名**：`_cookie_value`（`__init__.py:584-599`）从 `PluginManager().get_plugin_config("P115StrmHelper")` 里依次找 `cookies`/`cookie`/`pan_cookie`/`pan_cookies`；该插件源码不在本仓库/宿主中，键名无法核实（`app/core/plugin.py:776-787` 在插件未加载时返回 `{}`，也会导致取不到 cookie）。
4. **115 `search(code)` 的匹配语义**：`_settle_downloads` 依赖 `Pan115.search(code)`（`av115_pan.py:204-206`）定位下载文件；若 115 搜索是子串/模糊匹配，可能把其它番号的视频搬进当前片库目录（`_relocate_file` 对所有匹配视频执行 `move`，`av115_pan.py:231-245`）。无真实 115 访问，无法确认匹配严格度。
5. **详情页「加入任务」按钮是否发送 textarea 内容**：`get_page`（`__init__.py:324-343`）的 `VBtn` 只声明 `events.click.api`/`method`，未声明请求体字段；MoviePilot 前端不在 `/workspace/moviepilot` 中，无法核实是否自动带上 `text` model。页面文案「如果列表没变化，就填到配置里…」（`:314-319`）本身像是在提示这个兜底，建议与前端实现一起确认。
6. **50 条截断是否属于产品意图**：README 未提及上限；若设计意图只是「页面最多展示 50 条」，则 P1-2 的修法应是持久化与展示分离；需产品确认后再定实现。
7. **P2-4 懒建锁竞态**：静态推导成立（`hasattr` 与赋值非原子），但未做线程级稳定复现，实际触发概率很低。

---

## 附：复现材料（均在 /tmp，不改被审仓库）

- `/tmp/audit-a/av115scraper/`：插件副本（避免 import 时在被审仓库写 `__pycache__`）。
- `/tmp/audit-a/harness.py`：stub 掉 `app.*`/`fastapi`，用 `FakePan` 记录 `ensure_dir`/`add_magnet`/`relocate_code`；场景 S1 截断、S2 code 任务、S3 无番号、S4 重复提交、S5 异常重试、S6 dry_run、S7 watcher 生命周期、S8 `_update_task` 复活。运行：`cd /tmp/audit-a && python3 harness.py`。
- `/tmp/audit-a/race.py`：`sys.setswitchinterval(5.0)` 下连续 5 次 `init_plugin`，验证 watcher 不泄漏。
- `/tmp/audit-a/disabled.py`：`enabled=False` 的 `init_plugin` 仍触发 `add_magnet`/settle，验证 P1-5。
- 单测基线：`cd /workspace/mp-av && python3 -m unittest discover -s tests -v` → `Ran 51 tests … OK (skipped=1)`。
