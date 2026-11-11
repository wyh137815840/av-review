# 05 · 测试覆盖、dry_run、线程安全、安全性与文档一致性审计

- 被审仓库：`/workspace/mp-av`（`wyh137815840/mp-115` 的 clone，main 干净，`git log -1` = `3394c3f Create 115 directories with webapi fs_mkdir (v1.5.8)`，2026-11-10）
- 插件：`plugins.v2/av115scraper/`，`plugin_version = "1.5.8"`，`package.v2.json` version `1.5.8`
- 宿主对照源码：`/workspace/moviepilot`，`version.py APP_VERSION = 'v2.15.6'`
- 审计员：auditor-quality（任务 E / `task-5`）
- 审计范围：测试面与覆盖缺口、dry_run 写操作泄漏、线程与生命周期、安全暴露面、README/package 文档一致性、版本与 icon
- 结论一句话：**未发现 P0；发现 P1 5 条、P2 12 条。** 最需要处理的是「dry_run 并不拦住 115 离线入库/文件搬移」「插件被禁用后后台监视线程仍会写 115」「主类 934 行 0 测试覆盖」「p115client 运行时依赖未声明」。

---

## 1. 方法与可复现命令

所有命令都在容器内执行，`/workspace/mp-av` 只读（测试产生的 `__pycache__` 已被 `.gitignore` 忽略，`git status --porcelain` 为空）。实验脚本放 `/tmp`。

### 1.1 测试基线（实测）

```bash
cd /workspace/mp-av && python3 -m unittest discover -s tests -v
# Ran 51 tests in 0.430s
# OK (skipped=1)
```

- 51 用例：50 通过、1 跳过（`Fc2LiveTest.test_live_sample_codes`，需 `AV115_FC2_LIVE=1`），0 失败。
- README 里的命令也能跑通：`python3 -m unittest tests.test_av115 tests.test_av115_fc2 tests.test_av115_play`（`tests/` 无 `__init__.py`，但 Python 3 命名空间包可用）。
- 分布：`test_av115.py` 17 例、`test_av115_fc2.py` 18 例、`test_av115_play.py` 16 例。
- 51 例中**没有任何一例 import `__init__.py`**（插件主类）。测试全部只覆盖 8 个辅助模块。

### 1.2 缺 httpx 的复现（原始环境问题）

```bash
# 在干净进程里把 httpx 置为 None 再跑全套（模拟未安装）
python3 /tmp/no_httpx.py
# Ran 51 tests ... FAILED (errors=10, skipped=1)
```

10 个 import 失败用例：`test_av115.IntakeTests.test_add_url_posts_the_raw_ed2k_and_magnet` + `EnsureDirTests` 全部 9 例。测试依赖未写进 README，也没有 `requirements.txt`（见 E-05）。

### 1.3 覆盖率测量（无 coverage 工具，用标准库 `trace` + AST 语句行）

```bash
cd /workspace/mp-av && python3 -m trace --count --coverdir=/tmp/tr --summary /tmp/run_suite.py
# 再用 /tmp 脚本按 AST 取函数/语句行，统计“函数是否被进入、语句行是否被执行”
# 见附录 A 数据
```

### 1.4 宿主 stub 行为验证（22 项检查全 PASS，用于确认/证伪 dry_run 与生命周期行为）

容器里没有 `fastapi`/`p115client`/`PIL`/`apscheduler`，主类不能直接 import。`/tmp/harness.py` 用最小 `app.*`、`fastapi` stub 导入 `plugins.v2/av115scraper/__init__.py`；`/tmp/scenarios.py` 用假 `Pan115`、假 `run_scan`、假 Request 驱动真实插件代码，22 项断言全部成立（详见各问题证据）。

### 1.5 其他

- `python3 -m py_compile plugins.v2/av115scraper/*.py tests/*.py`：全部通过。
- 宿主行为核对：`app/core/plugin.py:106-108`、`app/api/endpoints/plugin.py:976-988`、`app/core/security.py:254-285`、`app/core/module.py:129-138`、`app/modules/emby/emby.py:577`、`app/chain/storage.py:46/94/112`、`app/helper/plugin.py:867-915`。
- 外部核对（icon）：官方插件市场 `MoviePilot-Plugins/icons/Moviepilot_A.png` 的 raw URL 返回 `image/png`（文件存在）。

### 1.6 方法局限（影响结论口径）

- 无官方 coverage 工具；覆盖率是 `trace.py --count` + AST 语句行统计，多行字面量/跨行表达式可能造成少量“伪未覆盖”，**函数是否被进入**的判断不受影响。
- `fastapi`/`p115client`/`PIL`/`apscheduler` 未安装：Emby 刷新、真实 p115client 调用只能静态核对，标「待确认」。
- 未访问 115/Emby 真机，本报告不做线上行为判断。

---

## 2. 总体结论

| 级别 | 数量 | 摘要 |
| --- | --- | --- |
| P0 | 0 | 未发现数据损坏、越权读取用户 115 内容、凭据泄漏级别的确定问题 |
| P1 | 5 | dry_run 不拦离线入库/115 搬移；禁用插件仍后台写 115；主类 0 测试；核心 115/JavBus 通路无测试；p115client 依赖未声明 |
| P2 | 12 | 重复提交竞态、资源/线程/缓存生命周期、`/redirect` 匿名暴露面、文档偏差、icon 缺失、无 CI 等 |

**测试基线**：51 用例 / 50 过 / 1 跳过 / 0 失败（0.43s）。**覆盖基线**：8 个辅助模块函数级进入 93/130（71.5%），AST 语句行 754/1367（55.2%）；`__init__.py` 45 个函数 0/45 进入；`av115_javbus.py` 12 个函数 0/12 进入。

值得注意的是：本次 P1 的 dry_run 泄漏与“禁用后仍写 115”都发生在 `__init__.py` 的编排层与离线线程里，而这一层恰好是 0 测试覆盖——测试缺口与行为缺陷直接对应。

---

## 3. 问题清单

