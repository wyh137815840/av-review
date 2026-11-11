# 「115 AV刮削」v1.5.8 审计报告 B：115 网盘操作与扫描/改名落盘

- 审计员：auditor-pan（共享任务 `task-2`）
- 被审仓库与版本：`wyh137815840/mp-115` @ `3394c3f`（main 干净，插件 `plugin_version = "1.5.8"`）
- 审计文件：
  - `plugins.v2/av115scraper/av115_pan.py`（435 行，全量）
  - `plugins.v2/av115scraper/av115_scan.py`（448 行，全量）
  - 关联核对（只读）：`plugins.v2/av115scraper/__init__.py` 中 Pan115/CookieStorage/`_U115Storage` 的调用点与离线整理流程、`av115_library.py` 的本地写盘，以及 `README.md` 的规则清单
- 评审仓库产出：本文件。审计期间未修改 `/workspace/mp-av` 的插件与测试代码；实验脚本放在 `/tmp/audit_pan/`。

## 方法与证据来源

1. **静态审阅**：逐行阅读 `av115_pan.py`、`av115_scan.py`，并交叉阅读 `__init__.py`（调用方）、`av115_library.py`（写盘）、`av115_codes.py`（番号识别）与 `README.md`。
2. **基线测试**：`cd /workspace/mp-av && python3 -m unittest discover -s tests -v` → **51 用例 / 50 通过 / 1 skip（FC2 live）**，与预期一致。
3. **离线复现**（不访问真实 115）：用 `httpx.MockTransport` 模拟 115 App/aps 的 HTTP 响应来驱动真实的 `Pan115/CookieStorage`；用内存版 `FakeStorage` 驱动真实的 `run_scan`。实验脚本：`/tmp/audit_pan/probe_checked.py`、`probe_relocate.py`、`probe_storage.py`、`probe_scan.py`、`probe_scan2.py`、`probe_aps.py`、`probe_lifecycle.py`（运行结果见各条「已做验证」）。
4. **外部接口语义核对**：以 PyPI 上 `p115client 0.0.9.7.2` 的源码/文档为准，核对插件用到的接口：`/2.0/ufile/search`（`fs_search_app2`）、`https://aps.115.com/natsort/files.php`（`fs_files_aps`）、`/files/move`（`fs_move_app`）、`/files/batch_rename`（`fs_rename_app`）、`/files/getid`（`fs_dir_getid`）、`/files/add`（`fs_mkdir`）。参考：<https://pypi.org/project/p115client/> 的 `0.0.9.7.2` 源码（`p115client/client.py`、`p115client/tool/fs_files.py`）。**115 真实环境未调用**，标注为「待确认」的条目是只有文档/模拟证据、需要真实账号复现的部分。
5. 分级口径：**P0** = 可导致死循环/挂死、或大范围静默漏扫；**P1** = 功能错误、文件被错误搬动/误报成功；**P2** = 边界条件、错误上报、并发/资源、维护性问题。

## 总体结论

- **扫描/改名主流程的骨架是成立的**：dry_run 不落盘、已有 `movie.nfo` 只更新 strm、多正片文件夹只改文件名、目标同名不覆盖、300MB 与预告过滤、扫描根（目录）不改名、相对幂等，这些规则在模拟用例中都按 README 预期工作（见「已确认正常」与规则核对表）。
- **但有 1 个可以直接打挂大库扫描的分页缺陷（P0）**，以及 3 个 P1：错误响应被吞导致「移动/改名假成功」；离线整理对全盘搜索命中的文件不加来源校验，会把归一化目录/扫描根里的同番号文件搬走；当「115 目录」填的是文件路径时，其**父目录**会被改名（违反 README「扫描根目录本身不改名」）。
- 其余为边界/上报/并发/性能类 P2，共 14 条。
- 统计：**P0 × 1，P1 × 3，P2 × 14**。
- P0 发现后已第一时间 `send_message` 报备 Lead。

---

## 问题清单

### P0-1 aps 列目录用 `len(page) < 500` 判终止：目录超过 1200 项会静默漏文件，可能死循环

**证据**

- `plugins.v2/av115scraper/av115_pan.py:208-223`：

```python
def list_children(self, directory_id: str) -> list[dict]:
    items = []
    offset = 0
    while True:
        found = self._call("GET", "", url=_APS_LIST, params={
            "cid": directory_id, "limit": _PAGE, "offset": offset,
            "show_dir": 1, "aid": 1,
        })
        page = [_from_aps(row, directory_id) for row in _items(found)]
        items.extend(page)
        if len(page) < _PAGE:      # ← 唯一终止条件
            return items
        offset += _PAGE
```

- `av115_pan.py:17`：`_PAGE = 500`。
- 接口事实（`p115client 0.0.9.7.2` 对同一接口 `https://aps.115.com/natsort/files.php` 的文档，`fs_files_aps`）：**「这个函数最多获取任何一种排序条件下的前 `1200 + n` 条数据（`n >= 0` 是一个潜在的不定限制）……当 `offset >= 1200 + n`，则相当于 `offset=0&fc_mix=1`，即从头开始」**；p115client 自己的 `fs_files_iter` 用 `offset != resp["offset"] or limit > len(resp["data"]) or resp["count"] <= offset + len(resp["data"])` 终止 + `resp["count"] > 1200` 告警（见 `p115client/tool/fs_files.py` 的 `fs_files_iter`）。插件既没有比对返回的 `offset`/`count`，也没有 1200 上限保护。

**已做验证**（`/tmp/audit_pan/probe_aps.py`，按上述文档行为构造 MockTransport）

- 目录 2000 项、上限 1200：插件只发出 3 次请求（offset 0/500/1000），返回 **1200 项，静默漏掉 800 项**，无异常、无告警；`run_scan` 会把该目录当成「只有 1200 个孩子」继续跑。
- 目录 2000 项、上限 ≥1500（文档的 `n ≥ 300` 情形）：offset 到 1500 后服务端回绕到头部，返回仍是 500 条 → `len(page) < 500` 永不成立 → **offset 序列 0,500,1000,1500,2000,… 无限循环，`items` 无限增长**（模拟在第 9 次请求被 loop guard 截停）。

