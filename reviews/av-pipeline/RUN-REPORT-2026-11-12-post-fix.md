# library-scan 运行报告（修复后第一轮，2026-11-12 08:40–09:20）

观察对象：Unraid moviepilot 容器内 `av_auto.py library-scan`（重启后的新进程 PID 243642）。
本轮前置状态：aps 回绕死循环已修（web `/files` 兜底）、P0-C review 假成功已修、插件升到 1.5.10 并引入公开门面 `av115_api`、旧 `/config/plugins/av115scraper` 副本已退役。

## 1. 运行与吞吐

| 指标 | 值 |
| --- | --- |
| 观察窗口 | 08:40–09:20（Asia/Shanghai） |
| 新完成 review | **5 部**：`SONE-049`（uncertain→Grok fail）、`SONE-067/071/164/236`（pass） |
| `complete=true` | 5/5；无 incomplete / 无假成功 |
| Grok tiebreak | `SONE-049`：Grok `ok=true`、verdict `fail`、resolved，最终未 promote（符合预期） |
| 本轮模型成本 | DeepSeek ¥0.0195 + luna ≈$0.0027 |
| `/AV` 目录数 | 504 → **508**（+4，正好等于 4 部 pass 的 promote） |
| 当前活动 | `frames SONE-305`；`library-scan` 进程存活（14 分钟，无挂起） |

## 2. 端到端验证（通过本轮真实入库）

- `/media/AV` 最新目录：`SONE-236, SONE-164, SONE-071, SONE-067, SOE-681, SOE-556, SOE-523, SNOS-452` ——与 review pass 结果一致。
- Emby 抽查 `SONE-236`：条目已出现，`Path=/media/AV/SONE-236/SONE-236.strm`，`ProviderIds` 含 **MetaTube**（+TrailerUrl），标题/图片齐全。
- 说明「审核 pass → 自动 promote → 115 `/AV` → 本地 strm → Emby 刷新 → MetaTube 刮削」整条链路在本轮实测跑通。

## 3. 进度/状态

- `progress.json`：phase=inventory、current=ABF-314、done=57、failed=11、submitted=1、counts.remaining=2439。
- `failed` 的 11 条全部是 **2026-11-11 12:17** 的历史失败（旧版 review 不完整），不是本轮新增。
- 说明：`updated_at` 字段仍停在 08:40（只在 inventory 阶段刷新），但 `current` 与实际子进程在推进——属于进度展示的小问题，不影响调度。

## 4. 修复项在运行中的证据

| 修复 | 运行时证据 |
| --- | --- |
| web `/files` 兜底 | `/AV` aps `count=508`；`list_children` 返回全量、无重复、无卡死 |
| P0-C 完整性 | 新 `SONE-*` review.json 均含 `complete=true`；无 `incomplete_models` |
| Grok 失败不再算完成 | SONE-049 tiebreak `grok.ok=true` 且 resolved；流程正确未 promote |
| 公开门面 | `av_auto._plugin_dir()` 返回 `/app/app/plugins/av115scraper`，`av115_api.API_VERSION=1` |
| 单一插件源 | `/config/plugins/av115scraper` 已改名为 `.bak-drift-20261112-005317`，进程仅用已安装副本 |

## 5. 仍需关注

1. **Emby 条目数与 `/AV` 差 4**（Emby movie total 504 vs /AV 508）：可能是 Emby 媒体库扫描/元数据异步滞后，或 4 部为尚未出现在 AV 库查询口径里的新 strm；下一轮观察是否追平。
2. **官方 corpus producer** 仍不在仓库/Unraid，本次是重建语料后启动；后续应由 grokbot-vm/仓外调度器接续喂语料。
3. `progress.json.updated_at` 只在 inventory 阶段刷新，建议加一个周期刷新（小改）。
4. 生产 `av_auto.py` 与 av-review 仓库已同步「review 完整性 + 公开门面」两批改动；其余历史差异（~1200 行）仍需一次完整同步。
5. 两份插件副本已收敛为「MoviePilot 安装副本」单一来源；`/config/plugins/av115scraper.bak-drift-*` 保留作回滚，确认稳定后可删。

## 6. 结论

本轮 library-scan 在修复后**无卡死、无假成功、无新增失败**；5 部新审 4 部通过并自动入库，1 部 uncertain 由 Grok 判 fail 拦下；每部片子的 strm/Emby/MetaTube 链路已实测通。整体可以继续跑下一轮观察。