> 编号规则：E-P1-xx / E-P2-xx。每条给「证据（`文件:行号` + 实测行为） / 影响 / 建议修复 / 可验证方式」。

### 3.1 P0

无。未发现不可逆数据损坏、凭据泄漏、越权读取/删除用户文件、远程代码执行级别的问题。`/redirect` 的匿名暴露面存在但需要已知 pickcode，定级 P2（E-P2-06）。

### 3.2 P1

#### E-P1-01 dry_run 只“预览扫描”，不拦 115 离线入库与 115 文件搬移，与 README「此时不改 115」冲突

- **证据**
  - `plugins.v2/av115scraper/__init__.py:467-481`：`_watch_offline` 每 20s 调 `_flush_offline`，后者对 `is_offline_task` 的任务调 `_push_offline`，随后无条件 `_settle_downloads()`；两者都不看 `_dry_run`。
  - `__init__.py:483-533`：`_settle_downloads` 会 `Pan115(...)`、`ensure_dir(self._offline_save_path())`、`pan.relocate_code(...)`（内部 `move`/`ensure_dir`→`fs_mkdir`），并把任务写成 `ready/packed`；`__init__.py:552-558` 的 `_add_offline` 会真的调用 `clouddownload_task_add_url`（115 离线下载）。
  - `_dry_run` 全仓唯一使用点：`__init__.py:669`（传入 ScanOptions）、`:687`（Emby 刷新门禁）；离线链路 0 引用（`grep -n _dry_run` 可复核）。
  - `README.md:46`：“保持「只预览」先跑一遍……**此时不改 115**，也不写本地文件。”
  - 实测（`/tmp/scenarios.py`，stub 宿主 + FakePan）：
    - `_dry_run=True` 时调用 `_flush_offline()` → `FakePan.add_magnet` 被调 1 次（B1），`relocate_code` 被调 1 次（B2），任务被写为 `status=ready, packed=True`（B3）。
- **影响**：用户按 README 在预览模式提交磁力/ed2k，115 账号里会真的多出离线下载任务，下载完成后正片会被 `move` 进 `/AV/番号`（还会建目录）。用户以为“只看不改”，实际云盘已被写。虽可回退，但违背预览的安全承诺。
- **建议修复**：在 `_flush_offline`/`_push_offline`/`_settle_downloads` 入口加 `if self._dry_run: return`（或独立开关 `enable_offline_tasks`）；若产品上认为“离线入队独立于预览”，则必须同步修正 README 与 UI 文案（`_switch(4,"dry_run","只预览，不改名不写文件")` 也要说明不含离线入库）。
- **可验证方式**：fake `Pan115` 断言 `dry_run=True` 时 `add_magnet/relocate_code/ensure_dir` 调用次数为 0；`dry_run=False` 时为 1；现有 `/tmp/scenarios.py` B1-B3 反转即可。

#### E-P1-02 插件被禁用后仍启动离线监视线程，并继续向 115 提交离线/搬文件；容器启动时所有“已安装但未启用”的插件都会起该线程

- **证据**
  - `__init__.py:154`：`init_plugin` 末尾**无条件** `threading.Thread(target=self._watch_offline, name="av115-offline", daemon=True).start()`，不看 `_enabled`。
  - `__init__.py:467-481/535-558`：`_push_offline`/`_add_offline` 不检查 `_enabled`；`api_add_task`（`:407-414`）也不检查，禁用状态下仍可通过 API 收任务，随后被后台线程提交。
  - 宿主生命周期：`app/core/plugin.py:106-108` 对所有**已安装**插件调用 `init_plugin(config)`（`enabled` 只影响事件处理器）；`app/api/endpoints/plugin.py:976-988` 每次保存配置都会调 `plugin_manager.init_plugin(plugin_id, conf)`。即“关闭插件”只是配置 `enabled=False`，插件实例照常 `init_plugin`。
  - 实测：`scenarios.py` A1/A2 —— `init_plugin({"enabled": False})` 后 `av115-offline` 线程存在；C1 —— `enabled=False`（且 `dry_run=True`）保存配置后，排队中的 ed2k 被真的提交（`FakePan.add_magnet` 被调）。
- **影响**：用户关闭插件并不能停止其对 115 账号的写操作；后台线程长期驻留。与“禁用”直觉相反，属于生命周期缺陷。
- **建议修复**：`init_plugin` 中仅当 `self._enabled` 时启动 watcher（或 watcher 每轮先 `if not self._enabled: wait; continue`）；`_push_offline`/`_settle_downloads`/`api_add_task` 增加启用检查；`stop_service` 保持只置 cancel。注意 MoviePilot 的 `init_plugin` 在保存配置时会被反复调用，不能靠“没启动就没人跑”来兜底。
- **可验证方式**：`enabled=False` 时 `init_plugin` 后 `[t for t in threading.enumerate() if t.name=="av115-offline"]` 为空、`add_magnet` 0 次；`enabled=True` 时相反。

#### E-P1-03 插件主类 `__init__.py`（934 行、45 个函数）0 测试覆盖

- **证据**
  - `tests/` 三个文件只 import `av115_intake`/`av115_scan`/`av115_fc2`/`av115_javbus.Movie`/`av115_play`/`av115_library`/`av115_pan`，从不 import 插件包入口（`grep -n "av115scraper" tests/*.py` 只有 `test_av115.py:175` 读源码文本做字符串断言）。
  - `trace` 结果：`__init__.py` **没有 cover 文件**（模块从未被导入），45/45 个函数（含 `init_plugin/get_api/api_redirect/api_add_task/_watch_offline/_settle_downloads/run/_execute/_refresh_emby_library/_download_url/_payload_text` 等）全部未进入。
  - 未覆盖的正是 P1-01、P1-02、E-P2-06 所在层：dry_run 门禁、启用门禁、认证配置、任务队列、生命周期、Emby 刷新。
