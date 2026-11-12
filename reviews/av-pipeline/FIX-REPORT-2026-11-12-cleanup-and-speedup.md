# 清理与提速报告（2026-11-12 10:00–10:30）

承接 `FIX-REPORT-2026-11-12-targeted-scan.md` 的三条建议：① 清理无视频云端目录 + 回收站；② 处理 5 个 failed/skipped 码；③ 插件扫描提速并发布。

## ① 无视频云端目录清理 + 回收站删除

- 22 个「云端有、本地无 strm 且确认无视频」的目录已从 115 `/AV` 移到根目录 `/_cleanup_20261112`（可恢复，不是直接删除）：
  - 4 个空目录：`SSNI-454`、`HTM-111`、`FC2-PPV-4699342`、`DA-9164`
  - 14 个仅 `nfo/movie.json/cover/samples`：`SNOS-373`、`ROOM-079`、`PAP-282`、`MSOC-083`、`MIDA-732`、`MIDA-031`、`MFYD-151`、`MFYD-130`、`MBDD-2178`、`HRD-420`、`HRD-421`、`CAWD-891`、`CAWD-906`、`ABF-393`
  - 4 个标准化后的 `CARIB-091523-001`、`CARIB-083023-001`、`1PONDO-110323_001`、`1PONDO-090223_001`（内部只有 `samples/gallery.zip`）
- 结果：115 `/AV` 522 → **505**（之后新 promote 到 507），`/_cleanup_20261112` 22 个。
- 本地回收站 `/mnt/user/media/.av_trash_20261112-013554`（12 个 strm 目录）在二次确认 Emby 已全部移除后**已删除**。

## ② 5 个 failed/skipped 码

| 码 | 现状 | 处理 |
| --- | --- | --- |
| `EBOD-531` | 云端有视频（10.3GB），无本地 strm | ✅ 通过 `/run_roots` 扫描补齐；Emby `javbus`+`MetaTube` 元数据完整 |
| `SONE-573` | 云端有视频（4.4GB），无本地 strm | ✅ 已补 strm；JavBus 404 → 仅含番号 nfo（Emby 仍由 MetaTube 补全） |
| `ABF-308-C` | 云端有视频，无本地 strm | ✅ 已补；Emby 元数据完整 |
| `IPZZ-003` | 云端 + `/av-auto` 都有视频，review 判定 fail | ⏸ 保持 review 拦截，不写入 Emby，待人工决定 |
| `IPX-528` | 同上 | ⏸ 同上 |

3 个重试码用新 `/run_roots` 接口扫描，**约 50 秒**完成（`renamed=3, scraped=2, failed/partial=1`，Emby 已刷新）。Emby 复核：3/3 条目存在，`ProviderIds` 均含 `javbus` + `MetaTube` + `TrailerUrl`。

## ③ 插件 1.5.11：目录缓存 + `/run_roots`（已发布）

- `Pan115.list_children` 增加 **60 秒目录列表缓存**，`move/rename/ensure_dir` 时失效。实测 `/AV` 第一次 5.09s，第二次 **0.000s**。
- 新增 `POST /api/v1/plugin/Av115Scraper/run_roots`，JSON `{"roots":["/AV/X",...]}` 直接按目录扫描，**不再需要临时改 `cloud_paths`**（消除 watcher 用首个目录做整理目标的副作用）。
- 测试：61 用例通过；容器内验证：不存在目录 → `failed: 目录不存在`，配置未被改动；3 个 root 扫描 50s 完成。
- 发布：版本 1.5.11 + history；**PR #7 已合并** → main `1375f56`；生产插件已更新并 reload（`plugin_version=1.5.11`）。

## 当前状态

| 项 | 值 |
| --- | --- |
| 插件 | `Av115Scraper 1.5.11`（含缓存 + `/run_roots`） |
| 115 `/AV` | 507 个目录；`/_cleanup_20261112` 22 个可删 |
| 本地 `/media/AV` | 608 个目录 |
| Emby Movie | **532** |
| 本地回收站 | 已删除 |
| library-scan | 运行中，未受影响 |
| 待人工 | `IPZZ-003`、`IPX-528`（review fail，未入库）；`/_cleanup_20261112` 22 个目录确认后可删 |

## 备注

- 后续定向扫描统一用 `/run_roots`（或 `av115_api`），不要再临时改 `cloud_paths`；如需 dry-run/覆盖开关可按需扩展该接口参数。
- 缓存只在单个进程内生效，且写操作会立即失效；对「扫描大量 root」提速明显，对单目录行为无变化。

## 5. 后续落实（10:30–10:50）

- **删除 115 `/_cleanup_20261112`**：二次确认 22 个条目均无视频后逐条删除，最后删除目录本身；`_web_getid("/_cleanup_20261112")` 返回 GONE，`/AV` 当前 544 个目录，云端无残留。
- **`IPZZ-003` / `IPX-528` 决策：不入库**。两者 review 都是两家模型一致 `fail`（原因均为「狰狞/难受表情」），没有 Grok 分歧；按要求保持 review 拦截，不写本地 strm/Emby。云端原片与 `/av-auto` 副本保留，供人工复核。
- **`/run_roots` 固化**：
  - 本机新增运维脚本 `/workspace/ops/scan_roots.py`（POST `/run_roots` → 轮询 → 打印 status），已用不存在目录验证失败路径输出。
  - `/workspace/ops/README.md` 更新到 1.5.11 现状，并写入「定向扫描统一用 `/run_roots`，不要临时改 `cloud_paths`」。
  - 文档 PR：mp-115 #8（README 增加 `/run_roots` 用法）、av-review #4（bin/av_auto.README 增加定向补 strm 说明，并注明它不经过 av_auto review 门）。
- 当前状态：插件 1.5.11；library-scan 运行中（PID 243642，约 3h20m）；frames 524、auto_promote ON；Emby 570；本地 strm 目录 646；`/AV` 544。
