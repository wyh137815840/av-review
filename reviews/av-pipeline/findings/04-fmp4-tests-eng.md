# 04 · fMP4 抽帧实现、测试基线、入口/部署/文档一致性审计

- 被审仓库：`/workspace/av-review-main`（`wyh137815840/av-review` 的只读 clone），HEAD `8337582 Persist frames before success and stop ed2k ssh splits.`（2026-11-10 18:27 UTC）
- 宿主形态：grokbot-vm `/home/yuanhao/av-review` + Unraid 镜像副本 + moviepilot 容器 `/config/scripts`（见 `README.md:3-4`、`bin/av_auto.README.md:13-15`）
- 审计员：auditor-quality（团队任务 9 / `task-9`）
- 范围：`scripts/fmp4_http.py`、`scripts/av_auto.py` 的抽帧/帧持久化/process 编排、4 个测试文件、`bin/*`、`cron/`、`systemd/`、`README.md`、`bin/av_auto.README.md`、部署/工程性
- 纪律：仓库只读；未执行任何 ssh/Unraid/115/Emby/Magpie/docker 命令；实验全部在 `/tmp`，网络只用了 127.0.0.1 本地 HTTP 服务
- 结论一句话：**未发现 P0；发现 P1 5 条、P2 13 条。** 最关键的工程风险是「实际执行的是容器内 `/config/scripts/av_auto.py`，仓库没有同步/版本机制」「`av_review` 在 Python 3.11 直接 SyntaxError，而仓库/系统服务都没声明 Python 版本」「concurrent guard 失效，cron daily 与 API/手工可以重叠」「fmp4 解析层 0 单测，现有 fmp4 测试全部绑死 ffmpeg」。

---

## 1. 方法与可复现命令

### 1.1 测试基线（实测两遍一致）

```bash
cd /workspace/av-review-main && python3 -m unittest discover -s tests -v
# Ran 60 tests in 1.375s
# FAILED (errors=2)
```

- 4 个测试文件共 **66 个 `test_*` 方法**；实际 `Ran 60`（2 个 `setUpClass` 失败注入 2 个 error，各自 3 个用例未执行），**58 pass / 2 error / 0 fail**。
- 2 个错误的根因都是 **本机没有 ffmpeg**：`tests/test_fmp4_http.py:67` 的 `subprocess.check_call(["ffmpeg", ...])` 在 `IndexTests.setUpClass`（`:134`）与 `HttpExtractTests.setUpClass`（`:179`）里抛 `FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'`。这是环境缺依赖，不是代码缺陷；但它是 **error 而不是 skip**，导致「没有 ffmpeg 的机器上测试套件直接红」，并且 6 个用例（含全部 fmp4 解析/HTTP 测试）实际没跑。
- 缺 ffmpeg 时实际有效的 fmp4 测试只剩 `BoxTests` 的 2 个（`:104-128`：tfra 2GiB offset、Content-Range 解析）。

### 1.2 覆盖率（标准库 `trace` + AST 语句行，无 coverage 工具）

```bash
cat > /tmp/run_suite_av.py <<'EOF'
import sys, unittest
sys.path.insert(0, "/workspace/av-review-main/tests")
unittest.TextTestRunner(verbosity=0).run(unittest.TestLoader().discover("/workspace/av-review-main/tests"))
EOF
cd /workspace/av-review-main && python3 -m trace --count --coverdir=/tmp/tr_av --summary /tmp/run_suite_av.py
```

| 文件 | 函数进入 | AST 语句行覆盖 |
| --- | --- | --- |
| `scripts/fmp4_http.py` | **6 / 48** | **58 / 905 = 6.4%** |
| `scripts/av_auto.py` | 89 / 133 | 1161 / 2236 = 51.9% |
| `av_review/` 包 | **0 / 0（没有任何测试 import）** | — |

`fmp4_http.py` 里只有 `parse_box_header / iter_boxes / _parse_content_range / _parse_tfra / parse_mfra / _u32` 被进入；`BoundedHttpFile` 全部方法、`parse_moov / parse_moof_samples / parse_sidx / _find_mfra / build_fmp4_index / _scan_fragments / _realize_moof / _append_segment / materialize_snippet / open_fmp4_session` 全部 0 进入。也就是说 **本机环境下 fmp4 的解析、索引、HTTP、取片段都是 0 测试**（不是"测了没发现"，是根本没执行）。

### 1.3 静态检查

```bash
python3 -m py_compile scripts/av_auto.py scripts/fmp4_http.py av_review/*.py
# 只有 av_review/vision.py 失败：
# SyntaxError: f-string expression part cannot include a backslash（vision.py:561）
bash -n bin/av_auto bin/av_review-api-start.sh bin/av_review_ff_batch.sh cron/av-review.cron
# 全部 OK
```

`bin/av_review --help` 在 Python 3.11 可以跑；`python3 -c "import av_review.vision"` 在 3.11 直接 SyntaxError（见 F-P1-01）。

### 1.4 离线构造最小 fMP4 二进制验证解析层（不需要 ffmpeg）

`/tmp/fmp4_synth.py` 手工拼 ftyp/moov(mvhd/tkhd/mdhd/hdlr/stsd/mvex/mehd/trex)/sidx/moof(mfhd/traf/tfhd/tfdt/trun)/mdat/mfra(tfra/mfro)，用与仓库测试相同的 `MemFile` 鸭子类型驱动真实 `fmp4_http` 函数。28 项检查：25 PASS、3 FAIL（3 个 FAIL 是下面 F-P2-01/02/03 的实证，另 T2e 预期写反）。

`/tmp/fmp4_http_probe.py` 起本地 `ThreadingHTTPServer`（只监听 127.0.0.1），验证 redirect/Set-Cookie/闭合 Range/Content-Range/忽略 Range/redirect 环：10 项检查，9 PASS；唯一 FAIL（H8 "fetched < file size"）是因为合成文件只有 746 字节、小于 2MB header window，属预期，不是缺陷。**这证明 fmp4 的 HTTP 层完全可以不依赖 ffmpeg 做单测。**

`/tmp/av_auto_probe.py`：mock `av_auto._run_ff`，离线验证 `_snippet_seek` 钳位、`_extract_fmp4_frame` 端到端（生成 snip、seek、清理、`via=fmp4-range`）、`seek_points`/`_nearby_times` 边界、默认 32MB cap 下大分片行为。

`/tmp/fmp4_styp.py`：验证 sidx 子段带 `styp` 前缀时的行为（F-P2-01）。

`/tmp/probe_busy.py` + 正则核对：把 `BUSY_PATTERNS` 逐条匹配真实命令行（`av_review/bin/av_review daily`、`review`、`-m av_review daily`、`run_rest_scan.py`），复现忙闲检测漏判（F-P1-02）。

### 1.5 局限

- 没有 ffmpeg：真实转码/帧正确性、`-ss` 对 snippet 绝对 tfdt 的语义只能靠仓库已有集成测试推断，标「待确认」。
- 没有 p115client/Unraid/115/Emby/Magpie：115/Emby/Magpie 相关函数只能静态审查与 mock，标「待确认」。
- 覆盖率是 `trace.py` + AST 语句行口径（多行字面量可能有少量伪缺失），但"函数是否进入/模块是否导入"的结论不受影响。

---

## 2. 总体结论

| 级别 | 数量 | 摘要 |
| --- | --- | --- |
| P0 | 0 | 未发现不可逆数据损坏、密钥明文泄漏、未授权删除正式库等确定问题（P0 候选的 promote 回滚不完整目前只在失败路径，定 P1-05） |
| P1 | 5 | Python 3.11 下 `av_review` 主入口不可用却无版本声明；并发/忙闲保护失效；fmp4 解析层 0 单测且 ffmpeg 依赖把测试打成环境错误；部署副本无同步/版本机制；promote 回滚 0 测试且不完整 |
| P2 | 13 | fmp4 三个边界缺陷（styp、moof↔mdat 夹 box、cap 自相矛盾）；无索引线性扫描无 pacing；测试 fixture 缓存/环境 error；cron/systemd 部署可靠性；API /health 暴露面；secrets 缓存不一致；ff_batch URL 卫生；无 CI/依赖声明；av_review 0 测试；process 强制要 Magpie key；cmd_clean 对 size=0 正片的删除风险（待确认） |

**测试基线**：66 个测试方法 / 实际 Ran 60 / 58 pass / 2 error（缺 ffmpeg，环境性）/ 0 fail，1.4s。**覆盖基线**：`fmp4_http.py` 6/48 函数、6.4% 语句行；`av_auto.py` 89/133 函数、51.9% 语句行；`av_review/` 0 测试。

---

## 3. 问题清单

### 3.1 P0

无。特别说明：唯一会移动正式库 `/AV` 的操作是 `av_auto promote`（显式人工命令，默认不自动调用）；其回滚逻辑有缺口（见 F-P1-05），但只在失败路径且会打印"回滚失败/片子可能已在 dst"，不满足 P0 的确定数据损坏标准。

### 3.2 P1

#### F-P1-01 `av_review` 在 Python 3.11 下无法运行 review/daily，仓库与部署文件均未声明 Python ≥3.12