**影响**

- 直接子项超过 1200 的目录是真实场景（例如 `/AV/有码`、`/AV/FC2` 根下几千个番号文件夹）：**超过 1200 的部分永远扫不到、没有错误上报**，表现为「插件跑完了，但后段影片没生成 strm」。
- 若该次 aps 请求的可访问上限 ≥1500，则扫描线程死循环 + 内存持续增长；`run()` 的 `_lock` 不会释放（`__init__.py:625-651`），此后所有手动/定时运行都被「已有任务在运行」挡住，直到重启容器。

**建议修复（具体到函数）**

- `Pan115.list_children`：按 `resp["offset"]`、`resp["count"]` 终止（对齐 p115client `fs_files_iter` 的 `check_for_stop`）：`offset != 请求的 offset` 视为回绕，立即停止并告警；`count <= offset + len(page)` 停止；首次响应 `count > 1200` 时不要假装完整。
- 超过 1200 的目录改用 App `/files`（`fs_files_app2`，文档称 limit 7000 安全）或 p115client `iter_files(app="android")` 兜底拉全；或者向用户明确报错「目录条目超过 aps 上限」而不是静默截断。
- `CookieStorage.list_dir` 把「被截断」升级为错误/告警，避免 `_collect` 当成空目录/完整目录。

**可验证方式**

- 用 MockTransport 模拟 aps：`data = rows[offset:offset+limit]`，`offset >= 1200` 时把 offset 归 0（文档行为），观察 `list_children` 的返回条数与请求次数；当前实现 2000 项目录只回 1200，上限 ≥1500 时无限请求。修复后应报错或返回全部条目。

---

### P1-1 `_checked` 吞掉「`state:false` 但无 errno」的错误响应：move/rename 假成功，扫描与离线整理计数虚增

**证据**

- `av115_pan.py:327-339`：

```python
if payload.get("state") in (False, 0) and payload.get("errno") not in (None, 0, "0"):
    ...
    raise RuntimeError(...)
return payload          # state=False 但 errno 缺失/为 0 时，原样当成功返回
```

- 受影响的调用：
  - `Pan115.move`（`av115_pan.py:225-226`）/`Pan115.rename`（`228-229`）不检查 `_call` 的返回值，`_checked` 一放行就认为成功；
  - `relocate_code`（`av115_pan.py:195-202`）据此累加 `moved`；
  - `CookieStorage.rename`（`av115_pan.py:310-316`）据此返回新路径，`_rename_file`（`av115_scan.py:348-355`）据此给 `result.renamed += 1` 并写「文件已改为 …」。
- 历史对照：改用 App API 的提交 `df405b6`（v1.5.0，`git show df405b6 -- plugins.v2/av115scraper/av115_pan.py`）之前，move/rename 对返回是 **只要 `state in (False, 0)` 就抛错**；同一提交引入的 `_checked` 改成「errno 非空才抛」，属于回归。
- 官方语义：p115client 的 `check_response` 对任何 falsy 的 `state` 都抛异常，并兼容 `errno/errNo/errcode/errCode/code/msg_code` 多种错误码字段；115 确实存在 `{"state": false, "error": "文件错误"}` 这种不带 errno 的错误响应（p115client `fs_shasearch` 文档即为例）。因此「state=false ⇒ 必有 errno」的假设不成立。

**已做验证**（`/tmp/audit_pan/probe_checked.py`、`probe_relocate.py`、`probe_storage.py`）

- `_checked`：`{"state": false, "error": "登录失效"}`、`{"state": 0, "error": "..."}`、`{"errno": 990001, "error": "..."}`（无 state）、`{"code": 40101017, "message": "..."}` 全部 **NO-RAISE**；只有 `{"state": false, "errno": 990001}` 才会抛。
- `relocate_code`：mock `/files/move` 返回 `{"state": false, "error": "登录失效，请重新登录"}` 时返回 `(1, 0, 0)`，即报告「已移动 1 个正片」，实际未移动。
- 端到端扫描：mock `/files/batch_rename` 返回 `{"state": false, "error": "重命名失败：无权限"}` 时，`run_scan` 输出 `ok=True, renamed=1, failed=0`，消息为「文件已改为 ABC-123.mp4」；同一场景换成带 errno 的响应才会出现「重命名文件失败」字样。
- `list_children` 的搜索/列目录错误若使用 `code` 字段（无 errno）也会被吞：`_items` 取不到 `data` → 返回空列表 → 目录被当成空目录，扫描静默漏文件。

**影响**

- 云盘没有真正移动/改名，但离线整理任务被标成 `ready/packed`，扫描器显示「文件已改为 …」且 `renamed` 计数虚增；用户看到的是成功、实际没有变化，排查困难。
- 若列目录错误以无 errno 形式返回，会把「有文件」误判为「空目录」→ 大面积漏扫。

**建议修复**

- `av115_pan._checked`：直接对齐 `p115client.check_response`——`state` 为 falsy 一律抛错；错误码字段兼容 `errno/errNo/errcode/errCode/code/msg_code`；错误文本取 `error/error_msg/message/msg`。
- `Pan115.move`/`Pan115.rename`/`CookieStorage.rename`：返回/校验响应，显式区分「已在目标目录/已同名」等可幂等错误（如 errno 20004、91002 等），其余抛错；不要让 `_checked` 的返回值被调用方忽略。
- 注意 `_web_mkdir` 依赖「已存在」语义的部分走的是 p115client 的 `fs_mkdir`，不受影响；但 `_call`（App/aps）应严格。

**可验证方式**

- MockTransport 构造上述 payload，断言 `move/rename` 抛错、`relocate_code` 不计数；补充单元测试覆盖 `_checked` 的 5 类错误 payload。