- **影响**：插件最关键的“编排/权限/生命周期”没有回归保护；本次两个 P1 缺陷之所以长期存在，与这层零测试直接相关。任何 `init_plugin` 配置迁移、API 签名调整、线程改动都只能靠人工验证。
- **建议修复**：新增 `tests/test_av115_plugin.py`，用最小 `app.*`/`fastapi` stub 导入主类（可直接复用 `/tmp/harness.py` 的思路，约 120 行），先覆盖本文第 5 节清单第 1 组用例。
- **可验证方式**：`python3 -m trace --count ...` 后 `__init__.cover` 出现且关键函数语句行被执行；CI 里跑覆盖率门槛。

#### E-P1-04 核心 115 数据通路与 JavBus 解析 0 测试（只测了假的替身）

- **证据**（trace 函数进入数 + 早退分支）
  - `av115_javbus.py`：`_clean/_field/parse_detail/detail_urls/JavBusClient.__init__/download_cover/scrape/default_fetch/download_bytes/_urllib_fetch/_urllib_bytes/_opener` **0/12 进入，0/146 语句行**。测试只用到 `from av115_javbus import Movie`（dataclass）；所有 JavBus 抓取都用测试里的 `_Client` 假替身（`test_av115_fc2.py:209-224`）。
  - `av115_pan.py`：37 个函数只进入 18 个。完全未进入：`relocate_code`、`search`、`list_children`、`move`、`rename`、`_relocate_file`、`_get/_post/_call`、`CookieStorage.*`、`_pace`、`_checked`、`_items`、`_from_aps`、`_size`、`_keeps_video`（`av115_pan.py:177-269, 272-316, 319-346, 349-366, 417-435`）。`test_av115.py` 只覆盖 `ensure_dir`/`_web_getid`/`_web_mkdir`/`_call_fs_mkdir`/`add_magnet`。
  - `av115_scan.py`：`run_scan` 只执行到“缺播放模板”早退（`test_av115_play.py:174-177`），`_collect`/`_is_video`/`_sibling_names`/`join`/`ScanResult.as_dict` 0 进入；`_rename_file`/`_rename_folder` 只进入 `name == new_name`/`rename_folder=False` 的早退分支（2/22、4/28 语句行），真实的 rename、重名冲突、dry_run 文案、取消分支全未测。
- **影响**：aps 列目录分页/字段兼容（`_from_aps` 文件用 `fid`、目录用 `cid`）、`/2.0/ufile/search`、`move`/`batch_rename`、`_checked` 的 state/errno 判定、`_call` 的 405/429 重试、JavBus 页面解析与 404 降级——这些是“刮削/改名/播放”主流程的实际网络面，却没有任何测试。任何上游接口变更都无保护。
- **建议修复**：见第 5 节清单第 2-5 组（fake fetch、fake storage 即可，不需要真网）。
- **可验证方式**：trace 后上述函数进入数 > 0；分支用 fake 逐条断言。

#### E-P1-05 `p115client` 运行时依赖未声明（仓库无 requirements.txt），缺失时离线提交与 `/redirect` 静默失败；测试还依赖未声明的 httpx

- **证据**
  - `git ls-files` 无 `requirements.txt` / `requirements.in`；插件目录只有 8 个 `.py`。
  - `p115client` 只在函数内惰性 import：`av115_pan.py:77`（`_web`）、`:171`（`add_magnet`）、`__init__.py:561`（`_download_url`）。宿主 `/workspace/moviepilot/requirements.in` 中没有 `p115client`（有 httpx、pillow），即 MoviePilot 本身不提供。
  - 实测（干净 cwd，无 p115client）：匿名 `/redirect` 返回 `502`，body 为 `resolve failed: No module named 'p115client'`；`_push_offline` 同理会把任务标 `failed`，只在日志里出现一行 warning。
  - 测试面：没有 `httpx` 时 10 个用例 import error（见 1.2）；README 的「测试」段未提任何依赖。
  - 缓解：`_cookie_value`（`__init__.py:584-599`）会回退读 `P115StrmHelper` 的 cookie；若用户装过 P115StrmHelper，其 requirements 可能顺带装了 p115client。但该链路既未声明也未测试。
- **影响**：按 README 安装但没装 P115StrmHelper 的环境里，“提交磁力/ed2k”和“Emby 播放跳转”两个主功能会在运行期失败，且用户只能从日志发现；CI/新开发环境跑测试直接红。
- **建议修复**：新增 `plugins.v2/av115scraper/requirements.txt`（内容 `p115client`，按宿主 `__install_dependencies_if_required` 的约定放在插件目录即可被安装：`app/helper/plugin.py:896-915`）；README 安装/测试段写明依赖；把 `import p115client` 失败转成可读错误（例如 `_download_url` 抛「缺少 p115client，请重装插件」），并在 `/status` 的 error 中体现。
- **可验证方式**：`pip install -r plugins.v2/av115scraper/requirements.txt` 后 `python3 -c "from p115client import P115Client"`；无依赖环境下 `/task` 提交返回明确错误文案而不是 502 内部异常。

### 3.3 P2

#### E-P2-01 配置重入时旧 watcher 未被中断，可与新 watcher 并发提交同一任务（重复离线任务）

- **证据**
  - `__init__.py:87-90`：`init_plugin` 先 `previous.set()` 再换新 `Event`；`:154` 立刻起新线程。旧线程只在 `_watch_offline` 的 20s 等待点检查 cancel（`:467-475`），若它正卡在 `_flush_offline` 的网络调用里，cancel 不会打断。
  - 更隐蔽的窗口：`_watch_offline` 在**线程开始执行时**才 `cancel = getattr(self, "_cancel", None)`（`:468`）；若线程 T1 已 start 但尚未被调度，第二次 `init_plugin` 已把 `self._cancel` 换成 E2，T1 会绑定 E2，导致 T1 永不退出。
  - 实测：`scenarios.py` D1 —— 连续两次 `init_plugin` + 慢速 `add_magnet`，同一个任务被提交 **2 次**；A3 —— 两次 `init_plugin` 后同时存在 2-3 个 `av115-offline` 线程。