- **证据**
  - `python3 -m py_compile av_review/vision.py`（容器 Python 3.11.2）失败：`SyntaxError: f-string expression part cannot include a backslash`，位置 `av_review/vision.py:561` 与 `:566`：
    `f"| {rec['code']} | {(rec.get('actress') or '').replace('|','\\|')} | ..."`
  - 该语法（PEP 701 允许 f-string 表达式内反斜杠）需要 **Python 3.12+**；3.11/3.10 会在解析期直接报错。
  - `python3 -c "import av_review.vision"` 复现 SyntaxError；`av_review/cli.py:127` 的 `cmd_review`、`:187` 的 `cmd_daily` 都在函数内 `from av_review.vision import ...`，因此 **`av_review review` / `av_review daily` 在 3.11 上直接 traceback**（`--help`、`health`、`catalog` 仍可跑，因为未 import vision）。
  - 部署端没有版本约束：`README.md:3-14`、`systemd/av-review-api.service:10`、`bin/av_review-api-start.sh:10` 都写 `/usr/bin/python3`，`cron/av-review.cron:12` 直接执行 `bin/av_review daily`；仓库没有 `requirements.txt`/`pyproject.toml`/CI 做 3.12 断言，测试也 0 次 import `av_review`（见 F-P2-11）。
- **影响**：若 grokbot-vm 的 `/usr/bin/python3` 是 3.10/3.11（Ubuntu 22.04 默认 3.10），cron daily、systemd API 的 `/review` `/daily` 全部不可用；本审计容器 3.11 已确定复现。这是"正式入口"级故障，且因为没有 CI/测试，改坏 3.12 语法不会被发现。
- **建议修复**：把 `\\|` 移出 f-string（如 `cell = str(...).replace("|", "\\|")` 先算好再插入，或用 `chr(92)`）；README/服务文件显式写 Python ≥3.12（或改回 3.11 兼容写法）；在 `tests/` 加 `import av_review.vision` 冒烟用例；CI 用目标 Python 版本跑 py_compile/import。
- **验证方式**：目标 Python 下 `python3 -m py_compile av_review/vision.py` 通过；`python3 bin/av_review review --dry-run --limit 0` 能进到业务逻辑而不是 SyntaxError。

#### F-P1-02 并发/忙闲保护失效：`VisionLock` 是死代码，`BUSY_PATTERNS` 漏掉 cron 实际使用的 `bin/av_review daily`，API 重活启动还有 TOCTOU

- **证据**
  - `av_review/busy.py:65` 定义了 `VisionLock`（基于 `run/vision.lock` 的 pid 锁），但全仓库 **没有任何地方 acquire**（`grep -rn VisionLock` 只有定义处）；`run_review`（`av_review/vision.py:615`）与 `run_catalog_judge`（`av_review/catalog.py:596`）都没有加锁。
  - 忙闲检测只靠 `BUSY_PATTERNS`（`av_review/constants.py:75-86`）。用真实命令行核对：
    - `/usr/bin/python3 /home/yuanhao/av-review/bin/av_review daily --skip-if-busy` → 所有 pattern 都**不匹配**（`"av_review.*review"` 需要 av_review 之后再次出现 review；daily 没有）。
    - `/usr/bin/python3 /home/yuanhao/av-review/bin/av_review review ...` → 恰好匹配 `"av_review.*review"`（因为子命令名本身是 review）。
    - `/usr/bin/python3 /home/yuanhao/av-review/bin/av_review catalog --judge` → 不匹配。
    - `"python3 -m av_review daily"` 只匹配 `-m av_review` 调用方式，而 `bin/av_review` 是直接执行脚本，不是 `-m`。
  - `cron/av-review.cron:12` 正是 `bin/av_review daily --skip-if-busy`；`av_review/cli.py:187-204` 的 `cmd_daily` 用 `vision_busy()` 决定 `skip_heavy`，README:56 承诺"若当时有识图在跑，则跳过识图"。
  - `av_review/api.py:143-151`：API 的 `/review` 重活在 `_spawn_or_run` 里 `vision_busy()` 检查后立刻 `Popen`（`:201`），两个并发 POST 都能在对方注册进 pgrep 前通过检查（TOCTOU）；且它同样漏判 cron daily。
  - 共享工作目录是固定路径：`av_review/vision.py:76-78` `IMG/FR/OUT = WORK/images`、`WORK/frames`、`WORK/out`，`WORK=/tmp/av-review-work`（`constants.py:34`）。两个 run 并发会互相覆盖同名 `images/<code>.jpg`、`frames/<code>_<label>.jpg`。
- **影响**：cron daily 与 API `/review` `/daily`、手工 review 可以同时跑；双重 Magpie 费用、共享中间帧/图片互相覆盖（可能把 A 的帧送给 B 的终审）、输出结果混写。`--skip-if-busy` 的文档承诺在 cron 这条主路径上不成立。
- **建议修复**：`run_review`/`run_catalog_judge`/`cmd_daily` 用 `VisionLock`（或 `fcntl.flock`）做真正的互斥，拿不到锁直接退出 0/1；`BUSY_PATTERNS` 补 `"bin/av_review (review|daily|catalog)"`、`"av_review daily"`；API 侧在拿锁之后才 Popen，或让被 spawn 的子进程自己拿锁；把共享中间目录改成带 run-id 的子目录。
- **验证方式**：单测用 fake `pgrep` 输出真实 cmdline（可复制本报告 1.4 的清单）断言 `vision_busy()["busy"]` 为真；两个 fake run 并发时第二个返回 busy/lock 失败；API 两并发 POST 只产生 1 个 Popen。

#### F-P1-03 fMP4 解析/索引/HTTP 层在本环境 0 覆盖；现有 fmp4 测试全部绑死 ffmpeg，且用 `setUpClass` 环境错误而不是 skip

- **证据**
  - 1.2 的 trace 数据：`fmp4_http.py` 48 个函数只进入 6 个，语句行 6.4%；`BoundedHttpFile`、`parse_moov`、`parse_moof_samples`、`parse_sidx`、`_find_mfra`、`build_fmp4_index`、`_scan_fragments`、`_realize_moof`、`_pick_segment`、`_segment_pair`、`_keep_gop`、`_load_sample_payload`、`build_moof_mdat`、`_append_segment`、`materialize_snippet`、`open_fmp4_session` 全部未执行。
  - `tests/test_fmp4_http.py:42-68`：所有 fixture 都用 `subprocess.check_call(["ffmpeg", ...])` 生成；`IndexTests.setUpClass`（`:131-138`）与 `HttpExtractTests.setUpClass`（`:175-186`）在无 ffmpeg 的机器上是 error（实测 2 errors），不是 skip。
  - 现有 8 个 fmp4 测试中 6 个依赖 ffmpeg；真正不依赖 ffmpeg 的只有 `BoxTests` 2 个（`test_tfra_offset_past_2gib_stays_unsigned`、`test_content_range_total`）。**解析层本身是纯 Python，本报告 1.4 已用合成 box 在无 ffmpeg 环境验证了 scan/sidx/mfra、绝对 base 重建、64 位 box、tfra、Content-Range 等 25 项——说明这部分完全可测但没测。**
  - 缺 ffmpeg 时 6 个用例连存在感都没有；即使有 ffmpeg，现有测试也只是"整条链路能出帧"的集成测试，没有针对 malformed sidx/moof、单 mdat、moof 后夹 free/styp、cap 边界等失败/边界分支的用例（见第 4 节清单）。
- **影响**：`fmp4_http.py` 是整条链路里最容易出边界 bug 的模块（本次就靠合成样本发现了 3 个），但没有单元测试保护；CI/新开发机缺 ffmpeg 时套件直接红，掩盖其它真实失败。
- **建议修复**：新增 `tests/test_fmp4_parse.py`（纯合成 box，无 ffmpeg）与 `tests/test_fmp4_http_local.py`（本地 127.0.0.1 HTTP server，验证 redirect/cookie/闭合 Range，本报告 `/tmp/fmp4_http_probe.py` 可直接改写搬入）；把依赖 ffmpeg 的类改成 `@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")` 或模块级 `skipUnless`。
- **验证方式**：无 ffmpeg 的干净环境跑 `python3 -m unittest discover -s tests` 应为 OK（带 skipped），新增解析/HTTP 用例全部通过；故意改坏 `_append_segment`/`parse_sidx` 断言测试能红。

#### F-P1-04 部署副本三/四处，实际执行的是 moviepilot 容器内 `/config/scripts/av_auto.py`；仓库没有同步脚本、版本标识或校验，`fmp4_http.py` 与插件目录是隐式依赖

