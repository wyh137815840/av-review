# 生产事件：aps 分页回绕导致 promote 死循环（2026-11-12）

- 环境：Unraid `192.168.1.9` 的 moviepilot 容器（MP v2.15.6 / Python 3.12.13），插件 `Av115Scraper 1.5.8`，`av_auto library-scan` 正在跑（`auto_promote.on` 开关 ON）。
- 发现途径：08:15 检查「`SNOS-437` 的 review 进程 07:12 启动后 1 小时不退出」。
- 结论：**不是 LLM 卡住，而是 `Pan115.list_children` 的 aps 分页死循环**（即 mp-115 评审的 P0-1 在生产上真实发生），由 `av_auto promote` 列 `/AV` 根目录触发。

## 时间线 / 症状

1. `SNOS-437` 的 `review.json` 在 07:13 已写好：`agreement=agree / consensus=pass`，两家模型 `ok=True / http=200`（审核本身 1 分钟完成）。
2. 之后 `cmd_review` 末尾调用 `_auto_promote_standalone`（开关 ON + pass）→ `cmd_promote` → `_promote_one` 列 `/AV` 根目录。
3. 进程 PID 239191 持续存活：RSS 389MB → 395MB（10s 采样 +1.7MB，持续增长），每约 2.5s 向 115 发一次 aps 请求（socket 常连 47.113.23.100:443，即 proapi/webapi/aps.115.com），CPU 几乎为 0（`hrtimer_nanosleep`）。
4. 同容器 `library-scan`（PID 59522）被该子任务拖住；期间又出现 `review SNOS-452` 等同类进程。

## 根因（真实 115 复现）

对 `/AV`（目录 id `3542541563349435767`）直接探测 aps（只读）：

| 请求 offset | 返回行数 | 响应 offset | 响应 count | 第一行 |
| --- | --- | --- | --- | --- |
| 0 | 500 | 0 | 500 | `_offline_staging` |
| 499 | 1 | 499 | 500 | `1PONDO-012324_001` |
| **500** | **500** | 500 | 500 | `_offline_staging`（回到第一页） |
| 999 | 500 | 999 | 500 | `_offline_staging` |
| 1000 | 500 | 1000 | 500 | `_offline_staging` |

`/AV` 真实条目数 = **500**（App2 `offset=500` 返回 0），aps 在 `offset >= 总数` 时**从头返回整页**，且响应里的 `offset` 仍回显请求值、`count` 是页大小。插件的唯一终止条件是 `len(page) < 500`，于是 offset 500/1000/1500… 永远返回满页 → 无限循环、`items` 无限增长。

## 修复