---

### P1-2 `relocate_code` 对全盘搜索命中不做来源/番号校验：会把扫描根等目录里的同番号文件搬进 `首个 cloud_path/番号/`

**证据**

- `av115_pan.py:204-206`：`search()` 调 `/2.0/ufile/search`，参数只有 `search_value/limit:100/offset:0/aid`，**没有目录范围参数**。p115client 对同一接口（`fs_search_app2`/`fs_search`）的文档说明：`cid: int | str = 0 💡 目录 id。cid=-1 时，表示不返回列表任何内容`，并注明 **「搜索范围是从指定目录开始的整个目录树」**（缺省 cid=0 即从根开始的整棵树）；`search_iter` 文档亦写明「搜索到的文件名必须包含这个字符串」。
- `av115_pan.py:186-202`：遍历搜索结果，唯一过滤是 `identity` 去重与 `identity == code_id`（番号目录自身）；`av115_pan.py:231-244` 的 `_relocate_file` 只判断 `parent == code_id`，**没有任何「来源必须在离线目录/cloud_id 子树下」或「文件名确实是本番号」的校验**，`_keeps_video` 只过滤体积与预告词。
- 调用方 `__init__.py:497-521`：`library = self._library_path()`（`__init__.py:605-610`，取 `cloud_paths` 第一行）后 `relocate_code(code, library, cloud_id, min_bytes)`，即所有离线任务的整理目标固定是第一个扫描根。

**已做验证**（`/tmp/audit_pan/probe_relocate.py` 场景 A/E）

- 搜索返回三个位于 **其他目录（`pid=OTHER`）** 的文件：`SSIS-001.mp4`、`SSIS-001-C.mkv`、`SSIS-0010.mp4`，`relocate_code("SSIS-001", …)` 依次对三者发起 `POST /android/files/move`（`to_cid` 为 `ensure_dir` 返回的番号目录 id），返回 `(3, 0, 0)`。
- 目录命中情形同样会移动：搜索命中某目录（`fid/cid` 行）后列其直接子文件，任一 ≥300MB 且非预告的视频都会被搬入番号目录（见同脚本场景 D）。

**影响**

- 用户已经整理在扫描根/演员目录/第二个扫描根（如 `/AV/FC2`）中的同番号文件，会被「离线整理」逻辑搬去 `第一个扫描根/番号/`：文件位置被改动、可能造成重复（同一部片在两个根下各一份）、并把 `-C`/`-UC` 等变体搬进基础番号目录。虽然不丢数据（可手动搬回），但属于未经确认的破坏性整理。
- 当目标目录就是源目录时（已在 `code_id` 下），若 P2-7 的 `parent` 字段问题成立，`ready` 分支不可达，可能对已在番号目录的文件反复发起 move；若 115 对「移动到当前目录」返回错误，`_settle_downloads` 每 20 秒重试一次（见 P2-11）。
- 搜索文件名是**包含匹配**，`SSIS-001` 会命中 `SSIS-001-C`、`SSIS-0010` 等；代码不做番号等价校验（`av115_codes.choose_code`/`parse_av` 未参与）。

**建议修复**

- `relocate_code` 增加来源约束：只接受「位于 `cloud_id`（离线下载目录）或其子树下」的条目。`/2.0/ufile/search` 不带目录过滤，需在拿到结果后校验父链（用 aps 列目录逐级回溯到 `cloud_id`），或改用 `fs_files_app2(cid=cloud_id, …)` 只列离线目录里匹配 `code` 的文件，而不是全盘 search。
- 增加文件名校验：`choose_code(_name(item))` 或 `parse_av(...).code == code`，拒绝 `SSIS-001-C`/`SSIS-0010` 这类非同番号命中（或明确支持 tag 变体时再单独处理）。
- 目标目录已包含该文件（同 pickcode）时跳过，不重复 move；`_settle_downloads` 对 `already` 的判定配合 P2-7 修好。

**可验证方式**

- 用 MockTransport 让 search 返回 `pid != cloud_id` 的文件，修复前会看到 `/files/move` 请求；修复后应无请求且三个文件留在原目录。

---

### P1-3 扫描根填的是「文件路径」时，其父目录会被改名，违反「扫描根目录本身不改名」

**证据**

- `av115_scan.py:120-131`：`_collect` 明确支持 root 是文件（`if not node.is_dir: return ([node] if _is_video(...) else []), ""`），说明这是被支持的输入形态。
- `av115_scan.py:179`：`protected = set(roots)` 只包含 root 字符串本身；`av115_scan.py:358-362` 的 `_rename_folder` 只检查 `parent in protected`。当 root 是 `/AV/有码/ABC-123.mp4` 时，该视频的 `parent` 是 `/AV/有码`，**不在 protected 中**，且 `counts["/AV/有码"] == 1`，于是父目录被改名为番号。

**已做验证**（`/tmp/audit_pan/probe_scan.py` 场景 4）

```
roots = ["/AV/有码/ABC-123.mp4"]，云端另有 /AV/有码/OTHER-999.mp4（不参与本次收集）
结果：cloud renames: [('/AV/有码', '/AV/ABC-123')]
      ok, renamed=1, status=ok，消息「文件夹已改为 ABC-123」
```

**影响**

- 违反 `README.md:59`「扫描根目录本身不改名」：被改的还不是扫描根，而是扫描根所在、通常包含其他影片的分类目录；目录内其它影片被一起搬走，用户原来的 `/AV/有码/...` 路径全部失效（其它扫描根、strm 里的旧路径、人工脚本都会受影响）。
- 由于 `counts` 只统计本次收集到的视频，父目录里其它未收集的影片不会阻止改名（`_rename_folder` 只看配置文件里那几个 root 是否 protected）。

**建议修复**