- **证据**
  - `bin/av_auto:215`（无 --url 路径）与 `:251`（--url heredoc 路径）都是 `docker exec ... moviepilot python3 /config/scripts/av_auto.py ...`；`bin/av_auto.README.md:13-15` 明确"实际脚本: moviepilot 容器内 `/config/scripts/av_auto.py`"。
  - 顶层 `README.md:3-4` 只列了 `grokbot-vm /home/yuanhao/av-review` 与 Unraid `/mnt/user/appdata/findav/bin/av-review/`、`/home/yuanhao/agent-xfer/av-review/` 三个"副本"，**没有写哪个是 source of truth、怎么同步到 `/config/scripts`、也没有提 `/config/scripts/fmp4_http.py` 必须同时在位**（`av_auto.py:44-47` 会从脚本目录 import `fmp4_http`；`av_auto.py:49` 还依赖 `/config/plugins/av115scraper`）。
  - 全仓库没有任何把仓库代码同步到 `/config/scripts` 的脚本（`av_review/util.py:53-63` 的 `scp_to/scp_from` 只用于 secrets/结果文件）；`bin/av_auto` 也没有 `--version`/commit 输出，无法确认远端执行的是不是仓库这份。
  - `bin/av_auto:310` 依赖 Unraid 上的 `/mnt/user/appdata/findav/bin/av_auto_apply_fs`，但该脚本不在仓库内（全仓库只有引用），属于未声明/未纳管的外部依赖。
- **影响**：改仓库 `scripts/av_auto.py` 不会自动生效；运行时跑的可能是旧副本，排障/回归时无法对齐版本；只同步 `av_auto.py` 而漏 `fmp4_http.py`/插件目录会 ImportError。三处副本长期漂移风险高。
- **建议修复**：加一个最小 `bin/av_deploy`（`scp` 两个脚本到 `/mnt/user/appdata/moviepilot/config/scripts/`、写入 `git rev-parse HEAD` 到 `av-auto-fs/VERSION`、可选 ssh 校验 sha），`av_auto --version` 打印本地+远端版本；README 写明 source of truth、同步顺序与 `av_auto_apply_fs`/插件目录前提；把 `av_auto_apply_fs` 纳入仓库或明确列出其维护位置。
- **验证方式**：部署后 `av_auto --version` 的 sha 与仓库 HEAD 一致；删掉 `fmp4_http.py` 时部署脚本应报错；在容器里 `python3 -c "import fmp4_http"` 有明确失败信息。

#### F-P1-05 正式库 `promote` 路径 0 测试，且回滚不完整（已写出的正式 strm 不撤销、无备份）

- **证据**
  - trace：`_promote_one`（`scripts/av_auto.py:2191`）、`cmd_promote`（`:2268`）、`_emby_refresh`（`:2151`）全部 **未进入**；测试里只有 `mock.patch.object(av_auto, "cmd_promote", side_effect=AssertionError(...))` 这类"确认没被调用"的断言（`tests/test_av_auto_process.py:738-757`），即"不自动 promote"有测试，promote 本身没有。
  - 回滚实现 `av_auto.py:2233-2242`：`_rollback` 只把 115 上的整夹 move 回 `/av-auto`；对已经 `media_mkdir(dest)`/`_write_strm_dir(...)` 写进正式 strm 目录的文件没有删除/恢复逻辑，`finally` 也没有清理。若 `_write_strm_dir` 写了 2 个文件后异常，正式目录会留下部分 strm + 回滚后的 115 夹（文案只说"staging strm 保留"，没提已写正式 strm）。
  - `_write_strm_dir`（`:774-789`）直接 `media_write_text` 覆盖同名 strm，无备份/临时文件+rename。
  - 步骤 3 Emby 刷新失败明确不回滚（`:2255-2260`，合理），但步骤 4 会 `media_rmtree(staging_dir)`（`:2263-2264`），所以"115 回滚了但 staging strm 被清"与"115 在正式库但 strm 只写了一部分"这两种半状态都会出现。
- **影响**：`promote` 是唯一会改正式库 `/AV` 与正式 strm 的操作，失败路径没有测试保护；半写状态需要人工判断和清理，且没有备份/审计文件名。
- **建议修复**：`_write_strm_dir` 先写 `<name>.strm.tmp` 再 `replace`，并记录写入清单；`_rollback` 删除本次新写入的正式 strm（只删本次清单内、且不是既有文件的）；tests 用 fake `Pan115`（记录 `dir_id/move`）+ 临时目录覆盖 dry-run / move 失败 / 搬家后核验失败 / strm 空 / strm 异常 / Emby 异常 / 回滚 move 失败 7 条分支。
- **验证方式**：新增 `tests/test_av_auto_promote.py` 后 trace 进入 `_promote_one`；构造写 strm 第 2 个文件抛错，断言正式目录无新增文件且 115 夹回到 `/av-auto`。

### 3.3 P2

#### F-P2-01 `sidx` 子段以 `styp` 开头时 materialize 硬失败，且没有退回 scan 索引

- **证据**：`_append_segment`（`scripts/fmp4_http.py:1094-1099`）读取 `seg.start` 后只接受 `moof` 开头，否则 `raise Fmp4RangeError("fragment does not start with moof")`；而 DASH/CMAF 的子段常以 `styp` 开头。实测（`/tmp/fmp4_styp.py`）：同一份文件
  - 带 sidx → `build_fmp4_index` 成功（`source=sidx`），`materialize_snippet` 抛 `Fmp4RangeError: fragment does not start with moof`；
  - 去掉 sidx → `source=scan`，`materialize_snippet` 成功（568 字节）。
  即"有 sidx"反而比"没有 sidx"更容易失败。`_scan_fragments`（`:875-899`）本来能跳过 `styp`，但 sidx 路径没走到它。
- **影响**：若 115/上游给的是带 `styp` 的 fMP4，`av_auto frames` 会 `open_fmp4_session` 成功、取片段失败，回退到 ffmpeg 直连又会 `partial file`；该时间点被降级丢弃，最多丢点不整批失败（`cmd_frames` 逐点降级），但会显著降低有效帧数。
- **建议修复**：`_append_segment` 先 `iter_boxes` 跳过 `styp` 等前置 box 找到 moof（或 `_segment_pair` 里 sidx anchor 向后对齐到 moof）；更稳妥的是 `build_fmp4_index` 在 sidx 取片段失败时，用 `_scan_fragments` 重建索引再试一次。
- **验证方式**：把 `/tmp/fmp4_styp.py` 场景做成单测，断言 styp 前缀时 `materialize_snippet` 成功且返回 init+moof+mdat。

#### F-P2-02 `moof` 与 `mdat` 之间夹 `free/styp/sidx` 时，片段被截到 moof 末尾且不报错（静默产出无 sample 的 snippet）

- **证据**：`_realize_moof`（`scripts/fmp4_http.py:915-921`）取 `end = moof_off + moof_size`，只有紧邻的下一个 box 是 `mdat` 才扩展 `end`；否则 Segment 只覆盖 moof。`_append_segment`（`:1103-1104`）的快路径在 `_moof_is_relative` 时直接 `remote.read(seg.start, span)` 返回——不会校验 mdat 是否在 span 内。实测（`/tmp/fmp4_synth.py` T5a）：`init + moof + free + mdat` 的 `materialize_snippet` 返回 init+moof（540 字节），**不含任何 mdat 字节**，也不抛错。
- **影响**：ISO BMFF 允许 moof 与 mdat 之间插 `free`/`sidx` 等 box（部分 muxer/转封装会这么做）。这类文件 fMP4 路径会产出 ffmpeg 解不出帧的 snippet，最终该点降级、无明确原因（错误只会是 ffmpeg 的 `Invalid data`）。
- **建议修复**：`_realize_moof` 在 `next_off` 处向后跳过 `free/sidx/styp` 等非 mdat box 再决定 end；或 `_append_segment` 校验「span 内必须包含 mdat 且 sample 字节范围都在 span 内」，否则走重建路径/报明确错误。
- **验证方式**：单测 `init+moof+free+mdat`，断言 snippet 含 mdat 且 `_extract_fmp4_frame`（mock ffmpeg）能拿到 sample 字节；当前代码会失败。

#### F-P2-03 `prev+cur` 双片容量检查自相矛盾：每片 ≤max_span 才合并，但最终要求合计 ≤max_span+init

- **证据**：`materialize_snippet`（`scripts/fmp4_http.py:1133-1143`）先用 `(prev.end-prev.start) <= max_span and (seg.end-seg.start) <= max_span` 允许合并两片，随后 `if len(blob) > max_span + len(init): raise "local snippet exceeds the fragment cap"`。两片各接近 max_span 时必然触发。实测：
  - `/tmp/fmp4_synth.py` T2e：两片合计 690 > max_span(149)+init(420) → raise；
  - `/tmp/av_auto_probe.py` C1：**默认 max_span=32MB、两片各 16MB、第二片 sidx SAP=0** → `Fmp4RangeError: local snippet exceeds the fragment cap`（每片都 ≤32MB，但 32MB+init+moof 超过阈值）。
- **影响**：非 sync 起点的分会话（sidx SAP=0 或 mfra/scan 判断首 sample 非 keyframe）遇到两片都较大时直接失败；`_extract_fmp4_frame` 不重试，附近时间点同样失败 → 该点丢帧。当前 115 常见 ffmpeg 分片较小，触发概率低，但默认 cap 下 ≥16MB 分片就会中招。
- **建议修复**：把前置条件改成 `prev_span + seg_span <= max_span`（与最终检查一致），或最终检查放宽到 `max_span*2 + init`，或超限时降级为只取当前片并在日志里说明（当前片无 keyframe 时再报错）。
- **验证方式**：单测构造两片各 ~cap/2+1，断言要么成功且 ≤cap，要么降级行为明确；默认 32MB 场景（C1）不再抛错。