- 分支：[wyh137815840/mp-115 `fix/aps-pagination-wrap`](https://github.com/wyh137815840/mp-115/tree/fix/aps-pagination-wrap)，commit `5e8159f`。
- 改动：`plugins.v2/av115scraper/av115_pan.py` 的 `Pan115.list_children`
  - 按 id 去重；**本页没有新条目 = 服务端回绕 → 立即停止**；
  - 保留 `len(page) < 500` 与空页终止；新增 `_APS_MAX_PAGES=200` 页数兜底与告警。
- 回归测试：`tests/test_av115.py::ListChildrenPagingTests`（整页==总数不循环；正常短尾页 700 条不重复）。本地 53 用例通过（含原有 51）。
- 部署（生产）：
  - MoviePilot 运行副本 `/app/app/plugins/av115scraper/av115_pan.py`（备份到 `/config/scripts/backups/` 后覆盖）；
  - **av_auto 实际 import 的旧副本** `/mnt/user/appdata/moviepilot/config/plugins/av115scraper/av115_pan.py`（同样先备份）——这一份与 MP 运行副本原本不一致（部署漂移，见评审 F-P1-04）；
  - `GET /api/v1/plugin/reload/Av115Scraper` 重载，返回 success。

## 修复后实测

| 验证 | 结果 |
| --- | --- |
| `Pan115(...).list_children('/AV')` | 500 项、两次请求、约 4.9s、id 全不重复（修复前：无限循环） |
| 杀掉卡住的旧进程 | `SNOS-437` review 进程 TERM 后退出；其 `review.json` 已是 pass |
| `av_auto promote SNOS-437 SNOS-452` | `OK SNOS-437：115 已在 /AV，补 strm 1 个；Refresh AV 204`；`OK SNOS-452：/av-auto/SNOS-452 → /AV/SNOS-452；strm；Refresh AV 204` |
| 本地 strm | `/media/AV/SNOS-437/…strm`、`/media/AV/SNOS-452/SNOS-452.strm` 已生成；`/media/av-auto/*` 已清理 |
| 115 侧 | `/AV/SNOS-437`（视频 9.5GB）、`/AV/SNOS-452`（3.5GB）在位；`/av-auto/*` staging 已消失 |
| Emby 元数据 | `SNOS-437`、`SNOS-452` 均已被 **MetaTube** 刮到：`ProviderIds.MetaTube = FANZA:snos00437 / snos00452`，标题/图片齐全（验证「通知 Emby → MetaTube 刮削」链路真实生效） |
| 流水线恢复 | `library-scan` 继续运行：新 `review SOE-110` 正常完成（无卡死），后续已开始 `intake SOE-523` |

## 残留风险 / 待办

1. **aps >1200 的静默截断仍在**：本次修复只保证「不死循环」，大目录超过可访问窗口后仍会漏条目；要彻底解决需在 `list_children` 用 App `/files`（支持 offset 分页，实测可正确翻页）或 web `/files` 的回绕信号做兜底。
2. **部署漂移**：`/config/plugins/av115scraper`（av_auto 用）与 `/app/app/plugins/av115scraper`（MP 用）是两份代码，本次已同步 `av115_pan.py`，但其余文件仍可能不同；建议统一为「插件从仓库安装 + av_auto 用公开接口」。
3. **P0-C（review 假成功）仍在**：`_complete_review` 不校验模型 `ok`；本次事件中它没有造成误 promote（`review_passed` 只认 pass），但失败 review 会被永久跳过。
4. `library-scan` 进程本身仍是修复前启动的旧代码（新子进程已用修复版）；建议在合适窗口重启一次，确保自身路径也加载新代码。
5. 生产 `auto_promote.on` 处于 ON，`/AV/SNOS-437` 的 strm 文件名仍是原文件名（`www.98T.la@…strm`），与 `<番号>.strm` 约定不完全一致，后续可统一。

## 复现命令（只读）

```bash
# aps 回绕
docker exec moviepilot python3 /tmp/probe_aps2.py        # 现场脚本；offset=500 返回第一页
# 修复后
docker exec moviepilot python3 /tmp/verify_fix.py       # list_children /AV -> 500 items
# promote 验证
docker exec moviepilot python3 /config/scripts/av_auto.py promote --dry-run SNOS-452
```

## 后续修复（2026-11-12 09:00 前完成）

### 1) aps 大目录兜底 + `library-scan` 重启

- `Pan115.list_children` 增加 **web `/files` 兜底**：aps 返回的 `count` 是目录总条目数；当 aps 只取到 `len < count` 时，用 `https://webapi.115.com/files`（`limit=1000`、正常 offset 分页、`count` 是总数、行结构与 aps 相同的 `n/fid/cid/fc/pc/s`）补齐。生产 `/AV` 实测从 500 项变为 **504 项（全量、无重复）**。
- 回归测试：新增 503 项与 2500 项目录用例；`python3 -m unittest discover -s tests` → **55 用例通过**（含原 51 + 分页 4 个）。
- `library-scan` 重启：官方 corpus producer 不在仓库/Unraid 上，本次按同一 TSV 协议（`T\tcode\tverdict\terotic` / `E\tcode\turl` / `M\tcode\turl`）从 findav DB 重建 corpus（2899 T + 2910 E + 377 M，mode 600，读后即删），用 `AV_AUTO_CORPUS_FILE` 启动。新进程 PID 243642 正常运行，`progress.json` 恢复（already_reviewed 421、remaining 2439），后续 `review SONE-049` 正常完成。

### 2) P0-C（review 假成功）修复（生产 `av_auto.py`）

- `_complete_review`：要求 `review.json` 的每个 `REVIEW_MODELS` 行 `ok is True`、verdict 合法、且 `complete is not False`；否则视为「未审」，下次重跑。
- `cmd_review`：任一模型调用失败时写 `agreement=incomplete`、`complete=false`、`incomplete_models=[...]`，保留现场但**返回非 0**，不再被当成完成。
- `_grok_already_done`：要求 `grok.ok is True` 且 `complete is not False`；Grok 调用失败不再算「已跑过」。
- `cmd_grok_review`：Grok 失败时留档 `grok_review.json`（审计用）但返回非 0，不 promote。
- 备份：`/config/scripts/backups/av_auto.py.bak-p0c-20261112-004110`；部署后 `md5=9f4c8a066582e6e5b2d54ff5f7264d63`。
- 容器内定向测试：双模型 503 → `rc=1`、`agreement=incomplete`、`complete=False`、`_complete_review` 拒绝；正常 pass → `rc=0`、`complete=True`；Grok 失败 → `_grok_already_done=False`；存量 pass review（SNOS-437）仍被接受。
- 生产实证：补丁后新审的 `SONE-049/review.json` 已带 `complete=True`，说明流水线在用新代码。

### 3) 发版（mp-115 → 1.5.9）

- 分支 `fix/aps-pagination-wrap`，PR **#5 已合并**：`0ef9fb1 Merge pull request: Av115Scraper 1.5.9 aps pagination`。
- `plugin_version` / `package.v2.json` 升至 **1.5.9**，history 记录本次修复；版本一致性测试改为动态比对（不再硬编码 1.5.8）。
- 生产两份插件副本（`/app/app/plugins` 与 `/config/plugins`）均已更新到 1.5.9 并 reload；`GET /api/v1/plugin/` 显示 `Av115Scraper 1.5.9, state=True`。

### 更新后的残留风险

1. web `/files` 兜底在 >1200 项目录上的真实表现尚未用生产大目录验证（本次只验证了 503 项；接口每页 1000，理论可分页，建议下次遇到 >1200 目录时观察日志「web 兜底取到 N 项」）。
2. `av_auto` 的 P0-C 补丁目前只在**生产副本**，av-review GitHub 仓库主线落后生产约 1200 行；下次同步时要把这 4 处补丁一起带回仓库。
3. 两份插件副本已同步 `av115_pan.py` + `__init__.py`，其余文件仍可能存在差异（部署漂移未根治）。
4. `library-scan` 本次是用「从 DB 重建的 corpus」启动的；官方 producer（grokbot-vm / 仓外调度器）若对 corpus 有额外过滤，需要下次由它接续。