- `run_scan`：构造 `protected` 时，若某个 root 经 `storage.stat` 判定为文件，则同时把 `parent_path(root)` 加入 protected（或在收集阶段返回 root 是文件的标记，处理时跳过 `_rename_folder`）。
- `_rename_folder`：增加一道保险——若 `parent` 是任意 root 的祖先（`parent in {parent_path(r) for r in roots}`），不改名。
- 配置入口（`__init__.py` 的 `_cloud_paths` 解析）校验每行必须是目录，文件路径直接报错，避免误配。

**可验证方式**

- FakeStorage：root 设为文件 → 修复前父目录出现在 rename 列表，修复后不出现。

---

### P2 级问题

#### P2-1 重命名失败未计入 `failed`，条目状态仍是 `ok`

- 证据：`av115_scan.py:333-355`（`_rename_file` 失败只 `messages.append(...)`）、`av115_scan.py:358-386`（`_rename_folder` 同理）、`av115_scan.py:216-217`；`result.failed` 只在 `write_library` OSError、缺 pickcode、刮削失败、root 读取失败时累加。
- 已验证（`/tmp/audit_pan/probe_storage.py` 第二段）：mock 115 返回 `{"state": false, "errno": 990001, "error": "登录失效"}` 时，`run_scan` 的结果是 `ok=True, failed=0`，条目 `status=ok`，消息里只有「重命名文件失败：…」。
- 影响：页面/API 把「改名失败」统计为成功；用户看「重命名 0，失败 0」但云端文件名没洗，且云盘文件仍是乱名时 strm 会按旧文件名生成（见 P2-6）。
- 建议：`_rename_file`/`_rename_folder` 在失败分支 `result.failed += 1` 并把行状态改为 `partial`/`failed`；`_handle_video` 依据 `result` 生成状态。
- 可验证方式：同上 mock，断言 failed≥1。

#### P2-2 重复处理：重叠扫描根/同番号多文件 → 同一文件处理多次、`counts` 虚增、本地写出多个 strm

- 证据：`av115_scan.py:181-193`（多个 root 分别 `_collect`，`videos` 列表不去重）、`av115_scan.py:190-193`（`counts` 按 parent 累加）、`av115_scan.py:197-215`（逐条处理）、`av115_scan.py:361`（`counts.get(parent) != 1` 就不改文件夹）。
- 已验证（`/tmp/audit_pan/probe_scan.py` 场景 9，以及用「rename 后回读名字」的 CookieStorage 仿真复跑）：roots=`["/AV","/AV/BOX"]` 且 `/AV/BOX/abc123.mp4` 为唯一视频时，同一个文件被处理两次。第二轮用收集阶段留下的旧文件名 `abc123.mp4` 去改 `ABC-123.mp4`，因「目标文件名已存在」而保留旧名，随后仍按旧 basename 写本地文件，最终 `Actress/ABC-123/` 下同时存在 `ABC-123.strm` 和 `abc123.strm`；`counts["/AV/BOX"]=2` 又导致这个单正片文件夹不满足「只有一部片也改名」（`renamed` 计数视第一轮是否改名成功而不同，重复请求与重复写 strm 则必然发生）。
- 影响：重复请求 115、`renamed`/`scraped` 统计失真；Emby 里同一电影目录出现两个 strm（重复条目/陈旧 URL）；单正片文件夹漏改名。
- 建议：`run_scan` 按 `(fileid 或 norm(path))` 去重 `videos`；或先对 roots 去重/检测包含关系并跳过被包含的子根；`counts` 用去重后的结果。

#### P2-3 `_MAX_DEPTH` 静默跳过；`norm()` 破坏含反斜杠/首尾空格的 115 名称导致整个根「列出失败」