#### F-P2-04 无 sidx/mfra 的文件要线性扫全片建立索引，且 Range 请求无 pacing/backoff

- **证据**：`build_fmp4_index`（`fmp4_http.py:789-872`）在 `info["has_mvex"] or saw_moof` 但没有 sidx/mfra 时走 `_scan_fragments`（`:875-899`）；它从 offset 0 顺序读每个 box 头（16 字节）+ 每个 moof（`_realize_moof` 再读 moof 和下一个 box 头），才得到全部 Segment。按 2 小时、每片段 2s 估算约 3600 个片段 ≈ 7000+ 次闭合 Range 请求，**且 `BoundedHttpFile` 没有任何请求间隔/重试退避**（对比插件 115 API 有 `_GAP_SECONDS=2.5` 的 pacing）。README/`bin/av_auto.README.md:70` 只写了"不整文件下载"，没有写无索引文件的线性扫描代价。
- **影响**：`frames` 打开 session 时可能先花数分钟到数十分钟做索引；高频 Range 也可能触发 CDN 限流/405。对 ffmpeg 默认带 mfra 的文件没影响（2~3 次请求即可），对其它 muxer 生成的文件影响大。**真实 115 文件的 mfra/sidx 情况待确认。**
- **建议修复**：`_scan_fragments` 加小步长 pacing（可配置，如 50ms）与 429/405 退避；对超过 N 个片段仍未扫完的文件尽早放弃并给出明确错误；README 补索引代价说明。
- **验证方式**：fake remote 统计请求数/时间；在无 mfra 的 100 片段样本上断言请求数与 pacing；真机对一个无 mfra 的 115 文件计时。

#### F-P2-05 测试卫生：ffmpeg 环境错误而非 skip；fixture 缓存在固定 `/tmp/fmp4-http-test` 且不校验参数

- **证据**
  - `tests/test_fmp4_http.py:42-68` `_ffmpeg_mp4` 用 `subprocess.check_call`，缺 ffmpeg 时 `FileNotFoundError` 在 `setUpClass` 抛出 → 套件 `FAILED (errors=2)` 而不是 skipped（实测）。
  - `tests/test_fmp4_http.py:22` `FIXTURE = Path("/tmp/fmp4-http-test")`；`_ffmpeg_mp4` 在 `dest.is_file() and dest.stat().st_size > 0` 时直接复用，不校验 flags/ffmpeg 版本/内容 → 改了 `-movflags`（例如 mfra → sidx 用例）后旧 fixture 仍被复用，测试可能测到旧文件；固定路径也导致并行/多用户冲突，且从不清洗。
- **影响**：无 ffmpeg 机器上测试套件红，掩盖真实失败；fixture 复用可能让"改了 flags 的测试"实际没生效。
- **建议修复**：`@unittest.skipUnless(shutil.which("ffmpeg"), ...)`；fixture 放 `tempfile.TemporaryDirectory` 或至少把 flags/mtime 写进旁路 manifest 校验，不匹配就重建；CI 分"无 ffmpeg（skip）"和"有 ffmpeg（全跑）"两档。
- **验证方式**：`PATH=/usr/bin` 且无 ffmpeg 时套件 OK(skipped=6+)；改 flags 后运行日志显示重建了 fixture。

#### F-P2-06 cron/systemd 部署可靠性：日志目录未建、双启动机制、无安装说明、TZ 依赖未校验

- **证据**
  - `cron/av-review.cron:12`：`30 19 * * * /home/yuanhao/av-review/bin/av_review daily --skip-if-busy >> /home/yuanhao/av-review/logs/daily.log 2>&1`。`logs/` 目录在仓库中不存在（`.gitignore` 忽略 `logs/`），只有 `bin/av_review-api-start.sh:4` `mkdir -p logs run` 会创建；全新部署在第一次 reboot/运行 API 之前，cron 的 `>>` 会失败（shell 重定向错误），daily 不会执行。README:64 只说"日志：.../logs/daily.log"。
  - `cron/av-review.cron:13` `@reboot ... av_review-api-start.sh` 与 `systemd/av-review-api.service` 同时启用两套 API 启动方式；README:87 说"systemd 用户服务……也可用 crontab @reboot"，两条都默认写在仓库里，没有说明选一/如何安装（`systemctl --user enable --now`、`loginctl enable-linger` 均未写）。
  - systemd 与 start.sh 的日志去处不同（journal vs `logs/api.log`），README 未说明；`logs/api.log` 无轮转。
  - `cron` 里注释"本机已是 CST"（README:50），但代码 `constants.py:8` 用 `ZoneInfo("Asia/Shanghai")`；若宿主机 TZ 不是 CST，19:30 的 cron 语义与文档不一致，仓库没有校验。
- **影响**：新机器部署 first-run 静默失败、API 可能被拉起两次（systemd 反复重启 + @reboot 抢端口时 systemd `Restart=on-failure` 会 5s 循环）、日志分散/无轮转。
- **建议修复**：cron 命令前加 `mkdir -p .../logs`（或统一由 wrapper 建）；README 写清"二选一"与完整安装/linger 命令；决定 API 日志单一路径并加 logrotate 建议；cron 显式 `CRON_TZ=Asia/Shanghai` 或部署时校验 `date`。
- **验证方式**：空 `logs/` 目录模拟首次部署跑 cron 行；`systemctl --user status` 与 `pgrep` 只出现一个 API 进程。

#### F-P2-07 LAN API 暴露面：`/health` 无鉴权却每次 ssh 到 Unraid 并返回进程命令行/路由指纹；0.0.0.0 明文 HTTP、无速率限制

- **证据**
  - `av_review/api.py:101-108`：`GET /health`、`/status` **不走 `_auth`**，直接 `_health()`。
  - `av_review/cli.py:104-126`：`_health()` 每次调用都 `ssh(... stat/md5sum ...)` 到 `192.168.1.9`，并把 `vision_busy()` 结果（`busy.py:36-63` 的 `processes` 是 `pgrep -af` 的完整命令行）和 route mtime/md5 一起返回。未授权 LAN 请求可反复触发 ssh 子进程（ThreadingHTTPServer 无并发/速率限制，`:216`）。
  - `README.md:70-71`：默认监听 `0.0.0.0:18765`、Token 放请求头；明文 HTTP，LAN 内可嗅探；`api.py:18-24` 的 `ensure_token` 只在文件不存在时 `chmod 600`，已存在的宽松权限不会被修正。
  - `api.py:45-56` `_body` 无 Content-Length 上限；`:169-171` inline `subprocess.run(timeout=180)` 的 `TimeoutExpired` 未捕获（超时会直接断连接而不是 JSON 错误）。
- **影响**：未认证即可让 grokbot-vm 反复 ssh Unraid（放大器/DoS）、读到其它识图进程的命令行（可能含番号/路径）；token 在 LAN 明文可嗅探；`/health` 高频调用会拖慢或拖垮 API 线程。
- **建议修复**：`/health` 去掉 `ssh`/`vision_busy` 的昂贵调用（拆成 `/health` 轻量与鉴权的 `/status` 详细版）；对 `/health` 做简单限速/缓存；token 文件启动时强制 `chmod 600`；README 注明只绑内网/建议反代 TLS；`_body` 限制长度；捕获 inline 超时返回 504 JSON。
- **验证方式**：未带 token 连续 100 次 `/health` 不应产生 100 个 `ssh`/`pgrep` 子进程（当前会）；`stat -c %a secrets/api.token` 手工改成 644 后重启应被修正为 600。

#### F-P2-08 密钥缓存策略不一致：`av_review` 把密钥长期缓存到 `secrets/cache/`（不清理），`av_auto` 用一次性 runtime 文件（已测试）

- **证据**：`av_review/secrets.py:30-45` `refresh_secrets()` 每次 `scp_from` Magpie `caller-keys.json`、`findav/emby.env`、`connection.env` 到 `HOME/secrets/cache/` 并 `chmod 600`，但没有任何清理逻辑（`grep -n "unlink\|rmtree" av_review/secrets.py` 为空），一次运行后密钥副本长期留在 grokbot-vm。对比 `bin/av_auto:255-299` + `cleanup_llm`：每次运行生成 `llm_runtime.<run-id>.json`，`trap EXIT` 删除（`bin/av_auto:180-186`），且 `tests/test_av_auto_recover.py:466-507` 有并发/清理测试。
  - `README.md:6` 只写"Key 只从 Magpie caller-keys.json 与 findav emby.env / connection.env 读取"，没写会落盘缓存。
- **影响**：grokbot-vm 的 `~yuanhao/av-review/secrets/cache/` 成为长期密钥副本；备份/快照/误操作更容易泄漏。与 av_auto 的"一次性"策略不一致。
- **建议修复**：`refresh_secrets` 用临时目录 + 运行结束删除，或加 `--keep-secrets` 显式保留；README 说明缓存位置与保留策略；`secrets/` 已在 `.gitignore`（好）。
- **验证方式**：跑完 `av_review review --dry-run` 后 `find secrets/cache -type f` 不应留下 key 文件（当前会）。

