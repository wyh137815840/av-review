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