- 证据：`av115_scan.py:16`（`_MAX_DEPTH = 10`）、`av115_scan.py:138-139`（`if depth > _MAX_DEPTH: continue`，无日志/无 failed）；`av115_scan.py:68-74`（`norm` 把 `\` 换成 `/`、`strip()` 并去尾部 `/`）、`av115_scan.py:146`（对 API 返回的目录路径做 `norm` 后入栈）、`av115_scan.py:235`。
- 已验证（`probe_scan.py` 场景 8、`probe_scan2.py` 场景 C/D）：11 层深度的视频被静默跳过（`run_scan` 结果全 0、无错误）；目录名含 `weird\name` 或尾部空格（`trailing `）时，入栈路径被改写成 `/AV/有码/weird/name`、`/AV/trailing`，列目录失败，整个 root 记一条 `failed`（且该目录每轮都失败）。
- 说明：115 允许通过上传产生非常规名字（p115client 的 `escape` 文档专门处理文件名里的 `/` 等），这类名字在真实库中存在可能；**「名称含 `\`/尾部空格」为待确认的真实可达场景**，但 `norm` 不应改写 API 返回的名字这一点是确定的。
- 建议：只对用户配置的 root 做规范化；API 返回的 name 原样保留并以 `posixpatht.escape` 之类的安全方式拼路径；对 `_MAX_DEPTH` 超限计一次 failed/告警（或改成可配置且默认更大）。

#### P2-4 `size == 0` 视为「未知」时绕过 300MB 过滤

- 证据：`av115_scan.py:112`：`if item.size and item.size < min_bytes: return False`；`size == 0`（未知）不会命中。`_size`（`av115_pan.py:417-421`）与 `_to_cloud`（`__init__.py:840`）在接口缺字段时都返回 0。
- 已验证（`probe_scan.py` 场景 7）：size=0 的 `BBB-222.mp4` 被收集并进入刮削流程。
- 影响：列表接口未返回体积时（或 aps 缺 `s`），预告/小文件会当作正片处理，违反 README「小于 300MB 会跳过」。
- 建议：把 0 视为未知——先按 pickcode/其它接口补体积，或在 `_is_video` 里对未知体积单独告警并跳过（至少不要当成「≥300MB」）。

#### P2-5 祖先目录被改名后，预收集的嵌套视频路径失效，嵌套文件/目录改名静默失败（顺序依赖）

- 证据：`av115_scan.py:197-215` 处理的是收集阶段抓到的旧 `video.path`；`av115_scan.py:222`/`235` 据此计算 `parent`/`actual_path`；祖先目录在更早一条视频处理时被改名（`_rename_folder`），后续嵌套视频的 `parent` 已不存在 → `_sibling_names` 抛 `FileNotFoundError` → 只追加「检查重名失败/检查文件夹重名失败」消息，不做改名。
- 已验证（`probe_scan2.py` 场景 A）：`/AV/pack/abc123.mkv` + `/AV/pack/sub/def456.mp4`，第一条把 `/AV/pack` 改为 `/AV/ABC-123`，第二条报「检查重名失败：/AV/pack/sub；检查文件夹重名失败：/AV/pack」，其文件与子目录都没有改名，但扫描仍记 `ok`、`scraped` 成功。
- 影响：同一轮扫描的结果依赖处理顺序；一部分文件保留乱名。
- 建议：按目录分组、自底向上处理（先处理更深的目录）；或在每次 rename 前用 fileid 重新定位（storage 提供按 id 解析路径），维护 rename 映射；`_rename_folder` 用父 id 而不是路径字符串比较。

#### P2-6 旧 strm 从不清理：改名/换 tag 后同一电影目录残留多个 strm

- 证据：`av115_scan.py:307-330` 只调用 `write_library`；`av115_library.py:156-158` 固定写 `dest / f"{safe_name(strm_name)}.strm"`，**不删除同目录下其它 strm**。
- 已验证（`/tmp/audit_pan` 场景）：第一轮改名前（或先关「文件也改名」）失败时写出 `Actress/ABC-123/abc123.strm`；第二轮改名成功后写出 `Actress/ABC-123/ABC-123.strm`，旧文件仍在 → 同一电影目录两个 strm。
- 影响：Emby 出现重复条目/陈旧 URL；用户以为已更新但播放器可能命中旧文件。
- 建议：`write_library` 写完新 strm 后清理同目录中不属于本次 `strm_name` 的 `*.strm`（或由扫描器维护「番号→strm 文件名」的映射后删除旧名）。

#### P2-7 `search` 只取前 100 条且 2.0 风格字段兼容不全（`parent_id` 等缺失）

- 证据：`av115_pan.py:204-206` `limit:100, offset:0` 固定，无分页；官方文档提示该接口最多 1 万条且「即使 offset/limit 正确也可能不足 limit」，单页 100 条不足以覆盖同番号多个命中。`_file_id`（`409-410`）、`_name`（`413-414`）、`_size`（`417-421`）已经为 2.0 风格字段加了 `file_id/file_name/size/file_size` 回退（说明作者见过该风格），但：
  - `_relocate_file`（`231-244`）的 `parent = item.get("pid") or item.get("cid")` **没有 `parent_id` 回退** → 若 `/2.0/ufile/search` 返回 2.0 风格行，`parent` 恒为 `""`，`parent == code_id` 的 `ready` 分支与「把误进番号目录的垃圾搬回云下载」分支**永远不会命中**；
  - `_file_id` 缺 `category_id` 回退、`_name` 缺 `category_name`、`_is_directory`（`424-429`）缺 `file_category` → 如果搜索返回目录行，目录行 identity 为空被 `relocate_code:187-189` 直接 `continue` 跳过；
  - `_child_dir`（`163-166`）用 `item.get("pid")` 过滤搜索结果，同样受 parent 字段缺失影响。
- **待确认**：`/2.0/ufile/search` 的真实响应字段风格（app1 的 `n/fid/cid/pid` 还是 app2 的 `file_id/parent_id/category_id`）。p115client 的 `normalize_attr` 按字段分派：含 `file_id/category_id` 即按 app2 处理，而 `fs_search_app2`/`fs_search`（open）是同一端点族，倾向 app2 风格，但未用真实账号验证。
- 已做验证（mock 返回 app1 风格时逻辑正常；把同样的逻辑换成 app2 风格行时 `ready`/junk 分支不可达、目录行被跳过——由代码路径直接可推）。
- 影响：若为 app2 风格：① 「已归位」和「垃圾回搬」失效，`relocate_code` 可能每次都对已在番号目录的文件再 move 一次；② 目录形式的离线资源（TORRENT 文件夹，内部文件名不含番号）无法整理，任务永远停在「还在下载」；③ 超过 100 条命中时漏找。
- 建议：`search()` 按 `offset += len(data)` 循环到 `len(data) < limit` 或 `count` 上限（仍受 1 万条限制），或改用 `p115client.fs_search_app2 + normalize_attr`；`_relocate_file` 增加 `parent_id`；`_file_id/_name/_is_directory` 补 `category_id/category_name/file_category`；用真实 115 抓一次 `/2.0/ufile/search` 响应确认字段后补单测。

#### P2-8 UID 解析过窄：大小写/空格/缺失时 `move()` 携带空 `user_id`

- 证据：`av115_pan.py:33-38` 只认 `key == "UID"`（区分大小写、`strip()` 后 `partition("=")`），`uid = value.split("_", 1)[0]`；`av115_pan.py:225-226` 的 `/files/move` 直接使用 `self.user_id`。p115client `fs_move_app` 文档的 payload 明确包含 `user_id`（默认取 `self.user_id`）。
- 已验证（`probe_relocate.py` 场景 F）：`"uid=123_A; CID=1"`、`"SEID=2"`、`" UID = 123_A ; CID=1"` 都解析出 `user_id=""`。
- 影响：cookie 非标准写法（小写、空格、缺 UID）时所有移动请求带空 `user_id`，115 大概率拒绝 → 离线整理每 20 秒失败重试、任务卡住；`_cookie_value` 用 `"UID=" in own` 做过滤，同样漏掉小写/空格写法。
- **待确认**：115 对空 `user_id` 的具体响应（文档未直说必填，但从 p115client 默认携带推断）。
- 建议：用 `http.cookies.SimpleCookie` 大小写不敏感解析；拿不到 UID 时退回 `P115Client(cookie).user_id`；明确报错「Cookie 缺少 UID」而不是静默空串。

#### P2-9 限速只覆盖 App/aps 的 `_call`；网页 `fs_dir_getid/fs_mkdir` 与离线提交不受 `_pace` 约束

- 证据：`av115_pan.py:319-324` `_pace()` 只在 `_call`（`252-269`）里调用；`_web_getid`（`83-95`）、`_web_mkdir/_call_fs_mkdir`（`97-139`）、`add_magnet`（`169-175`）直接走 p115client，不经 `_pace`、也无重试/退避。调用方 `_flush_offline`（`__init__.py:477-481`）会一次性把队列里所有 queued 离线任务 `_push_offline` 掉，每条 `_add_offline` 都是「新建 Pan115 → ensure_dir（多次 getid/mkdir）→ add_magnet」。
- 已验证（`probe_relocate.py` 场景 H）：`ensure_dir("/A/B/C")` 发出 3 次 `fs_dir_getid` + 3 次 `fs_mkdir`，`_pace` 的 sleep 为 0。
- 影响：一次粘贴 71 条 ed2k（仓库 `tests/fixtures/ed2k_uris.txt` 就是这种量级）会背靠背提交 71 次离线请求 + 71 次建目录，正是 115 风控最容易命中的场景；失败后 `is_offline_task` 只在错误文本含 `_get_id` 时才重试，其余直接终态 failed。
- 建议：复用单个 `Pan115`/`_web()` 客户端；在 `_web_getid`/`_web_mkdir`/`add_magnet` 前后统一调用模块级 `_pace()`；`fs_dir_getid/fs_mkdir` 加与 `_call` 一致的 405/429/5xx 重试；`_push_offline` 逐条之间加间隔。
- 可验证方式：mock p115client 记录调用时间戳，断言相邻调用间隔 ≥ `_GAP_SECONDS`。

#### P2-10 客户端生命周期：`Pan115.http` 从不关闭，且离线监视每 20 秒新建一次

- 证据：全仓库 `grep -n "\.close()"` 在 `av115_pan.py`/`__init__.py` 无命中；`Pan115.__init__` 每次 `httpx.Client(...)`（`40-47`）；`__init__.py:497` 在 `_settle_downloads` 里「有 pending 任务就每 20 秒 new 一个 Pan115」，`__init__.py:556` `_add_offline` 每个离线任务 new 一个 Pan115，`add_magnet` 每次再 new 一个 `P115Client`（`av115_pan.py:173`）；`_url_cache`（`__init__.py:560-582`）按 `(pickcode, ua)` 无限增长、过期项不淘汰。
- 已验证（`probe_lifecycle.py`）：未 close 的 `httpx.Client` 连接在 CPython 下多数靠 refcount 释放，但存在引用环时需 `gc.collect()` 才回收 socket（50 个客户端：open=105 fd → drop=17 → gc=10）；`Pan115`/`P115Client` 对象本身没有 `__del__` 保证。
- 影响：长驻 MoviePilot 进程里 fd/内存抖动，长时间卡住的任务越多、轮询越久越明显；`_url_cache` 只增不减。
- 建议：插件实例持有长生命周期 `Pan115`（或 `httpx.Client`）复用，`stop_service`/`finally` 关闭；`_settle_downloads` 复用同一实例；`add_magnet` 复用 `self._web()` 客户端；`_url_cache` 加容量上限/定时清理。

#### P2-11 离线整理无终态：`packed=True` 先置位、扫描失败不重试、无超时，卡住任务每 20 秒无限轮询

- 证据：`__init__.py:483-533` `_settle_downloads`：`moved + already <= 0` 时把任务留在 `offline` 并每 20 秒重试；成功时先写 `task["packed"] = True` / `status="ready"`（`517-519`），再启动 `run(roots=ready, wait=True)` 线程（`527-533`）——若该 `run` 因锁等待 1800 秒超时返回「已有任务在运行」、或因 `_cancel` 已置位直接返回、或扫描内部异常，**没有回滚 `packed` 的逻辑**，该任务不会再被 `_settle_downloads` 选中（`487-493` 过滤 `not task.get("packed")`），只能等下一次全量扫描碰巧覆盖到该目录。
- `relocate_code` 只处理搜索命中目录的**直接子文件**（`av115_pan.py:191-194` 对子目录 `continue`），所以 `云下载/CODE/sub/CODE.mp4` 这类嵌套结构永远搬不进番号目录，任务无限轮询。
- 影响：每 20 秒一轮 `ensure_dir`（web getid）+ `search`（App）+ 可能的 `list_children`（aps），既耗 115 请求也占资源；任务状态长期显示「等待下完」，不会失败。
- 建议：`packed` 在扫描成功（或被明确调度）之后再置位；引入尝试次数/截止时间，超过后置 `failed`；正片迟迟不出现时给出明确原因（体积过小/嵌套目录/下载失败）；递归或按 cid 子树处理离线目录内的视频。

#### P2-12 并发：离线监视线程的 115 写操作不受 `run()` 的 `_lock` 保护

- 证据：`__init__.py:625-651` `run()` 用 `self._lock` 串行化扫描；但 `_watch_offline`（`467-475`，每 20 秒）→ `_flush_offline`/`_settle_downloads` 里的 `Pan115.ensure_dir`、`relocate_code`（move）与 `_push_offline`/`_add_offline`（mkdir/add）**都没有拿这把锁**。扫描（cron/手动，可能持续数小时）与离线整理可以同时操作同一棵 115 目录树。
- 影响：扫描收集的路径/pickcode 可能在处理途中被 relocate 移动，导致改名失败或对错误文件操作（如扫描正在处理 `/云下载` 同番号文件时被搬走）；同一目录的 move/rename 交错没有幂等保证。
- 建议：离线整理与扫描共用同一把锁（或独立的 115 写锁），`_settle_downloads` 在扫描运行时暂停/排队；`relocate_code` 与扫描都改为「先解析 fileid 再操作」。

#### P2-13 `CookieStorage` 与 `_U115Storage` 语义不一致，同一份扫描逻辑两种模式结果不同

- 证据：
  - 重命名失败处理：`CookieStorage.rename`（`av115_pan.py:310-316`）只依赖 `Pan115.rename` 是否抛错，而 `_checked` 在「state=false 无 errno」时会吞（P1-1）→ cookie 模式可能报成功；`_U115Storage.rename`（`__init__.py:825-831`）在 `chain.rename_file` 返回 False 时抛 `RuntimeError` → 同一场景 storage 模式报失败。
  - pickcode 来源：`CookieStorage.list_dir` 的 `pickcode` 只来自 aps 行的 `pc`（`av115_pan.py:305`）；`_U115Storage` 用 MoviePilot 的 `FileItem.pickcode`（`__init__.py:841`）。**待确认**：aps 是否总返回 `pc`；若某些行缺 `pc`，cookie 模式会以「缺少 pickcode，无法生成播放地址」失败，而 storage 模式正常。strm 生成只依赖 pickcode（`av115_scan.py:264-273`），目前没有按 fid 计算的兜底（p115client 有 `to_pickcode(fid)`）。
  - 路径形式：`CookieStorage` 的 `CloudFile.path` 由插件自己 `join(folder, name)` 拼接；`_U115Storage` 用后端 `item.path`（`__init__.py:834-843`）。`run_scan` 的 `protected`/`counts` 全用字符串比较（`av115_scan.py:179,190-193,361`）；若后端返回的路径形式（大小写、首尾斜杠、前缀）与配置 roots 不完全一致，「扫描根不改名」的保护会失效。**待确认**（依赖 MoviePilot 存储实现）。
  - 目录 id 缓存：`CookieStorage` 走 `Pan115._dirs` 缓存，祖先目录改名后旧路径仍能解析出 id（嵌套改名反而能继续）；`_U115Storage` 每次按路径查，旧路径直接 FileNotFoundError。同一云端结构，两种模式在 P2-5 场景下的行为不同。
- 影响：同一配置只因为「填了 Cookie / 没填 Cookie」就走了不同分支，失败表现和结果不同，难以复现与支持。
- 建议：统一 storage 接口语义：`rename` 返回结果或抛错一致；`pickcode` 缺失时统一用 `p115client.to_pickcode(fid, stable_point)` 计算兜底；`run_scan` 在比较前把 roots/后端路径统一为同一规范形式（并由 storage 层保证）；或者干脆只保留一种实现（cookie 模式优先）。

#### P2-14 `run_scan` 的 `ok` 语义：有 failed 时仍可能返回 `ok=True`，以及一行无效分支

- 证据：`av115_scan.py:216-217`：

```python
if result.failed and not result.error:
    result.ok = True