- **影响**：用户在配置页快速连点保存/热重载时，同一磁力可能被 115 建两条离线任务；旧线程永不退出会一直轮询。
- **建议修复**：watcher 改为单例（`if getattr(self, "_watch_thread", None) and alive: return`），或 `_cancel` 在启动线程时作为参数传入（`Thread(target=self._watch_offline, args=(event,))`）；`_flush_offline` 内每个任务提交前在 `_intake_lock` 下做“queued→submitting”原子抢占；每个任务之间检查 cancel。
- **可验证方式**：并发两次 `_flush_offline`（Barrier 同步假 `add_magnet`）断言只调用 1 次；连续两次 `init_plugin` 后线程数不超过 1。

#### E-P2-02 无 cookie/禁用状态下，`_watch_offline` 每 20s 重写一次相同的任务数据

- **证据**：`__init__.py:467-475` 固定 20s 循环；`_push_offline` 在无 cookie 时把任务设回 `status="queued"` 并 `_update_task`（`:535-540`），而 `is_offline_task` 对 `queued` 恒为真（`av115_intake.py:34-41`），下一轮又走一遍。
- **影响**：长期空转写插件数据（DB 写/唤醒），且任务永远停在 queued，用户只能看日志；与 E-P1-02 叠加时即使插件禁用也在写。
- **建议修复**：无 cookie 时不重复写相同状态（比较后跳过），或指数退避并停止轮询直到配置变化；`status` 增加 `blocked`。
- **可验证方式**：fake `save_data` 计数，无 cookie 跑 2 轮应只写 ≤1 次。

#### E-P2-03 每轮新建 `Pan115`/`httpx.Client` 且从不关闭，长时间运行存在连接/句柄泄漏

- **证据**：`av115_pan.py:40-47` 每个 `Pan115` 创建一个 `httpx.Client`；`__init__.py:497`（`_settle_downloads`，每 20s 一次）、`:556`（`_add_offline`）、`:675`（`_execute`，每次扫描）各 new 一个，插件没有 `close()`/`with` 关闭；测试里反而是手动 `pan.http.close()`（`test_av115.py:165,230`）。
- **影响**：有排队离线任务且下载持续数小时时，每 20s 泄漏一个 Client（连接池/句柄），长期运行有资源增长风险。
- **建议修复**：`Pan115` 实现 `close()`；`_settle_downloads`/`_add_offline` 用 `try/finally` 关闭；扫描用共享 client 或 `_execute` 结束即关；插件 `close()` 钩子里统一回收。
- **可验证方式**：fake httpx 统计 `close` 次数 = 创建次数；或运行 100 轮后 `len(gc.get_objects())` / FD 数量不增长。

#### E-P2-04 `_url_cache` / `_url_lock` 懒创建有竞态，缓存无上限也无清理

- **证据**：`__init__.py:565-581` 用 `if not hasattr(self, "_url_cache")` 懒创建；两个并发 `/redirect` 首次调用可能各建一份 dict/lock，互相覆盖。缓存 key=(pickcode, ua)，只在命中时判断 `hit[0] > now`，过期条目永不删除。实测 `scenarios.py` L1/L2：同 key 命中、52 个 key 全部常驻内存。
- **影响**：Emby 播放器 UA 多种多样 + 长期运行 → 内存缓慢增长；并发首次请求的锁覆盖使缓存失去保护（最坏只是重复取链，不崩）。
- **建议修复**：`init_plugin` 里预建 `_url_cache`/`_url_lock`；用 `OrderedDict`/TTL 清理，或缓存上限（如 512）+ 过期清理。
- **可验证方式**：注入假 `time.monotonic`，断言过期 key 被删、上限生效。

#### E-P2-05 `_intake_lock` 懒创建同样有首次并发竞态（读改写可能丢任务）

- **证据**：`__init__.py:444-445`（`_enqueue`）与 `:613-614`（`_update_task`）各自 `if not hasattr(self, "_intake_lock"): self._intake_lock = threading.Lock()`；`init_plugin` 不预建。两个线程首次同时进入时可能各建一把锁，之后 `_load_tasks()`+`save_data()` 的读改写不再互斥（`:454-457`、`:615-623`）。
- **影响**：低概率下“API 新收任务”与“watcher 更新旧任务”并发写，后者覆盖前者，丢一条排队任务。
- **建议修复**：在 `init_plugin` 里和 `_lock` 一起预建 `_intake_lock`（一行改动）。
- **可验证方式**：多线程首次并发 `_enqueue`/`_update_task` 断言无丢失（例如 100 轮，最终任务数 = 期望）。

#### E-P2-06 `/redirect` 匿名换取任意 pickcode 的 115 签名直链：无归属校验、无限速、502 回显内部异常

- **证据**
  - `__init__.py:198-204`：`/redirect` 注册 `allow_anonymous: True`（宿主据此不加 `verify_token`/`verify_apikey`：`app/api/endpoints/plugin.py:157-167`）。
  - `__init__.py:370-405`：只校验 pickcode 是 17 位字母数字，不校验该 pickcode 是否属于插件扫描的库/本用户视频；命中后 `_download_url`（`:560-582`）用宿主保存的 115 cookie 换签名直链并 302。
  - 失败分支 `:400-404`：匿名调用者能看到 `resolve failed: <异常原文>`（实测 502 body `resolve failed: No module named 'p115client'`）。
  - 实测：`scenarios.py` F4 —— 任意 17 位 pickcode 都得到 302 到 `_download_url` 结果；F5 —— 非 17 位返回 400；F6 —— `file_name` 里的 CRLF 被 `quote` 编码，无响应头注入。