#### F-P2-09 `bin/av_review_ff_batch.sh` 的 URL/日志卫生与容器临时文件：与文件头"Never echo source URLs"承诺不完全一致

- **证据**：`bin/av_review_ff_batch.sh` 把 `$url` 直接作为 `docker exec embyserver ffprobe/ffmpeg ... "$url"` 的参数（` :24`、`:38`），URL 会出现在容器内进程 argv（`ps` 可见，115 签名直链短时有效）；ffmpeg/ffprobe 的 stderr 被重定向到 `$OUTROOT/logs/*.err`（`:24`、`:38`）且没有像 `av_auto._redact_media` 那样的脱敏，ffmpeg 错误信息可能带出输入 URL；`docker exec embyserver mkdir -p /tmp/av-review-frames`（`:10`）创建的文件在 `docker cp`（`:39`）后不清理，容器 `/tmp` 累积；脚本没有并发锁、没有 `set -e`、依赖 `jq` 未在文档声明。
- **影响**：与"never echo source URLs"的注释承诺有差距；日志目录可能长期保存签名 URL；容器临时目录无界增长；两个 batch 并行会互相覆盖 `OUTROOT` 同名文件。
- **建议修复**：日志写入前统一过 `_redact_media`（或用 `sed -E 's#https?://[^ ]+#http(s)://REDACTED#g'`）；docker 内临时文件在 `docker cp` 后删除（`docker exec rm -f`）；加 `flock`/run-id 目录；README 说明 `jq`/`embyserver` 前提。
- **验证方式**：用 fake `docker`/`jq`/`ffmpeg` 跑脚本，断言 stdout/stderr 与日志中不出现完整 URL；运行后容器 `/tmp/av-review-frames` 为空。

#### F-P2-10 工程性：无 CI、无依赖声明、无 Python 版本约束；`av_review` 依赖外部 `ssh/scp/docker/mariadb`，`av_auto` 依赖 p115client+httpx+插件目录

- **证据**：`git ls-files` 共 27 个文件，没有 `requirements*.txt`/`pyproject.toml`/`.github/`；`grep -rn "^import "` 显示 `av_review` 基本纯标准库（sqlite/urllib），`scripts/av_auto.py:621/875/2152` 惰性 import `p115client`/`httpx`；`av_auto.py:49` 写死 `/config/plugins/av115scraper`（运行需要 moviepilot 里已安装该插件）；`av_review/util.py:43/53/59` 用 `ssh`/`scp`，`catalog.py` 用 `docker exec ... mariadb`。README 没有一节说明"运行前提/依赖"。
- **影响**：换机器/新容器无法从仓库直接复现；缺 `p115client` 时 `submit/wait/clean` 在 115 侧操作失败（`_p115()` 或 `Pan115.add_magnet` 处），缺 `ssh`/`docker` 时 av_review 全挂；没有 CI 兜底。
- **建议修复**：加 `requirements.txt`（av_auto: `httpx`；若插件仓库仍不声明 `p115client`，在 README 显式列"需先安装 Av115Scraper 插件，由它带入 p115client"），README 加"运行前提"与"目标 Python 版本"；加最小 GitHub Actions：`python -m compileall`、`python -m unittest discover -s tests`、`bash -n`、可选 `shellcheck`。
- **验证方式**：干净容器按 README 安装后跑通 `python3 -m unittest`；CI 在无 secrets 的 runner 上绿。

#### F-P2-11 `av_review` 包（约 2814 行，README 称"正式入口"）0 测试

- **证据**：`grep -rln "av_review" tests/` 为空；4 个测试文件只覆盖 `scripts/av_auto.py`/`scripts/fmp4_http.py` 与 bash wrapper。`av_review/` 共 `api.py 223`、`busy.py 97`、`catalog.py 859`、`cli.py 240`、`constants.py 114`、`cost.py 144`、`inventory.py 126`、`secrets.py 66`、`util.py 235`、`vision.py 705`。其中 `cost.compute_cost`、`util.extract_code_from_text/norm_code/redact`、`inventory.plan_review`、`api.Handler` 路由/鉴权、`busy.vision_busy` 都是可离线单测的纯函数/类（本报告 F-P1-01/02 就靠它们找到问题）。
- **影响**：README 主入口没有任何回归保护；本次 Python 3.11 SyntaxError、busy 漏判都是"一测就现形"的问题，却因 0 测试长期存在。
- **建议修复**：至少补 `test_av_review_smoke.py`（import 全部模块、py_compile 目标版本）、`test_av_review_busy.py`（fake pgrep）、`test_av_review_api.py`（Handler 鉴权/方法/坏 body）、`test_av_review_util.py`（redact/norm_code/publish_pair 用 tempdir/mock scp）。见第 6 节清单。
- **验证方式**：`python3 -m unittest discover -s tests` 的 `Ran` 数增加且覆盖上述模块。

#### F-P2-12 `process` 在 wrapper 层无条件要求 Magpie key 复制成功；除 `share-floor` 外所有命令（含 `--help`）都先 ssh 探测/最后 apply_fs

- **证据**：`bin/av_auto:160-162` 把 `process` 归入 `need_llm=1`，随后 `:283-299` 先 `ssh install -m 600 caller-keys.json → llm_runtime.<run>.json`，失败则 `rc!=0` → `:301` 直接跳过 `run_python_remote`。但 `_process_offline_title`（`av_auto.py:2882-2909`）在"全部步骤已 skip"时完全不需要 LLM。另 `:264` 对所有命令（包括 `--help`）先跑 `load_share_floor_probe`（一次 ssh），`:308-315` 结束后再 ssh 跑 `av_auto_apply_fs`。
- **影响**：Magpie caller-keys 文件缺失/权限问题时，即使只是重跑一个已完成的 `process`（或想看帮助），也会失败；所有命令依赖 Unraid 可达（`--help` 也慢）。
- **建议修复**：`need_llm` 改为"实际要跑 frames/review/grok 时才注入"（例如 process 先 dry 判断，或允许 `AV_AUTO_ALLOW_NO_KEY=1` 时无 key 跑只做 clean/strm 的路径）；`--help`/纯本地命令在 wrapper 里直接转发、不探测；文档说明。
- **验证方式**：PATH 里假 ssh 让 `install` 失败，跑一个"已全部完成"的 `process` 应成功（当前会失败）；`time av_auto --help` 不应有 ssh 延迟。

#### F-P2-13（待确认）`cmd_clean` 会把 size=0 的正片当垃圾删除

- **证据**：`scripts/av_auto.py:636-649` `_is_keeper` 里 `if not size and min_bytes > 0: return False`；`cmd_clean`（`:2441-2481`）只把 `keepers` 搬到 `/av-auto/<code>/`，随后对 inbox 内**所有**剩余文件/子目录 `fs_delete`（`:2462-2477`）。因此只要 inbox 里存在至少一个 size 非 0 的 keeper，另一个 size=0 的视频文件就会被当垃圾删掉。
- **待确认**：115 App/aps 列表是否会对正常视频返回 `size=0`（`av115_pan._size` 取 `fs/s/size/file_size`，正常应有值；但风控/部分字段缺失时可能为 0）。若可能，则是 staging 内误删正片；若不可能，仅防御性问题。
- **建议修复**：`size` 未知（0）时不要判 junk，而是保留并在日志列出（或按扩展名再给一次机会）；`fs_delete` 前只删除明确判定为 junk 的项。
- **验证方式**：fake pan 让一个文件 size=0 + 一个正常 keeper，断言 size=0 文件不被 `fs_delete`（当前会被删）。

---

## 4. 测试覆盖与缺口（专节）

### 4.1 每个测试文件覆盖了什么

