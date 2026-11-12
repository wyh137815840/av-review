# 第二轮观察 + Emby/`/AV` 数量对账（2026-11-12 08:52–09:35）

承接 `RUN-REPORT-2026-11-12-post-fix.md`。本轮重点：修复后第二段运行、以及「`/AV` 目录数 vs Emby 条目数」的差异到底是不是扫描滞后。

## 1. 第二轮运行

| 指标 | 值 |
| --- | --- |
| 自 08:40 重启以来新 review | **8 部**：6 pass、2 uncertain（均 complete=true，无 incomplete/假成功） |
| 最近 review | `SONE-319` 09:00 uncertain（已带完整字段） |
| 模型成本 | DeepSeek ¥0.0354 + luna ≈$0.0042 |
| `/AV` 目录数 | 504 → **511**（pass 的自动 promote 持续生效） |
| 当前活动 | `library-scan` 存活 23 分钟；当前 `review SONE-373`；progress current=ABP-598 |
| 健康 | 无卡死、无新增 failed；failed 仍为 11-11 的 11 条历史记录 |

结论：第二轮与第一轮一致——审核、promote、Grok tiebreak、Emby 刷新均正常，没有回退。

## 2. Emby / 本地 strm / 云端 `/AV` 三方对账

统计口径：Emby `IncludeItemTypes=Movie`（AV 是唯一 movies 库）；本地 `/media/AV` 同时存在两种布局——`<演员>/<番号>/`（mp-115 写的）与 `<番号>/`（av_auto promote 的）；云端 `/AV` 为 115 目录。

| 集合 | 条目 | 基准番号数 |
| --- | --- | --- |
| Emby Movie 条目 | **507** | 470 |
| 本地 strm 番号目录 | 496（337 扁平 + 159 演员子目录） | **470** |
| 115 `/AV` 目录 | **511** | 501 |

关键结论：

1. **Emby 与本地 strm 完全一致**：按番号基准比对，两个方向都 **0 差异**。之前的「508 vs 504 / 511 vs 507」不是 Emby 扫描滞后。
2. 差异全部来自 **115 云端 `/AV` 与本地 strm 的不对称**：
   - **云端有、本地无：43 个番号**（43 个云端目录没有本地 strm，因此不会出现在 Emby；其中包含部分审核 fail/uncertain 或历史未扫的条目，例如 `ABF-308`、`ABF-393`、`CAWD-891`…）。
   - **本地有、云端无：12 个番号**（本地 strm 遗留，云端目录已不存在，例如 `FC2-PPV-1037933`、`MIDV-432`…）。
3. 507 vs 511 的数字差只是「Emby 条目 / 云端目录 / 带 `-C`、`-UC` 变体的目录」三种口径的折算差，**不是丢数据**。

建议：不要自动补那 43 个（其中一部分本来就是被审核拦下的）；需要时按白名单触发 mp-115 扫描。12 个本地遗留 strm 可人工确认后清理。

## 3. 与生产同步

- av-review 仓库已同步生产版本：`scripts/av_auto.py` 与生产 `/config/scripts/av_auto.py` **sha256 完全一致**（`59ca2003…`），并新增 `scripts/library_scan.py`、`bin/av_auto.unraid`、`bin/av_auto_apply_fs`。
- PR #3 已合并 → main `43fc580`。
- 测试：67 用例、0 失败、2 个既有 ffmpeg 环境错误。

## 4. 剩余事项

1. 43 个「云端有、本地无 strm」的番号需要按需处理（白名单扫描或人工决定），本轮未自动补。
2. 12 个本地遗留 strm 可清理（建议先确认云端确实已删）。
3. `library-scan` 主进程仍是 08:40 启动的实例（子进程已用最新代码）；下次自然重启后调度进程也会加载 `av115_api` 门面版本。
4. `progress.json.updated_at` 只在 inventory 阶段刷新，建议后续补一个周期刷新。