- **影响**：只要 pickcode 泄漏（strm 被分享、日志/截图、第三方播放器上报），任何未认证请求都能拿到可下载直链、消耗宿主 cookie 的配额；也能用 502/400 差异探测 pickcode 是否存在。pickcode 是 17 位不透明串，不可枚举，故定 P2 而非 P1。
- **建议修复**：启动扫描时缓存“库内 pickcode 白名单”，`/redirect` 只放行白名单（或校验 `file_name` 与 pickcode 绑定）；加简单速率限制/UA 限制；502 只回显通用文案，异常细节走日志。
- **可验证方式**：未扫描过的 pickcode 返回 403/404；连续请求触发限速；响应体不出现 Python 异常文本。

#### E-P2-07 README 泄漏内网拓扑与个人环境

- **证据**：`README.md:5`（Unraid Emby `192.168.1.9:8096`、Windows）、`:11-12`（`mp.yuanhot.top`、`192.168.1.9:3000`）、`:18`、`:22`、`:30`、`:34`（`D:\git\moviepilot`）；`__init__.py:40-48` 的 UI 提示同样写死这些地址。
- **影响**：公开仓库/截图会暴露内网 IP、域名、目录结构；换环境的人照抄会直接连到作者家里。
- **建议修复**：README 用占位符（`http://<emby-host>:8096`）并单列一节“示例环境”；代码里 UI 文案改为配置项或通用描述。
- **可验证方式**：`grep -nE "192\.168|yuanhot|D:\\\\" README.md plugins.v2/av115scraper/__init__.py`。

#### E-P2-08 文档细节与实现不一致

- **证据**
  - `README.md:64` 与 `package.v2.json:15`（v1.5.5 历史）都说 FC2 失败后备是“官网和 Wayback”，实际还有两个备用站 `fc2ppv-db.com`、`missav.ws`（`av115_fc2.py:369-386`），README 未提。
  - `README.md:62` 列出的本地文件是 `SSIS-001-C.strm`、`movie.nfo`、`poster.jpg`、`fanart.jpg`，实际 `write_library` 还会写 `folder.jpg`（`av115_library.py:167`）。
  - `package.v2.json:4` 的 description 与 `__init__.py:53` 的 `plugin_desc` 文案不同；宿主 skill 要求二者尽量一致（`/workspace/moviepilot/skills/create-moviepilot-plugin/SKILL.md:217-219`）。
  - `README.md:40/318` 写“请求头带 API 令牌”，但 `/task` 是 `auth="bear"`（`__init__.py:182-196`）；宿主 `verify_token`（`app/core/security.py:254-285`）只接受 JWT、`X-API-KEY`/`?apikey=`、`?token=`，**不接受 `Authorization: Bearer <API_TOKEN>`**（会被当 JWT 解析失败 403）。按常见直觉写会踩坑。
- **影响**：用户按文档操作后行为不符（找不到备用站说明、播放失败排查方向错误、API 调用 401/403）。
- **建议修复**：README 补备用站与 `folder.jpg`；统一 description；API 文档明确写 `X-API-KEY: <API_TOKEN>`（或把 `/task` 改为 `auth="apikey"` 若只是脚本集成）。
- **可验证方式**：逐条改 README 后 grep 对照；用 `X-API-KEY` 和 `Authorization: Bearer` 各调一次 `/task` 验证。

#### E-P2-09 `interval_seconds` 默认值三处不一致（3 / 3 / 8）

- **证据**：类属性 `_interval_seconds = 3`（`__init__.py:80`）；表单默认 `"interval_seconds": 3`（`:300`）；`init_plugin` 的缺省回退是 `_as_int(config.get("interval_seconds"), 8)`（`:138`）。
- **影响**：老配置没有该键时实际用 8 秒，表单显示 3；用户以为改了间隔却没变（或反之），也影响扫描耗时。
- **建议修复**：抽常量统一，例如 `DEFAULT_INTERVAL_SECONDS = 3` 三处引用。
- **可验证方式**：`init_plugin({})` 后断言 `_interval_seconds == 3`；`get_form()` 默认值与类属性一致。

#### E-P2-10 icon 资源 `Moviepilot_A.png` 在插件仓库与宿主后端均不存在（前端解析路径待确认）

- **证据**：`package.v2.json:7` 与 `__init__.py:54` 都写 `Moviepilot_A.png`；`git ls-files` 没有任何 png/`icons/` 目录；宿主 `/workspace/moviepilot` 中 `find . -name '*.png'` = 0；后端静态文件路由只会从 `app/plugins/<id>/<filepath>` 取文件（`app/api/endpoints/plugin.py:764-795`），插件目录无该文件则 404。前端 `MoviePilot-Frontend` 的 `src/components/cards/PluginCard.vue:359-368` 用 `./plugin_icon/${plugin_icon}` 取图，但其源码仓库 `public/` 下没有 `plugin_icon/` 目录（GitHub API contents 核对，404）。
- **待确认**：官方市场仓库 `MoviePilot-Plugins/icons/Moviepilot_A.png` 确实存在（raw URL 返回 `image/png`）。前端发行版是否内置该图标、离线/本地仓库安装时是否回退默认图标，无法从提供的宿主源码确认。
- **影响**：本地仓库安装时插件卡片可能无图标或裂图；作为第三方插件市场上架时图标会 404。
- **建议修复**：在仓库根/插件目录提供 `icons/Moviepilot_A.png`（或改用 `mdi-*` 图标名，前端对 mdi 走 Vuetify 内置）。
- **可验证方式**：安装后打开插件页看 `./plugin_icon/Moviepilot_A.png` 是否 200；或 `find` 确认文件存在。

#### E-P2-11 仓库没有 CI，测试命令只能手工执行

- **证据**：`ls -a /workspace/mp-av` 无 `.github/`；`git ls-files` 无 workflow/脚本；README 只有手工命令（`README.md:68-79`）。
- **影响**：51 个已有用例无法在提交/PR 阶段自动跑；本次“缺 httpx 即 10 错”这类环境问题没有 CI 会立即暴露。
- **建议修复**：加最小 GitHub Actions：`python -m pip install httpx && python -m unittest discover -s tests -v`。
- **可验证方式**：提交后 Actions 运行；故意删 `httpx` 应红。