| 文件 | 用例数 | 覆盖内容 | 主要缺口 |
| --- | --- | --- | --- |
| `tests/test_fmp4_http.py` | 8 | `BoxTests` 2 个离线：tfra 2GiB 偏移、Content-Range；`IndexTests`/`HttpExtractTests` 用 ffmpeg 生成 mfra/sidx/scan/absolute/progressive 文件，断言 progressive→None、三种索引、absolute 重建路径、闭合 Range 拒绝 open-ended、两帧 PSNR、忽略 Range 拒绝整文件 | **本环境 0 个解析测试实际执行**（2 errors）；无合成 box 单测；无 sidx/moof/mfra 畸形、styp、free 夹层、cap 边界、redirect 链上限、cookie 丢失等单测；HTTP 层本来可以完全离线测（1.4 已证） |
| `tests/test_av_auto_process.py` | 23 | 番号当子命令的 argv 顺序错误（Python+shell）、`process` 的 clean→strm→frames→review→grok 编排（mock 掉 115/ffmpeg/Magpie）、已做步骤 skip、缺番号不拖累其它、无效帧不算完成、持久化失败不算成功、容器 `/tmp` 结果先复制再报成功、candidate 目录优先级、shell/python 正则一致 | `cmd_clean/cmd_strm/cmd_submit/cmd_wait` 真实实现全 mock；`_promote_one` 完全没测（F-P1-05）；`_ffmpeg_one_frame/_extract_one/_snippet_seek/_extract_fmp4_frame` 未进入；`_precheck_image/_chat_retry/_post_magpie_chat` 未进入；wrapper 的 key-copy 失败路径未测 |
| `tests/test_av_auto_recover.py` | 19 | ENOSPC 四种判定（full/reservation/stale_floor/not_full/unknown）、pending 队列格式与 apply、media_write ENOSPC 文案、pending strm 不报缺失、process 不重复排队、`apply_fs` 即使在 python 非 0 也执行、`apply_fs` 失败上报、并发 official run 各自 llm_runtime 文件、share-floor 只读、wrapper 不 reload shfs/不写 key、ed2k URL 不拆 argv、docker 失败仍 apply、双 --url 在 ssh 前拒绝 | `media_mkdir/media_rmtree` ENOSPC 路径未进入（trace）；`_queue_fs` 碰撞重试、未知 kind、非 /media 路径仅部分；wrapper 的 key-copy 失败、ssh 超时、trap 在信号下的行为未测；`av_auto_apply_fs` 用 fake ssh 代表 |
| `tests/test_grok_tiebreak.py` | 16 | trigger 矩阵（agree/disagree/partial/uncertain）、Grok pass/fail 保留、uncertain 保持未决、hook 只在未决时给 argv、prior 行过滤 grok/minimax、只选有效已抽帧（排除封面/剧照/Thumb/Backdrop）、默认 review 仍是双模型、默认 chat 拒绝 grok/minimax、runtime 禁用模型、swap 常量被拒、已 settled 不调 Magpie、写 `grok_review.json` 不覆盖 `review.json`、Grok uncertain 记 unresolved、密钥回显时拒绝写入、code 不一致/缺 review 报错 | `_post_magpie_chat` 网络层（HTTP 错误/超时/重试）未测；`_grok_tiebreak_chat` 重试分支部分；真实 Magpie 协议无契约测试 |
| `av_review/` 包 | **0** | — | 全部（F-P2-11） |

### 4.2 关键未覆盖分支（file:line）

- fMP4：`parse_sidx` 的 truncated/hierarchical（`fmp4_http.py:618-667`，只有 1 个 tfra 和 Content-Range 用例离线跑）；`parse_moof_samples` 的 tfhd/trun 各种 flag 组合与 v0/v1 混用（`:488-615`）；`_find_mfra` 的尾随 box/错误 mfro（`:723-754`）；`_scan_fragments` 的 `moof` 超过 cap、超大 `free/mdat`（`:875-899`）；`_realize_moof` 的 next box 非 mdat（`:902-936`）；`_keep_gop` 的 keyframe 在片段中间/尾部（`:996-1013`）；`_load_sample_payload` 的多段/裁剪（`:1016-1042`）；`_append_segment` 的重建路径（`:1090-1122`，只有 ffmpeg 生成的 absolute 文件覆盖）；`materialize_snippet` 的 prev 合并/cap（`:1125-1144`）；`BoundedHttpFile` 的 200/short body/total changed/redirect 环（`:148-300`）。
- av_auto：`_ffmpeg_one_frame` 超时/非 0/空文件（`av_auto.py:1256-1281`）；`_snippet_seek` 钳位（`:1284-1315`，本报告已 mock 验证，但仓库无测试）；`_extract_fmp4_frame` 失败降级/重试（`:1318-1337`）；`_extract_one` 的 partial-file 触发与 fallback（`:1340-1363`）；`_extract_one`/`cmd_frames` 里 session 与 ffmpeg 的交互（`:1436-1660`）；`_precheck_image` 解析失败/调用失败"先当有效"（`:1171-1205`）；`_chat_retry` 重试与 sleep（`:1157-1168`）；`_post_magpie_chat` 的 HTTPError/超时/JSON 解析（`:1082-1149`）；`_emby_refresh`（`:2151-2188`）；`_promote_one` 与 rollback（`:2191-2265`）；`cmd_submit/wait/clean/strm` 的 115 交互（`:2367-2512`）；`cmd_clean` 的 fs_delete 分支（`:2462-2481`）。
- av_review：全部。

### 4.3 环境错误根因与可重复性

- 2 个 error 根因：本机无 `ffmpeg`（`tests/test_fmp4_http.py:67`），`IndexTests`/`HttpExtractTests` 的 `setUpClass` 直接抛 `FileNotFoundError`；`ffprobe` 同样缺失（`_psnr`/`av_auto._ffmpeg_one_frame` 也会失败，但那些测试被 mock 或未运行）。
- 干净环境可跑性：测试只依赖 **Python 3.9+（建议按代码实际 3.11+ / av_review 3.12+）、bash、ffmpeg（6 个用例）**；无 pip 依赖、无网络依赖；wrapper 测试用 `PATH` 注入 fake `ssh`/`docker`，fmp4 HTTP 测试只监听 127.0.0.1。`av_auto` 生产路径另需 `p115client`/`httpx`/moviepilot 插件（见 F-P2-10）。
- 建议：ffmpeg 缺失应 `skip` 而不是 error；补 `requirements-dev`/CI。

---

## 5. 部署/文档一致性核对表

| # | 声明（文档/部署文件） | 实现核对 | 结论 |
| --- | --- | --- | --- |
| 1 | `README.md:3-4`：grokbot-vm `/home/yuanhao/av-review`；镜像副本 Unraid `/mnt/user/appdata/findav/bin/av-review/`、`/home/yuanhao/agent-xfer/av-review/` | `bin/av_auto:215/251` 实际执行容器内 `/config/scripts/av_auto.py`；`bin/av_auto.README.md:15` 也这么说 | ⚠️ 不一致/未写同步（F-P1-04） |
| 2 | 三处入口 `bin/av_auto`、`/mnt/user/appdata/findav/bin/av_auto`、容器 `/config/scripts/av_auto.py`（`bin/av_auto.README.md:12-15`） | 仓库只有 `bin/av_auto`（grokbot-vm 版，负责 ssh）；无 Unraid 版/同步脚本/版本号 | ⚠️ 漂移风险（F-P1-04） |
| 3 | `README.md:50-64`：cron 19:30 `daily --skip-if-busy`，日志 `logs/daily.log` | `cron/av-review.cron:12` 行一致；但 `logs/` 不在仓库、重定向前未 mkdir | ⚠️ 首次部署会失败（F-P2-06） |
| 4 | `README.md:87`：systemd 用户服务（linger）+ 也可 `@reboot` 脚本 | `systemd/av-review-api.service` 与 `cron:13` 两套都启用；无安装/linger 命令；日志去处不同 | ⚠️ 双启动/文档缺步骤（F-P2-06） |
| 5 | `README.md:68-80`：API `0.0.0.0:18765`，`/health` 免 token，POST 用 `Authorization: Bearer` | `constants.py:33-34` 端口/host 一致；`api.py:27-33` 接受 Bearer；`api.py:101-108` `/health` 免鉴权 | ✅ 功能一致；但暴露面见 F-P2-07 |
| 6 | `README.md:82-85`：`GET /health`、`POST /review|/catalog|/daily` | `api.py:101-137` 路由齐全；但 daily 的 `judge` 参数只对 heavy 分支生效（inline 时忽略） | ✅ 基本一致 |
| 7 | `README.md:6/94`：默认仅 DeepSeek-flash + gpt-6-luna，Grok 只做 tiebreak，禁止 MiniMax | `av_auto.py:75-79`、`_assert_allowed_model`、`GROK_TIEBREAK_MODEL`；`test_grok_tiebreak.py` 多项断言 | ✅ 一致且有测试 |
| 8 | `README.md:92`：不允许改 `media_llm_route.json`（期望 mtime `2026-11-10 09:05:53`） | `constants.py:25-26` md5/mtime 常量；`_health`/vision 只读+md5 比对；无写路径 | ✅ 一致 |
| 9 | `README.md:19`：默认跳过 HPET-022/MFYD-175 + mid/low/pilot/rest/grok/历史结果 | `constants.py:36-73` `HARD_SKIP`/`VISION_RESULT_PREFIXES`/`PROTECTED_BASENAMES` | ✅ 一致 |
| 10 | `README.md:56`：daily 有识图在跑就跳过识图与 judge | `cli.py:187-204` 逻辑在；但 `vision_busy()` 漏判 `bin/av_review daily` 自身（F-P1-02） | ⚠️ 承诺不成立 |
| 11 | `bin/av_auto.README.md:17-34`：各子命令 | `av_auto.py:2952-3050` `build_parser` 全部存在（setup/status/submit/wait/clean/strm/frames/review/grok-review/intake/promote/process/share-floor[wrapper]） | ✅ 一致 |
| 12 | `bin/av_auto.README.md:38-53`：process 顺序、已做步骤 skip、持久帧先写后报成功 | `av_auto.py:2882-2909`、`2808-2652`；`test_av_auto_process.py` 多例断言 persist 在 success 之前 | ✅ 一致且有测试 |
| 13 | `bin/av_auto.README.md:55-59`：ENOSPC → pending、入口退出后一定 apply_fs、每次 run 用独立 `llm_runtime.<run>` | `bin/av_auto:308-319`、`180-299`；`test_av_auto_recover.py:439-507` | ✅ 一致且有测试 |
| 14 | `bin/av_auto.README.md:68`：清杂质 ≥ min_size_mb 默认 300；UC/AI/4K 变体可留；sample/预告/广告删 | `av_auto.py:57-60,636-649`；`test_av_auto_process.py` 部分（mock clean）；size=0 风险见 F-P2-13 | ⚠️ 基本一致（P2 待确认） |
| 15 | `bin/av_auto.README.md:70`：fMP4 闭合 Range/redirect cookie/sidx/mfra/不整文件下载 | `fmp4_http.py` 实现方向一致；本报告 HTTP probe 验证 cookie/闭合 Range；styp/free/cap 边界未文档化（F-P2-01/02/03）；无索引线性扫描代价未写（F-P2-04） | ⚠️ 方向一致但有未写边界 |
| 16 | `bin/av_auto.README.md:70`：某一处 seek 过慢则降级跳过，不堵整批 | `_ffmpeg_one_frame` 单次 180s（`av_auto.py:1268`），`_extract_fmp4_frame` 最多 3 次 ffmpeg 尝试，再乘 4 个候选点 → 单点最坏可达 ~36 分钟；"不堵整批"偏乐观 | ⚠️ 文档弱于实现（F-P2 备注） |
| 17 | `README.md:71`：Token 在 `secrets/api.token`（600） | `api.py:18-24` 仅在文件不存在时写 600；已存在文件权限不修正 | ⚠️ 部分一致（F-P2-07） |
| 18 | 仓库根目录未提供"运行前提/依赖/Python 版本/部署步骤" | 无 requirements/CI；`av_auto` 需 p115client/httpx/插件目录；`av_review` 需 ssh/scp/docker 且实际要求 Python ≥3.12 | ❌ 缺失（F-P1-01/F-P2-10） |
| 19 | `bin/av_review_ff_batch.sh` 未被 README/av_auto.README 提及 | 是 `constants.FF_BATCH_*`/vision 旧流程资产，无文档、无测试 | ⚠️ 未文档化资产（F-P2-09） |
| 20 | `bin/av_auto:310` 依赖 `av_auto_apply_fs` | 该脚本不在仓库，README 只在行为描述里提到 | ⚠️ 未纳管外部依赖（F-P1-04） |