```

  `result.ok` 初始为 True（`164`），只有「没有填写 115 目录 / 没填 strm 目录 / 没填播放地址」会置 False；这一行是 no-op。
- 已验证（`probe_scan.py` 场景 13）：roots=`["/nope"]` 时返回 `ok=True, failed=1`，条目 `status=failed`；用户/接口看到的整体结果是「成功」，只有 details 里有失败项。
- 影响：定时任务/接口调用方无法用 `ok` 判断失败；页面也显示「结束」而非失败。
- 建议：明确语义并在 `ScanResult.as_dict`/页面/接口上体现（例如 `ok = not result.error and result.failed == 0`，或保留 `ok` 表示流程跑完但新增 `failed>0` 的显式提示），删除无效分支。
- 可验证方式：缺根目录场景断言返回值的 `ok` 与 `failed` 组合。

---

## 已确认正常（核实过的实现 / 与 README 一致）

1. **cookie/UID 常规格式解析正确**：`UID=123_A; CID=…; SEID=…` → `user_id="123"`（`probe_relocate.py` F）。
2. **httpx 客户端基础配置合理**：`timeout=20`、`follow_redirects=True`、Cookie/UA 头；`httpx.TimeoutException/ConnectError` 均属 `TransportError`，连接类错误会重试（`probe_lifecycle.py`：4 次尝试后抛错）。
3. **`_RETRY_STATUS`/`_RETRY_WAITS` 重试生效**：mock 连续 2 次 405 后成功，共 3 次请求、`sleep` 序列 5/20（外加 `_pace` 2.5s 间隔）；业务错误（HTTP 200 + errno）不重试，避免重复 move/rename（`probe_relocate.py` G）。
4. **`_pace` 全局限速线程安全**：模块级 `_pace_lock` + `_last_call`；3 线程并发调用（`_GAP_SECONDS` 调成 0.1）实测相邻请求最小间隔 0.1s（`/tmp/audit_pan` 临时验证）。
5. **`ensure_dir` / `fs_mkdir` 竞态处理正确**：先 `fs_dir_getid` → 不存在走网页 `files/add`（`pid+cname`，兼容旧签名 `mkdir(name, pid=…)`）→ 20004/“已存在”时用 0.4s/0.8s 重试 getid → 最后 aps 列目录/search 兜底；root 返回 `"0"` 且不请求网络；失败抛 `无法创建 115 目录 …`；第二次调用命中 `_dirs` 缓存（与 `tests/test_av115.py` 的 9 个 `EnsureDirTests` 用例一致，基线 51/50/1skip 全过）。
6. **`files/getid` 只会返回目录 id**（p115client 文档明确「只能获取目录，不能获取文件」），`_web_getid` 不会把文件 id 误当目录 id。
7. **aps 行映射 `_from_aps` 与实际字段结构一致**：目录行 `n/cid/pid`（无 `fid`），文件行 `fid/n/cid/s/pc`；与 p115client `normalize_attr_web` 的字段判定一致（`av115_pan.py:349-366`）。其中 `pc`（pickcode）是否每行都有见 P2-13 的待确认项。
8. **move/rename 的端点与参数正确**：`POST /android/files/move`（`ids/to_cid/user_id`）与 `/android/files/batch_rename`（`files_new_name[file_id]`）与 p115client `fs_move_app`/`fs_rename_app` 一致（`av115_pan.py:225-229`）。
9. **relocate 正常路径可工作**：mock 搜索命中目录 → 列直接子文件 → 视频移入番号目录；已在番号目录的正片计 `ready`；番号目录里的垃圾回搬云下载；重复运行对已搬文件幂等（前提是 parent 能解析，见 P2-7）——用 app1 风格 mock 验证通过（`probe_relocate.py` A/B/D）。
10. **dry_run 完全不落盘**：不调用 rename/move、不请求 javbus/FC2、不创建本地文件/目录；仅列目录与拼预览路径（`probe_scan.py` 场景 11）。
11. **已有 `movie.nfo` 默认只更新 strm**：不重刮元数据、不覆盖 nfo/封面（`probe_scan.py` 场景 10；与 `README.md:60` 一致）。
12. **目录改名与多正片规则**：一个文件夹里 ≥2 部正片时只改文件名、单正片且非扫描根时改文件夹、`rename_folder=False` 时完全不改文件夹、扫描根（目录）本身不改名（`probe_scan.py` 1/2/3、`probe_scan2.py` E/F；与 `README.md:59` 一致）。
13. **目标已存在不覆盖**：文件改名目标名已存在时保留原名并提示；文件夹改名目标名已存在时保留原文件夹（`probe_scan.py` 场景 5；与 `README.md:58` 一致）。
14. **300MB / 预告过滤**：文件名含「预告/sample/trailer/preview」（大小写不敏感）与体积 <300MB 的视频被过滤（`probe_scan.py` 场景 6；与 `README.md:57` 一致）。
15. **`_collect` 遍历骨架合理**：DFS + 深度限制、root 为文件/目录均可、列出失败返回 failed item 且保留已收集项、每目录/每视频检查取消（`av115_scan.py:120-149,181-200`）。
16. **启动参数校验**：roots 为空 →「没有填写 115 目录」；非 dry_run 缺 strm_root/播放模板 → 明确错误（`av115_scan.py:165-178`；`tests/test_av115_play.py:174-177`）。
17. **`CookieStorage` 基本行为正确**：`stat("/")` 返回合成根、存在路径返回 CloudFile、父目录不存在返回 `None`、`list_dir` 对不存在目录抛 `FileNotFoundError`（`probe_storage.py`）。
18. **测试基线**：`51 tests / 50 pass / 1 skip`（FC2 live 被 skip）；插件与测试代码未被本次审计修改。

## 与 README 规则逐条核对

| README 规则（行号） | 实现位置 | 结论 |
| --- | --- | --- |
| 有码/FC2/无码番号格式，字幕 `-C`、破解 `-UC`（53-55） | `av115_codes.py` + `_handle_video` 用 `parsed.labeled` | 扫描侧按 `labeled` 命名，一致（番号识别本身由 A 审计） |
| 文件名没有番号时用所在文件夹名（56） | `choose_code(video.name, name_of(parent))`（`av115_scan.py:223`） | 一致 |
| 小于 300MB、文件名带预告/sample/trailer/preview 跳过（57） | `_is_video`（`av115_scan.py:106-117`） | 一致；但 `size==0` 未知时绕过（P2-4） |
| 目标文件名或文件夹名已存在不覆盖（58） | `_rename_file`/`_rename_folder` 的 `_sibling_names` 检查 | 一致；失败上报问题见 P2-1，重复 strm 见 P2-2/P2-6 |
| 扫描根目录本身不改名（59） | `protected = set(roots)`；`parent in protected` | 目录形态一致；**文件形态违背（P1-3）** |
| 一个文件夹两部以上正片只改文件名（59） | `counts[parent] != 1` 时不改文件夹 | 一致；重叠根/重复收集会把 counts 抬成 2（P2-2） |
| 已有 `movie.nfo` 默认只更新 strm，覆盖开关才重刮（60） | `need_meta` + `write_library(force=overwrite)` | 一致（已实测） |
| 未刮到时仍写 strm 和只含番号的 nfo（62） | `movie is None` 分支写 `_nfo(parsed, None)` + strm | 一致 |
| 只预览不改 115、不写本地（46） | dry_run 分支 | 一致（已实测） |

## 补充：测试覆盖缺口（建议）

- 现有 51 个用例集中在 intake（ed2k/磁力/番号）、`EnsureDirTests` 与 play/strm；**`av115_pan` 的 `_checked`/`move`/`rename`/`list_children`、`av115_scan` 的 `_collect`/`run_scan`/`_rename_file`/`_rename_folder` 没有自动化用例**。本次报告中的 P0/P1 都可以用 `httpx.MockTransport` + 内存 storage 写成低成本单测（`/tmp/audit_pan/probe_*.py` 即原型）。
- 建议至少补：`_checked` 的 5 类错误 payload、`list_children` 的 aps 截断/回绕模拟、`relocate_code` 来源校验、`_collect` 深度/循环/异常、`_rename_*` 的同名冲突与失败、dry_run 无副作用。

## 附：本次审计的边界与未覆盖

- 未访问真实 115 接口（按要求尽量不调）；所有涉及真实响应格式/风控行为的结论标注了「待确认」，并在方法中给出复现途径。
- `av115_codes.py`/`av115_javbus.py`/`av115_fc2.py`/`av115_library.py` 的番号识别、刮削与 Emby 写盘细节未作独立审计（只核对与扫描/落盘交互的部分）。
- `__init__.py` 中 API/页面/内外网跳转逻辑不在本任务范围，仅审计了与 Pan115/relocate/scan 直接相关的调用点。