#### E-P2-12 测试自身的边界盲区：dry_run 无“没写盘”断言、封面裁剪分支、README 命令依赖未说明

- **证据**
  - `tests/test_av115_fc2.py:380-385` 只断言 dry_run 不请求 FC2，**没有断言临时 `strm_root` 下没有产生任何文件**；如果 `_handle_video` 的 dry_run 早退被删掉，这个测试仍然会通过（只是输出目录多文件）。
  - `av115_library.py:126-142` 的 `crop_poster` 在测试环境（无 Pillow）永远走 `return data`；`tests/test_av115_fc2.py:305-336` 只验证“FC2 不调用 crop”，没有验证 JavBus（`crop=True`）会调用 `crop_poster`（`av115_scan.py:319`），也没有裁剪几何/质量断言。
  - `tests/test_av115_play.py:174-177` 的 `run_scan(None, ...)` 只测缺播放模板早退，`run_scan` 完整流程（`_collect` 遍历、目录深度、跳过规则、rename）无测试。
  - README 测试命令未写先装 `httpx`（与 E-P1-05 同一根因）。
- **影响**：dry_run 保证和封面行为属于“看着有测试其实测不到”的状态，重构时容易悄悄回归。
- **建议修复**：dry_run 用例加 `self.assertEqual(list(Path(root).rglob("*")), [])`；用 `sys.modules["PIL"]`/`PIL.Image` fake 覆盖 `crop_poster` 两种分支（或把 Pillow 加进开发依赖）；给 ``run_scan`` 补一个完整 fake storage 用例。
- **可验证方式**：删掉 dry_run 早退/改动 crop 参数，新用例应当失败。

---

## 4. 已确认正常（核实项）

1. **版本与索引一致**：`package.v2.json` 的 key `Av115Scraper`、`version 1.5.8`、`history["v1.5.8"]` 均与 `__init__.py:52-60` 的 `plugin_name/plugin_version` 对应；`icon`、`author`、`labels/level` 与类属性方向一致；`system_version ">=2.0.0,<3"` 与宿主 `v2.15.6` 兼容（`app/helper/plugin.py:160-188`）。
2. **README 测试命令真实可跑**：两种写法都通过（见 1.1）；README 的 `AV115_FC2_LIVE=1` 联网用例实现为 `skipUnless`，默认跳过（`tests/test_av115_fc2.py:404-431`），不会误连网。
3. **扫描层 dry_run 不写云盘/本地、不刷新 Emby**：`run_scan` 在 `dry_run` 下跳过本地路径/模板校验（`av115_scan.py:170-178`）；`_handle_video` 在 `dry_run` 时于写盘前返回（`:252-256`），`_rename_file`/`_rename_folder` 的 dry_run 分支只累加计数不动云盘（`:344-347, 375-378`）；`_execute` 的 Emby 门禁是 `self._refresh_emby and not self._dry_run and wrote`（`__init__.py:687-690`），实测 J1 不刷新、J2 正式模式刷新。
4. **`/redirect` 的输入校验与响应头安全**：pickcode 必须 17 位字母数字，否则 400（`__init__.py:372-378`，实测 F5）；`file_name` 经 `quote` 编码（`av115_library.py:30`），CRLF 无法注入响应头（实测 F6）。
5. **日志不输出 cookie/直链**：全部 `logger.*` 调用（`grep -n logger.`：15 处）都在打数量、番号、目录、错误摘要；成功 302 只打印 `location.split("?", 1)[0]`（`__init__.py:398`），不打印 pickcode/签名 URL；任务文本（磁力）也不会被整条打印。cookie 只出现在 `Pan115.http` 的请求头（`av115_pan.py:40-47`）和 `_cookie_value` 返回值里。
6. **`/task`、`/run`、`/status` 鉴权配置合理**：`/run`、`/status` = `apikey`；`/task` POST/GET = `bear`（宿主 `verify_token` 同时接受 `X-API-KEY`/`apikey`/`token`，因此脚本用 API 令牌也能调通，见 `app/core/security.py:254-285`）；只有 `/redirect` 匿名，符合 Emby 播放器无法带凭据的设计。
7. **run 互斥与取消语义正确**：`run()` 用 `_lock.acquire(blocking=wait, timeout=1800 if wait else -1)` 做互斥（`__init__.py:625-651`）；`_cancel` 已置位时立即返回“已取消”且 `finally` 释放锁（实测 E1）；`stop_service` 只置位 cancel（`:356-359`），`_execute`/`_collect`/`run_scan` 在每个目录、每个视频间检查取消（`av115_scan.py:135, 182, 198`）。
8. **线程为 daemon，插件卸载不会挂死进程**：`av115-once`/`av115-offline`/`av115-api`/`av115-scrape` 均 `daemon=True`（`__init__.py:153-154, 364, 531`）。
9. **`_cancel` 在重载时被替换**：`init_plugin` 先把旧 Event 置位再换新的（`__init__.py:87-90`），旧 run 的 `flag` 引用旧 Event，扫描会在下一个检查点停下（`:631-635`）。
10. **Emby 刷新走宿主公开生命周期**：`_refresh_emby_library` 使用 `ModuleManager().get_running_type_modules(ModuleType.MediaServer)`（宿主 `app/core/module.py:129-138` 存在），`EmbyModule.get_instances()` 返回 `Emby` 实例，`_Emby__refresh_emby_library_by_id`/`refresh_root_library` 在宿主 `app/modules/emby/emby.py:577,599` 确实存在；媒体库按名字匹配并用 `target.id`（`MediaServerLibrary.id` 存在）。实测 G1 走指定库分支（私有方法名依赖是 P2 级脆弱点，但当前宿主可用）。
11. **`_U115Storage` 与宿主 StorageChain 接口匹配**：`get_file_item(storage=, path=)`、`list_files(item, recursion=False)`、`rename_file(item, name)` 签名与 `app/chain/storage.py:46/94/112` 一致。
12. **热重载清理 `av115_*` 模块的声明与实现一致**：`__init__.py:12-18` 在导入时删除 `sys.modules` 里所有 `av115_*`，宿主热重载另清 `app.plugins.av115scraper*`（`app/core/plugin.py:641-671`）。
13. **配置迁移方向正确（但无测试）**：旧 `LEGACY_WAN_PLAY_BASE` 且未迁移过时改为 `WAN_PLAY_BASE` 并写回 `play_base_migrated`；缺失 `play_base_lan/lan_networks/strm_use_lan` 时补默认值并持久化（`__init__.py:99-130`，实测 K1）。
14. **`pending_text` 保存即入队并清空**：`__init__.py:143-147`，实测 K2（提交 `SSIS-789` 后任务入队、配置 `pending_text` 置空）。
15. **51 个用例可重复、无网络依赖**：所有抓取都注入假 fetch/storage；唯一联网用例默认 skip；我连续运行两次结果一致（`git status` 干净）。

