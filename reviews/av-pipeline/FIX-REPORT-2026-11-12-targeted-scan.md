# 定向修复报告：43 个「云端有、本地无 strm」+ 12 个本地遗留 strm（2026-11-12）

承接第二轮对账：云端 `/AV` 与本地 strm 不对称（43 个云端目录没有本地 strm；12 个本地 strm 的云端目录已不存在）。本轮按白名单修复。

## 1. 定向扫描（38 个白名单）

方法：通过 MoviePilot 插件配置临时把 `cloud_paths` 换成 38 个目标目录（`overwrite=false`、`dry_run=false`），调用插件 `/run`，完成后恢复原配置（已确认恢复：`cloud_paths=/AV`、`overwrite=True`）。全程约 15.7 分钟，扫描结果由插件 `/status` 返回。

排除的 5 个（不自动入库）：
- progress failed：`EBOD-531`、`ABF-308`、`SONE-573`
- review fail：`IPZZ-003`、`IPX-528`

扫描结果（status）：`items=16`、`renamed=11`、`scraped=15`、`failed=1`、`partial=1`（`SIRO-1690` JavBus 404，已写仅含番号的 nfo）。

**16 个已补齐并在 Emby 验证**（全部 `ProviderIds` 含 `javbus` + `MetaTube`）：

```
WANZ-059  STAR-471  SSIS-743  SSIS-586  SNOS-222  SNIS-420  SIRO-1690
ROYD-358  MIKR-031  MIDE-592  IPX-867   IPX-850   IPX-831   IPX-706
IPTD-500  ABP-968
```

Emby 查询：16/16 命中，`Path` 为 `/media/AV/…/<番号>.strm`，0 缺失；Emby movie 总数 507 → 524（含同期 library-scan 新增）。

其余 22 个云端目录**没有可写的视频**（不是修复失败）：
- 4 个空目录：`SSNI-454`、`HTM-111`、`FC2-PPV-4699342`、`DA-9164`
- 14 个只有 `nfo / movie.json / cover.jpg / samples/`（无视频）：`SNOS-373`、`ROOM-079`、`PAP-282`、`MSOC-083`、`MIDA-732`、`MIDA-031`、`MFYD-151`、`MFYD-130`、`MBDD-2178`、`HRD-420`、`HRD-421`、`CAWD-891`、`CAWD-906`、`ABF-393`
- 4 个被扫描标准化成 `CARIB-091523-001 / CARIB-083023-001 / 1PONDO-110323_001 / 1PONDO-090223_001`，内部只有 `samples/gallery.zip`，无视频

建议：这 22 个云端目录可以后续按「空目录/纯元数据目录」清理（不影响 Emby）；本轮未动。

## 2. 本地遗留 strm 清理（12 个）

- 先确认云端确实不存在：重新拉取 115 `/AV` 全量名称，并对每个番号做「全名/去符号/数字 ID」匹配，12 个全部确认云端无对应目录。
- 处理方式：移动到回收目录 `/mnt/user/media/.av_trash_20261112-013554/`（不直接删除，可回滚）。
- Emby：触发 `POST /Library/Refresh`，扫描完成后逐个查询，12 个在 Emby 中均为 **0 条**（已移除）。
- 移动清单：`FC2-PPV-1037933/1218365/1946644/1947670/2063055/2132144/2523603/3120686/3972042/4512681/4591756`、`MIDV-432`（`宮下玲奈/`）。

## 3. 当前状态

| 项 | 值 |
| --- | --- |
| 插件 | `Av115Scraper 1.5.10`；配置已恢复（`cloud_paths=/AV`、`overwrite=True`） |
| 云端 `/AV` | 522 个目录 |
| 本地 `/media/AV` | 598 个目录（337+ 扁平 + 演员子目录） |
| Emby Movie | 524 |
| library-scan | 运行中，未受影响 |
| 回收目录 | `/mnt/user/media/.av_trash_20261112-013554/`（12 个，确认后可删） |

## 4. 备注 / 风险

1. 临时切换 `cloud_paths` 期间，插件 watcher 会用第一个 cloud_path 作为离线整理目标（本次首个是 `/AV/091523-001`）；未观察到文件被错误搬移，但这是配置切换的副作用，后续定向扫描建议给插件加「roots 参数」或独立入口，避免改正式配置。
2. 定向扫描较慢（38 个 root 约 16 分钟）：插件对每个 root 都会重新列一遍 `/AV`（aps 1200 窗口 + web 兜底），后续可加目录列表缓存或在 `run` 里复用根目录列表。
3. 22 个无视频云端目录与 12 个回收 strm 属于「历史脏数据」，可按需清理；`SKIP` 的 5 个码仍由 library-scan 的重试/人工决定处理。