---

## 6. 建议补充的测试清单（按价值排序）

> 全部离线可做；前 5 组对应 P1。可直接把本报告 `/tmp` 脚本改成 `unittest` 用例。

1. **fMP4 合成 box 解析/索引单测（无 ffmpeg）**：新建 `tests/fmp4_fixtures.py`（把 `/tmp/fmp4_synth.py` 的 builder 搬入）+ `tests/test_fmp4_parse.py`。
   - 测什么：`parse_box_header`（8/16/64 位、size=0、size<hdr、截断）；`parse_moov`（streams/timescale/video_track_id/trex/mehd）；`parse_moof_samples`（tfhd flag 组合、tfdt v0/v1、trun v0/v1、data_offset 有/无、多 trun、sample flags/cts）；`parse_sidx`（v0/v1、hierarchical ref、SAP=0 强制 sync、truncated）；`parse_mfra`/`_parse_tfra`（track 选择、fallback、2GiB offset）；`build_fmp4_index`（scan/sidx/mfra/progressive None/moov 缺失报错）；`materialize_snippet`（三源、prev 合并、cap 行为、styp/free 边界）；`build_moof_mdat` 往返；`_keep_gop`/`_load_sample_payload` 边界。
   - 怎么 fake：`MemFile`（size/read 计数）+ `build_moof_mdat` 生成合法片段 + 手工拼 sidx/mfra；不需要网络/ffmpeg。
2. **fMP4 HTTP 层单测（无 ffmpeg）**：新建 `tests/test_fmp4_http_local.py`（把 `/tmp/fmp4_http_probe.py` 搬入）。
   - 测什么：redirect 302+Set-Cookie 保留（403 保护媒体可读）、闭合 Range 唯一形态、`Content-Range` total、忽略 Range 的 200 拒绝、short body/total changed、redirect 环 6 跳上限、`read` 跨缓存片段。
   - 怎么 fake：`ThreadingHTTPServer(("127.0.0.1",0))`，handler 记录 Range/Cookie；不需要 ffmpeg。
3. **`av_auto` fmp4 接入单测（无 ffmpeg）**：`tests/test_av_auto_fmp4.py`（把 `/tmp/av_auto_probe.py` 搬入）。
   - 测什么：`_snippet_seek` 钳位（dur≤2 / >2 / ffprobe 超时）、`_extract_fmp4_frame` 成功/首尝试失败/output_seek 回退/seek=0 回退/snip 清理/`via`、`_extract_one` 的 partial-file→fallback 路径、`seek_points`/`_nearby_times` 边界。
   - 怎么 fake：mock `av_auto._run_ff`（ffprobe 返回 JSON、ffmpeg 写 ≥500 字节 jpg）；`Fmp4Session(MemFile, build_fmp4_index(...))`。
4. **promote/rollback 单测**：`tests/test_av_auto_promote.py`。
   - 测什么：dry-run；无 staging；正式库已存在；无成片；`pan.move` 异常；搬家后核验失败（dst/src 状态组合）；strm 写入 0 个/异常；Emby 刷新异常仍 OK；回滚 move 异常；成功后 staging strm 清理。
   - 怎么 fake：fake `Pan115`（`dir_id` 状态机 + `move` 记录）、tmp 目录、`media_mkdir/media_write_text/media_rmtree` 用临时前缀替换、`_emby_refresh` mock。
5. **并发/忙闲单测**：`tests/test_av_review_busy.py` + API 并发用例。
   - 测什么：真实 cmdline（daily/review/catalog --judge、`-m av_review`、rest scan）对 `vision_busy` 的判定；`VisionLock` 获取/释放/陈旧 pid；两个并发 `run_review` 只有一个进入；API 两并发 POST 只 Popen 一次。
   - 怎么 fake：mock `subprocess.run`（pgrep 输出）与 `os.kill`；用 `threading.Barrier` 模拟并发。
6. **`av_review` 冒烟/纯函数单测**：`test_av_review_smoke.py`（`import` 所有模块、`build_parser().parse_args` 各子命令）、`test_av_review_util.py`（`redact`/`norm_code`/`extract_code_from_text`/`publish_pair` 用 fake scp + tmpdir）、`test_av_review_cost.py`（`compute_cost` 边界）、`test_av_review_api.py`（Handler 鉴权 401、坏 body 400、/health 不含密钥、`_bool/_int`）。
   - 怎么 fake：fake `ssh/scp/subprocess`；HTTP 用 `http.client` 对本机 `ThreadingHTTPServer` 或直接构造 handler mock。
7. **115 操作单测（fake pan/p115client）**：`test_av_auto_115ops.py`。
   - 测什么：`cmd_clean` keeper/junk/size=0/嵌套目录/`fs_delete` 参数；`cmd_strm` 无 pickcode 跳过、单/多文件命名；`cmd_submit` state/errno 判定；`cmd_wait` done/fail/timeout；`_load_cookie` 跨插件回退。
   - 怎么 fake：`sys.modules["p115client"]` 注入 fake；fake `Pan115` 用真实 `av115_pan` 的 helper；`_paths` 用 tmp 目录。
8. **ENOSPC 补充分支**：`media_mkdir`/`media_rmtree` 的 ENOSPC 入队、`_queue_fs` 名字碰撞重试（mock `time.time_ns`）、`_perform_pending_job` 未知 kind/非 media 路径、`_resolve_strm_for_code` 的 pending→落盘→复用顺序。
9. **wrapper 失败路径**：fake ssh/docker 覆盖 key-copy 失败、ssh 探测失败仅 NOTE、python 非 0 后 apply_fs 仍执行（已有）、apply_fs 非 0 上报（已有）、trap 清理 local/remote URL、`need_llm=0` 不碰 caller-keys、双 `--url` 拒绝（已有）。
10. **magpie/Emby 网络层**：mock `urllib.request` 返回 HTTPError 422/429/500、超时、非 JSON、无 `choices`，断言 `_chat_retry` 重试与 `_precheck_image` "不确定先当有效"；`_emby_refresh` 用 fake httpx.Client 断言 Media/Updated + Refresh 参数。
11. **ffmpeg 测试改造**：`skipUnless(shutil.which("ffmpeg"))`；fixture 用 `tempfile`/manifest 校验；保留现有集成测试作为"有 ffmpeg 才跑"的 E2E。
12. **文档/版本一致性测试**：断言 `build_parser` 子命令集合 == `bin/av_auto.README.md` 的用法列表（简单文本解析）；`constants.API_PORT_DEFAULT`/host 与 README 一致；`cron` 行路径与 README 一致；`python3 -m py_compile` 目标版本；`git rev-parse` 版本文件与 `av_auto --version` 一致（部署后）。