---

## 5. 建议补充的测试清单（按价值排序）

> 全部可离线完成；前 5 组对应 P1，建议优先补齐。

1. **（最高）插件主类编排/权限/生命周期测试** —— 新建 `tests/test_av115_plugin.py`，用 `/tmp/harness.py` 的最小 `app.*` + `fastapi` stub 导入 `__init__.py`。
   - 测什么：`get_api()` 的 path/auth/allow_anonymous 组合；`api_redirect` 合法/非法 pickcode、`location` 分支、`_download_url` 异常→502；`api_add_task` 的 JSON/纯文本/`{"items":[...]}` 解析与 rejected 统计；`_enqueue` 上限 50、id 唯一；`init_plugin` 的默认值补齐/旧模板迁移/`pending_text`；`enabled=False` 不启 watcher（修复后）；`dry_run=True` 不刷新 Emby、`dry_run=False` 且有 wrote 才刷新；`get_service` 无效 cron 返回 `[]`（fake `apscheduler.triggers.cron.CronTrigger`，本容器无 apscheduler）。
   - 怎么 fake：`_PluginBase` stub 提供内存 `save_data/get_data/update_config`；`run_scan`/`Pan115`/`_refresh_emby_library` 打桩；`threading.Event` 控制线程退出。
2. **dry_run 全链路“零写”测试** —— fake storage（记录 `rename` 调用）+ FakePan（记录 `ensure_dir/add_magnet/relocate_code`）+ 临时 `strm_root`。
   - 测什么：`run_scan(dry_run=True)` 后 `storage.rename` 0 次、`write_library` 0 次、临时目录为空、`_flush_offline` 后 FakePan 0 次；`dry_run=False` 时以上各 1 次。
   - 怎么 fake：`ScanOptions(dry_run=True)` 走真实 `_handle_video`；`_flush_offline` 走真实 `_push_offline/_settle_downloads`，只替换 `mod.Pan115`。
3. **Pan115 / CookieStorage 115 通路测试**（当前 0 进入，价值最高的一块网络面）
   - 测什么：`_checked` 的 200/非 200/非 JSON/`state=False+errno`；`_call` 的 405/429 重试（顺序 5/20/40，`_pace` 用 fake sleep）；`list_children` 分页（500 条边界）；`_from_aps` 文件/目录字段映射；`_items` 的 data/list 兼容；`search` 参数；`move`/`rename` 的 POST 数据键名；`_relocate_file` 的 video/junk/ready/ignore 四分支；`relocate_code` 的计数与 `code_id` 幂等；`CookieStorage.stat/list_dir/rename`（含 `FileNotFoundError`）。
   - 怎么 fake：monkeypatch `pan._call` 或 `pan.http.request` 返回 `httpx.Response`；`_web` 用测试已有的 `_WebClient` 扩展 `fs_mkdir`；`time.sleep`/`_pace` 打桩。
4. **JavBus 解析与降级测试**（当前 0 进入）
   - 测什么：`detail_urls` 有码/FC2/无码顺序；`parse_detail` 从最小 HTML fixture 提取 code/title（去掉前缀）/cover（`//`、相对 URL）/发行日期/长度/genre/star-name；缺 `識別碼`/空页返回 None；`JavBusClient.scrape` 的 404→第二 URL、异常→continue、`HTTP 500` reason、成功即停；`default_fetch` 优先 `app.utils.http.RequestUtils`，异常回退 `_urllib_fetch`（fake 模块）。
   - 怎么 fake：`fetch(url)->(status,html)` 闭包记录调用；`sys.modules["app.utils.http"]` 注入 fake `RequestUtils`。
5. **run_scan / 重命名 / 冲突 / 跳过规则测试**（当前全被早退绕开）
   - 测什么：`_collect` 多层目录、`_MAX_DEPTH`、取消、`stat` 失败/目录不存在；`_is_video` 的扩展名/大小边界（299/300MB）/`预告|sample|trailer|preview`；`_rename_file` 的已存在目标不覆盖、dry_run 文案、异常文案；`_rename_folder` 的 `counts!=1`/根目录/已存在/成功；`run_scan` 完整流程的 `ok/renamed/scraped/skipped/failed` 汇总、`interval_seconds` 仅在上一条 scraped 后 sleep。
   - 怎么 fake：内存 `storage` 类（dict 目录树 + rename 记录 + 可注入异常）；`sleep` 记录。
6. **线程/生命周期回归测试**（对应 E-P2-01/02/03/05）
   - 测什么：连续 `init_plugin` 后 watcher 数 ≤1；`stop_service` 后 1 个周期内不再提交；两个 `_flush_offline` 并发只提交 1 次；`Pan115` 每次创建都有对应 `close`；`_enqueue`/`_update_task` 并发不丢任务。
   - 怎么 fake：FakePan 用 `threading.Barrier` 强制并发窗口；fake `threading.Thread` 或检查 `threading.enumerate()`。