---

## 7. 已确认正常（核实项）

1. **测试基线可重复**：连续两次 `python3 -m unittest discover -s tests` 都是 Ran 60 / 58 pass / 2 error（均 ffmpeg 环境）/ 0 fail，耗时 1.4~2.0s；`git status` 干净。
2. **fmp4 闭合 Range / cookie / redirect 设计有效**：`/tmp/fmp4_http_probe.py` 本地 HTTP 验证——302+`Set-Cookie` 跨跳跃保留、403 保护媒体可读、所有请求都是 `bytes=a-b`、`Content-Range` total 得 size、忽略 Range 的 200 被拒绝、redirect 环 6 跳内报错（H1/H2/H3/H5/H9/H10 PASS）。这与模块 docstring 的声明一致。
3. **fmp4 解析层核心数学正确**：合成样本验证 scan/sidx/mfra 三种索引、duration 取 mehd、tfdt 时间、sample dts/duration/size/flags/cto 往返、绝对 `base_data_offset` 的 sample 偏移与重建路径、sidx hierarchical ref 跳过、SAP=0 强制 sync、64 位 box、size=0 allow_eof、Content-Range 解析（1.4 中 25 项 PASS）。
4. **`_extract_fmp4_frame` 流程（mock ffmpeg）正确**：生成 init+分片 snip、`_snippet_seek` 在 ffprobe 0.5s 时把 0.5 钳到 0.45、ffmpeg 收到 `-ss 0.450`、成功后 `via=fmp4-range`、临时 `.snip.mp4` 被删除（E1-E4）。
5. **ENOSPC 语义与 pending 恢复链路测试扎实**：19 个 recover 测试覆盖 queue 格式、apply 成功/ENOSPC 留队/非 media 路径、stale floor/预留/磁盘满区分、pending strm 复用、process 不重复排队、`apply_fs` 在 python 失败后仍执行、并发运行 llm_runtime 隔离、wrapper 不 reload shfs/不 bake key、ed2k `|` 不拆 argv、docker 失败仍 apply。
6. **帧/审查结果"先持久化后报成功"有测试**：`test_av_auto_process.py:691-845` 断言 persist 在 success 输出之前、容器 tmp-only 不算成功、持久目录阻塞时返回 1 且不打印成功。
7. **Grok tiebreak 边界与安全有测试**：16 个用例覆盖 trigger 矩阵、Grok pass/fail/uncertain、hook 只在未决时给 argv、prior 行过滤 grok/minimax、只选有效已抽帧（排除 cover/still/Thumb/Backdrop）、默认 review 双模型、默认 chat 拒绝 grok/minimax、已 settled 不调 Magpie、写 sibling 不覆盖 review.json、结果含密钥时拒绝写入、code 不一致/缺 review 报错。
8. **process/argv 陷阱有测试**：Python 与 shell 两侧都拒绝"番号当子命令"并给出正确 `av_auto process ...`；shell/python 正则一致性有专项测试（`test_shell_regex_matches_python`）。
9. **URL 不进命令行/不打印**：`bin/av_auto:211-253` 把 `--url` 写到本地+远端 mode-600 临时文件，heredoc 用 `"$URI"` 传给 docker exec；`Ed2kUrlForwardTests` 断言 ed2k `|` 不会被 ssh 拆开、stdout/stderr 无 key/URL；`_redact_media` 在 ffmpeg/Magpie 错误输出上做 URL 脱敏。
10. **密钥不落结果文件（grok 路径）**：`_write_grok_review`（`av_auto.py:2051-2064`）检查 `secret in text` 则中止；Grok 测试 `test_does_not_write_secret_or_unprechecked_frames` 覆盖。
11. **`.gitignore` 与仓库内容无密钥**：`git ls-files` 27 个文件，无 `secrets/`、`*.token`、`*caller-keys*`、`*.env`、`llm_runtime*`、`grok_review.json`；`.gitignore` 覆盖这些模式；`git status` 干净。
12. **shell 脚本语法正确**：`bash -n bin/av_auto bin/av_review-api-start.sh bin/av_review_ff_batch.sh cron/av-review.cron` 全 OK；`scripts/av_auto.py`、`scripts/fmp4_http.py`、`av_review` 其余 12 个模块在 3.11 下 `py_compile` 通过（只有 `vision.py` 失败，见 F-P1-01）。
13. **测试不碰真实基础设施**：wrapper 测试用 `PATH` 注入 fake `ssh`/`docker`/`apply_fs`，fmp4 HTTP 测试只监听 127.0.0.1，`Fc2Live` 类联网用例在另一个仓库；本仓库测试无真实 115/Unraid/Magpie 访问。
14. **`av_auto process` 不自动交磁/promote**：`cmd_process` 只 clean→strm→frames→review→grok→persist；测试用 `side_effect=AssertionError` 断言 cmd_submit/cmd_promote 未被调用（`test_av_auto_process.py:738-757` 等）。

---

## 8. 待确认（需真机/需 ffmpeg/需真实 CDN）

1. **grokbot-vm 的 `/usr/bin/python3` 版本**：若 <3.12，`av_review review/daily` 与 systemd API 全部不可用（F-P1-01）；需在真机 `python3 -V` 确认。
2. **115 CDN 真实 fMP4 结构**：是否有 `sidx`/`mfra`（决定索引是 2 次请求还是线性扫全片，F-P2-04）、子段是否带 `styp`（F-P2-01）、`moof` 与 `mdat` 之间是否有 `free`（F-P2-02）。
3. **115 CDN 对高频闭合 Range 的限流行为**：`BoundedHttpFile` 无 pacing，长片扫描（尤其无索引）可能触发 405/429；需真机观察。
4. **115 App/aps 列表是否会对正常视频返回 size=0**：决定 `cmd_clean` 的删除风险等级（F-P2-13）；需真机/接口样本核对。
5. **ffmpeg `-ss` 对"绝对 tfdt snippet"的语义**：`build_moof_mdat` 把 tfdt 设为绝对 track 时间，`_snippet_seek` 用相对偏移 seek；仓库 ffmpeg 集成测试（PSNR）在作者环境通过，但本机无法验证；建议在有 ffmpeg 的机器复跑 `IndexTests.test_absolute_offsets_rebuild_without_copying_the_file` 并加断言。
6. **实际运行副本 sha**：真机上 `/config/scripts/av_auto.py` 与仓库 HEAD 是否一致（F-P1-04）；`av_auto_apply_fs` 的实际维护位置/内容是否与 pending 队列格式匹配（仓库只有引用）。
7. **pgrep 实际命令行**：真机上 `bin/av_review` 的进程 argv 是否与本文假设的 `/usr/bin/python3 <repo>/bin/av_review daily ...` 完全一致（F-P1-02 的漏判在真机再核一次）。
8. **`grok-review` 与 `review --grok-review` 的关系**：av_review CLI 的 `--grok-review` 走 vision 内部分支，而 av_auto 的 `grok-review` 是独立 Magpie tiebreaker；两份实现在"何时调 Grok"上是否一致需产品确认（本次只审 av_auto 侧）。

---

## 附录 A：本报告使用的 /tmp 脚本

| 脚本 | 作用 | 结果 |
| --- | --- | --- |
| `/tmp/run_suite_av.py` | 带 `trace` 跑整套测试 | 用于覆盖率 |
| `/tmp/fmp4_synth.py` | 手工构造最小 ftyp/moov/sidx/moof/mdat/mfra | 28 检查：25 PASS，3 个 P2 缺陷实证 |
| `/tmp/fmp4_http_probe.py` | 127.0.0.1 HTTP server 验证 redirect/cookie/闭合 Range | 10 检查：9 PASS（H8 为合成文件过小，预期） |
| `/tmp/av_auto_probe.py` | mock ffmpeg 验证 `_snippet_seek`/`_extract_fmp4_frame`/`seek_points`/`_nearby_times`/cap | 22 检查：15 PASS，其余为本文 F-P2-03 与 /tmp 期望写反 |
| `/tmp/fmp4_styp.py` | sidx 子段带 styp 前缀 | 带 sidx 失败、无 sidx 成功（F-P2-01） |
| `/tmp/probe_busy.py` + 正则核对 | `BUSY_PATTERNS` 对真实 cmdline | `bin/av_review daily`/`catalog --judge` NOT DETECTED（F-P1-02） |
| `/tmp/no_httpx.py`（上一任务遗留） | 不适用本仓库 | — |

## 附录 B：测试基线原始输出（节选）

```
$ cd /workspace/av-review-main && python3 -m unittest discover -s tests -v
...
ERROR: setUpClass (test_fmp4_http.HttpExtractTests)
  File "/workspace/av-review-main/tests/test_fmp4_http.py", line 179, in setUpClass
  File "/workspace/av-review-main/tests/test_fmp4_http.py", line 67, in _ffmpeg_mp4
FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'
...
ERROR: setUpClass (test_fmp4_http.IndexTests)
...
Ran 60 tests in 1.375s
FAILED (errors=2)
```

（66 个测试方法中 6 个因上述 2 个 `setUpClass` 错误未执行；`BoxTests` 2 个正常通过。）