7. **`_download_url` 缓存测试**：命中/不同 UA/过期（fake `time.monotonic`）/无 cookie 抛错/并发首次调用；上限与清理（修复后）。
8. **`_refresh_emby_library` 测试**：fake `ModuleManager`（返回 EmbyModule/JellyfinModule 混排）、媒体库名大小写匹配、库不存在→`refresh_root_library`、无 Emby→提示、`_Emby__refresh_emby_library_by_id` 缺失→fallback；`_execute` 中 `refresh_emby=False` 不调用。
9. **`_offline_message` / `_payload_text` / `_as_int` / `_read_body` / `_page_text` 纯函数测试**：`state/errno/已存在` 判定、嵌套 dict/list 展开、`"{text}"` 占位过滤、非 JSON body、`_page_text` 截断 20/40 行与“预览不刷新 Emby”文案。
10. **`av115_codes` 边界补测**：`_match` 的 HEYZO/PONDO/CARIB 优先级、`_BAD_PREFIXES` 全表、`_DOMAIN` 清洗、`_normalize` 补零、`merge_tag` 合并；`choose_code` 文件/文件夹同码合并 tag。
11. **`library` 写盘细节测试**：`write_library` 首次/已存在/`force` 三种组合对 `strm/nfo/poster/fanart/folder.jpg` 的写/不写矩阵；`find_existing` 的“根下直接命中”和“演员子目录命中”；`destination` 有/无演员；`safe_name` 非法字符/保留名/80 截断。
12. **封面裁剪测试**：注入 fake `PIL`/`PIL.Image` 覆盖 `crop_poster` 横图裁剪几何与 JPEG 输出；无 PIL 原样返回；断言 JavBus 路径 `crop=True` 被调用、FC2 `crop=False` 不被调用。
13. **文档/版本一致性断言（防回归）**：在现有 `test_user_copy_mentions_ed2k` 基础上加：`package.v2.json` 的 `version == plugin_version`、`history` 含当前版本、`icon` 文件存在（若作为仓库资源）、README 的四个默认配置值与 `av115_play.py` 常量一致、README 测试命令中的依赖已声明。
14. **FC2 补充分支**：`parse_inertia` 非法 JSON/无 `component`；有 page 无 `version` 时不发 actress 二次请求；Wayback 快照被 challenge 拦截的 reason；`download_cover` 无 download 回调/异常；`_get` 返回非法 tuple。

---

## 6. 未决/待确认

1. **icon 前端解析**：`Moviepilot_A.png` 在官方市场 `icons/` 存在（raw 200 image/png），但前端源码无 `public/plugin_icon/` 目录，`./plugin_icon/${icon}` 在本地插件仓库安装时是否 404、是否有默认回退，未能在提供的宿主源码中确认（E-P2-10 标待确认）。
2. **p115client 真实 API 签名**：容器未安装 p115client，`P115Client(cookie, app="web")`、`clouddownload_task_add_url({"url","wp_path_id"})`、`download_url(pc, user_agent=, app=)` 是否为实际可用签名只能静态推断，建议集成测试或对照 p115client 版本确认。
3. **115 离线下载中的文件是否可见**：`_settle_downloads` 只要 `relocate_code` 返回 moved/ready>0 就标 `packed=True` 并起刮削（`__init__.py:509-533`）；若 115 在下载未完成时即暴露临时文件，可能把半成品搬进 `/AV`，本环境无法验证，需真机确认。
4. **详情页 VTextarea 提交**：`get_page` 的按钮事件直接调 `plugin/Av115Scraper/task`（`__init__.py:334-343`），`_payload_text` 过滤字面量 `"{text}"`（`:906-923`）；前端是否把 textarea 实际值包成 `{"text": ...}` 无法在本仓库验证，建议用浏览器 Network 面板确认。

---

## 附录 A：覆盖率数据（`trace.py --count` + AST 语句行）

命令：`python3 -m trace --count --coverdir=/tmp/tr --summary /tmp/run_suite.py`。

| 模块 | 语句行覆盖（AST 首行） | 函数进入 |
| --- | --- | --- |
| `av115_codes.py` | 46/57 = 80.7% | 8/8 |
| `av115_fc2.py` | 226/284 = 79.6% | 27/28（`download_cover` 未进入） |
| `av115_intake.py` | 30/31 = 96.8% | 7/7 |
| `av115_javbus.py` | **0/146 = 0.0%** | **0/12** |
| `av115_library.py` | 77/103 = 74.8% | 7/7（`crop_poster` 只走无 PIL 早退） |
| `av115_pan.py` | 122/296 = 41.2% | **18/37** |
| `av115_play.py` | 106/129 = 82.2% | 13/13 |
| `av115_scan.py` | 147/321 = 45.8% | 13/18（`_collect/_is_video/_sibling_names/join/as_dict` 未进入） |
| `__init__.py` | **0/934 行、45/45 函数未进入（无 cover 文件）** | **0/45** |
| 合计（辅助模块） | 754/1367 = 55.2% | 93/130 = 71.5% |
| 合计（含主类） | — | **93/175 = 53.1%** |

口径说明：语句行按 AST 节点首行去重统计，非官方 coverage 口径；多行字面量可能造成少量伪缺失，但“函数是否被进入/整个模块是否被导入”的结论不受影响（`__init__.py` 没有 cover 文件即从未被 import）。

## 附录 B：实验脚本

- `/tmp/harness.py`：最小 `app.*`/`fastapi` stub，导入插件主类（约 150 行）。
- `/tmp/scenarios.py`：22 项行为检查（A 生命周期、B dry_run 泄漏、C 禁用仍提交、D 重入重复提交、E 取消、F `/redirect` 暴露面、G Emby 私有钩子、J dry_run/Emby、K 配置迁移与 pending_text、L URL 缓存）。当前 22/22 PASS（PASS 表示“缺陷行为被成功复现”或“正常行为被确认”，见各条说明）。
- `/tmp/no_httpx.py`：缺 httpx 时 10 个用例 import error 的复现脚本。

> 建议：上述脚本可作为补测试的起点搬进 `tests/`，但搬入前需把对插件内部属性的直接赋值改成构造配置 + 显式初始化，避免测试与实现细节过度耦合。
