# mp-115「115 AV刮削」v1.5.8 代码评审

评审对象：[wyh137815840/mp-115](https://github.com/wyh137815840/mp-115) 的 MoviePilot V2 插件 `Av115Scraper`（`plugins.v2/av115scraper/`，v1.5.8，`main@3394c3f`）。宿主对照源码：`jxxghp/MoviePilot` v2.15.6。

> **一句话结论：单机单 Emby、已装 P115StrmHelper、单个 115 目录少于 1200 项的现有环境基本可用；但不建议按当前 README 装机或直接发布——有 1 个 P0（大目录扫描静默漏文件/可能死循环）和 16 个去重后的 P1，另有约 55 个 P2。**
>
> 最该先修的 5 件事：**aps 分页截断（P0）**、**`p115client` 依赖未声明**、**详情页「加入任务」是死按钮**、**播放地址默认值写死作者环境**、**提交纯番号没有消费路径**。

## 1. 评审范围与方法

- 范围：`番号/磁力/ed2k 入库 → 115 离线提交 → 完成整理（/云下载 → /AV/番号）→ 扫描改名 → JavBus/FC2 刮削 → 本地 strm/nfo/封面 → 内网/外网播放分流 → Emby 刷新`，以及 MoviePilot V2 宿主契约、配置表单、任务状态机、测试与文档。
- 组织：Lead 做编排层与跨模块复核，5 名审计员按模块并行（A 入库/状态机、B 115 操作/扫描、C 刮削/写盘、D 播放/Emby/宿主、E 测试/横切），各自的完整清单在 `findings/`。
- 方法：逐行静态审计 + 宿主 v2.15.6 源码逐项对照 + 单测基线 + stub 宿主/`httpx.MockTransport`/内存 storage 离线复现 + 限量线上抽查（JavBus/fc2cmadb/官网/Wayback/备用站，见 `03` 附录 A）+ MoviePilot-Frontend v2.15.6 release 产物核对。
- 基线：`python3 -m unittest discover -s tests` → **51 用例 / 50 通过 / 1 跳过**（FC2 联网用例默认 skip）；`py_compile` 全过。仓库无 `requirements.txt`，测试依赖 `httpx`（缺它时 10 个用例 import error）。
- 边界：没有真实 115/Emby 凭据，未做全链路线上实测；涉及真实接口返回形状、前端行为的结论均标「待确认」，详见各分册。

### 报告导航

| 文件 | 内容 | 原始问题数 |
| --- | --- | --- |
| [00-lead-crosscut.md](findings/00-lead-crosscut.md) | 跨模块问题 + Lead 复核记录（含对 5 份报告的交叉印证） | P1×3、P2×10（与分册有交叉） |
| [01-intake-codes.md](findings/01-intake-codes.md) | 番号/磁力/ed2k 入库、任务状态机、并发与任务持久化 | P0×0、P1×5、P2×11 |
| [02-pan-scan.md](findings/02-pan-scan.md) | 115 App/aps 接口、限速重试、离线整理、扫描/改名/落盘 | **P0×1**、P1×3、P2×14 |
| [03-scrape-library.md](findings/03-scrape-library.md) | JavBus/FC2 抓取解析、nfo/封面/strm 生成（含限量线上抽查） | P0×0、P1×2、P2×10 |
| [04-play-host.md](findings/04-play-host.md) | 内外网播放分流、`/redirect`、Emby 刷新、MoviePilot 宿主契约 | P0×0、P1×2、P2×8 |
| [05-quality-tests.md](findings/05-quality-tests.md) | 测试覆盖、dry_run、线程生命周期、安全、文档/版本一致性 | P0×0、P1×5、P2×12 |
| [evidence/verify_aps_paging.py](evidence/verify_aps_paging.py) | Lead 对 P0 的独立复现脚本（MockTransport 模拟 aps 1200 上限与回绕） | — |

> 原始计数合计：P0×1、P1×20、P2×65。跨文件重复引用已在各文件互相标注，去重后约 **P0×1、P1×16、P2×55**。

## 2. P0

### P0-1 · aps 列目录分页终止条件错误：目录 >1200 项静默漏扫，极端情况死循环

- 位置：`av115_pan.py:208-223`（`Pan115.list_children`），`_PAGE=500`。
- 事实依据：p115client 对同一接口 `https://aps.115.com/natsort/files.php` 的文档明确「最多获取任何一种排序条件下的前 `1200+n` 条；`offset >= 1200+n` 时等价于 `offset=0`」；p115client 自身用 `offset`/`count` 判断终止，插件只用 `len(page) < 500`。
- 复现（[evidence/verify_aps_paging.py](evidence/verify_aps_paging.py)）：
  - 目录 2000 项：只发 3 次请求（offset 0/500/1000），返回 **1200 项，静默漏 800 项**，无异常、无告警；
  - 若服务端越界后仍返回满页：offset 序列 0,500,1000,1500… **无限循环**，`items` 无限增长，`run()` 的 `_lock` 不释放，之后所有扫描都被「已有任务在运行」挡住。
- 影响：`/AV/有码`、`/AV/FC2` 这类直接子目录上千个番号文件夹是常见形态；超过 1200 的部分永远扫不到，表现为「插件跑完了，但后段没生成 strm」。若触发回绕则整套扫描挂死。
- 修复：按响应里的 `offset`/`count` 终止（回绕即停止并告警）；超过 1200 项改走 App `/files` 或 `p115client.iter_files` 兜底，或明确报错而不是静默截断。

## 3. P1（去重后按优先级）

| # | 问题 | 位置 | 影响 | 分册 |
| --- | --- | --- | --- | --- |
| 1 | 提交「纯番号」返回成功但没有任何消费路径，任务永远 `queued` | `__init__.py:443-458`、`467-481`、`av115_intake.py:34-41` | README 头号入口之一实际不生效，用户/机器人被静默忽略 | [01](findings/01-intake-codes.md) / [00](findings/00-lead-crosscut.md) |
| 2 | 任务表硬截断 50 条且静默丢弃：一次提交 60 条只存 50，返回 `accepted=60` | `__init__.py:454-457` | 批量/机器人转发时最早的任务被丢，调用方看不出 | [01](findings/01-intake-codes.md) |
| 3 | 无番号的磁力/ed2k 被接受后永久 `offline`：不整理、不刮削、无提示 | `__init__.py:487-493`、`av115_intake.py:65-67` | 文件留在 `/云下载`，任务永远显示等待 | [01](findings/01-intake-codes.md) / [00](findings/00-lead-crosscut.md) |
| 4 | 重复提交无幂等，重复任务永久 `offline` 且每 20s 重打 115 | `__init__.py:443-458`、`av115_pan.py:188` | 点两次/重发即产生悬挂任务 + 持续 115 请求，有风控风险 | [01](findings/01-intake-codes.md) |
| 5 | 插件禁用后仍启动离线监视线程并提交离线/搬移云盘；配置重入还会产生双 watcher | `__init__.py:154`、`467-533` | 「关闭插件」不生效，后台继续写 115 | [01](findings/01-intake-codes.md) / [05](findings/05-quality-tests.md) / [00](findings/00-lead-crosscut.md) |
| 6 | `dry_run=True` 只挡扫描，不挡离线提交与 115 搬移，与 README「此时不改 115」冲突 | `__init__.py:467-533` | 预览模式下云盘/离线任务已被真实修改 | [05](findings/05-quality-tests.md) / [01](findings/01-intake-codes.md) |
| 7 | `p115client` 是硬依赖但仓库无 `requirements.txt`，宿主也不提供 | `av115_pan.py:77/171`、`__init__.py:561` | 新装环境离线提交与 `/redirect` 播放全部失败（502/ImportError） | [00](findings/00-lead-crosscut.md) / [05](findings/05-quality-tests.md) / [04](findings/04-play-host.md) |
| 8 | 详情页「加入任务」是死按钮：`VTextarea.props.model` 在宿主 PageRender 不绑定，POST body 为空 | `__init__.py:323-343`（前端 `PageRender.vue`） | 页面输入的番号/磁力/ed2k 根本不会提交 | [04](findings/04-play-host.md) |
| 9 | 播放默认模板写死作者环境（`mp.yuanhot.top` / `192.168.1.9:3000`）并写入新用户配置 | `av115_play.py:26-33`、`__init__.py:69-71/99-130` | 新用户 strm 不可达；关内网开关后会把 pickcode/片名发往第三方域名 | [04](findings/04-play-host.md) |
| 10 | `_checked` 吞掉「`state:false` 但无 errno」的错误：move/rename 假成功、计数虚增 | `av115_pan.py:327-339` | 云盘没动但报告「已改为/已移动」，离线任务被标 ready | [02](findings/02-pan-scan.md) |
| 11 | `relocate_code` 全盘搜索命中即搬，不校验来源目录与番号 | `av115_pan.py:186-256` | 会把扫描根/其他根里的同番号文件（含 `-C`/`SSIS-0010` 类包含匹配）搬进第一个 cloud_path | [02](findings/02-pan-scan.md) |
| 12 | 扫描根填「文件路径」时，其父目录会被改名 | `av115_scan.py:120-131/179/358-362` | 违反「扫描根不改名」，可能把整个分类目录改掉 | [02](findings/02-pan-scan.md) |
| 13 | 覆盖模式下刮削失败会用「只含番号」的 nfo 覆盖已有完整 nfo，之后默认跳过不再重刮 | `av115_scan.py:283-290/307-320`、`av115_library.py:159-160` | README 推荐的 FC2 回填流程可能把好元数据洗掉 | [03](findings/03-scrape-library.md) |
| 14 | FC2 降级到官网/Wayback/备用站时只取 og:title/og:image，半截数据被当成功 | `av115_fc2.py:232-244/290-294/344-346` | 缺演员/发售日/标签，目录落不到 `<演员>/`，且写盘后不再补全 | [03](findings/03-scrape-library.md) |
| 15 | 插件主类 `__init__.py`（934 行、45 个函数）0 测试覆盖 | `tests/` 从未 import 主类 | 编排/权限/生命周期/Emby 刷新无回归保护 | [05](findings/05-quality-tests.md) |
| 16 | 核心 115/JavBus 数据通路 0 覆盖（只测替身） | `av115_javbus.py` 0/12 进入，`av115_pan` 18/37，`av115_scan` 13/18 | 上游接口一变就无保护 | [05](findings/05-quality-tests.md) |

> 说明：第 7 条在三份报告里同根因（`00-L2` / `04-D-P1-03` / `05-E-P1-05`），只计一次。

## 4. P2 概览（约 55 条，详见分册）

按主题归纳，完整清单与证据在对应文件：

- **任务/状态机**：`_update_task` 复活被截断任务、状态模型与实际不符（`packed`/`settled` 非 status）、扫描结果不回写任务、`_load_tasks` 吞异常可能覆盖整表、`/task` 在 async 端点里同步批量写库。
- **115 操作**：改名失败不计 failed、`_MAX_DEPTH` 静默跳过、`size==0` 绕过 300MB、重叠扫描根/同番号多文件重复处理与 counts 虚增、祖先目录改名后嵌套改名失效、旧 strm 不清理、`search` 只取前 100 且 2.0 字段兼容不全（待确认）、UID 解析过窄、网页/离线调用不受 `_pace` 限速、`Pan115.http` 从不关闭且每 20s 新建、离线整理无终态、watcher 与扫描无共用写锁、`CookieStorage` 与 `_U115Storage` 语义不一致。
- **刮削/写盘**：代理以 `str` 传给宿主 `RequestUtils(proxies: dict)` 导致退到 urllib（状态码丢失、SOCKS 不支持）、无码 `/uncensored/` 路径全 404 白打一次请求、JavBus host 不补 scheme、FC2 演员二次请求失败被吞、`item["scraped"]` 失败仍为 True、strm 文件名空格/`[]` 未编码、无码封面被误裁、备用站实测 403 未文档化。
- **播放/Emby**：`X-Forwarded-Host` 被固定成内网时 302 自循环、只信 XFF 首跳（Docker nginx 必注入 XFF，LAN 反代漏设会误判外网为内网）、播放模板无校验（非法端口/IPv6 → `/redirect` 500）、`p115client` 版本未约束（新版移除 `download_url(app=)`）、Emby 多实例只刷第一个、`wrote` 判定在 items 截断 200 之后、`/task` 的 `auth=bear` 与「请求头带 API 令牌」文案易 401。
- **横切**：`_url_cache` 懒创建竞态/无上限、`/redirect` 匿名可用任意 17 位 pickcode 换 115 签名直链且 502 回显异常原文、README 泄漏作者内网地址、`interval_seconds` 默认值 3/3/8 不一致、icon 结论已由 `04` 关闭、无 CI。

## 5. 与 README/页面承诺不一致（用户视角）

1. 「详情页、输入框、`POST /task` 可以提交番号、磁力或 ed2k」——纯番号无消费；详情页按钮实际提交空 body。
2. 「保持只预览……此时不改 115」——`dry_run` 不挡离线提交与云端搬移。
3. 「扫描根目录本身不改名」——扫描根填文件路径时父目录会被改名。
4. 「小于 300MB 会跳过」——接口缺体积（`size=0`）时会被当正片。
5. 「安装：把目录加到插件仓库即可」——没声明 `p115client` 依赖，干净环境装完就功能不齐。
6. 播放地址默认值与 README 默认表写的是作者自己的域名/内网 IP。
7. `/task` 鉴权说明只写「请求头带 API 令牌」，未写要发 `X-API-KEY`（`Authorization: Bearer` 只收 JWT）。
8. 文档未提 FC2 的额外备用站、实际会写 `folder.jpg`、`interval_seconds` 默认值三处不一致。

## 6. 做得好的地方（避免只列问题）

- 内外网播放判定矩阵相当完整：14 组 Host/XFH/XFF/Real-IP 组合实测通过，反代改写 Host 时的回环保护、同 origin 不自跳、IPv4-mapped IPv6 归一都正确（`04`）。
- 扫描规则与 README 基本一致：dry_run 不落盘、已有 `movie.nfo` 只更新 strm、300MB/预告过滤、目标同名不覆盖、多正片只改文件名、扫描根（目录形态）不改名（`02` 实测）。
- 115 风控与幂等有认真处理：全局限速 `_pace`、405/429/5xx 退避重试、`fs_mkdir` 的 getid 竞态与「已存在」处理、离线/移动的幂等设计（`02` 已确认 18 项）。
- 宿主契约与 v2.15.6 逐项匹配（14 项）：`_PluginBase/get_form/get_page/get_api/get_service`、`save_data/get_data`、热重载、`StorageChain`、`ModuleManager`/EmbyModule、`package.v2.json` 元数据、icon（`04`）。
- JavBus 有码/无码解析、fc2cmadb Inertia+演员版本号、nfo 转义/uniqueid、strm 写入形状、FC2 不裁封面经线上抽查实测正常（`03` 已确认 15 项）。
- 51 个单测无网络依赖、可重复（`05`）。

## 7. 建议修复顺序

1. **发布/新装前必须**：P0-1（aps 分页）、P1-7（`p115client` 依赖声明 + 可读错误）、P1-8（死按钮：去掉按钮或改 Vue 页面，二选一）、P1-9（默认播放模板改空并强制首次填写）、P1-1/P1-2/P1-4（番号任务、队列截断、去重与幂等）、P1-5/P1-6（禁用/dry_run 门禁）。
2. **115 写安全**：P1-10（`_checked` 严格化）、P1-11（`relocate_code` 来源与番号校验）、P1-12（文件形态扫描根保护）、P1-3（无番号离线任务显式失败/提示）。
3. **刮削正确性**：P1-13（刮削失败不得覆盖已有 nfo）、P1-14（FC2 降级结果标记为部分成功/继续补全）。
4. **可维护性**：`requirements.txt`、最小 CI（`python -m unittest` + `py_compile`）、新增 `tests/test_av115_plugin.py`（stub 宿主导入主类）与 `01`/`02`/`05` 列出的补测清单；顺手把 P2 里「状态模型/任务持久化/客户端生命周期」一起重做。

## 8. 复现方式

```bash
# 基线
cd /workspace/mp-av   # 或你的 clone
python3 -m unittest discover -s tests -v     # 51 / 50 过 / 1 skip

# P0：aps 分页（Lead 独立复现，MockTransport，不访问真实 115）
python3 evidence/verify_aps_paging.py
# 期望看到 clamp 模式 returned=1200（漏 800）；wrap 模式 LOOP DETECTED

# p115client 缺失时的播放失败
#   在无 p115client 的环境请求 /api/v1/plugin/Av115Scraper/redirect?pickcode=<17位>
#   → 502 body: resolve failed: No module named 'p115client'

# dry_run / 禁用状态仍写 115
#   见 05 的 /tmp/harness.py + /tmp/scenarios.py 思路（stub 宿主导入主类，FakePan 记录调用）
```

各分册条目里都写了对应的最小复现/验证命令，MockTransport 与内存 storage 的脚本原型在审计员报告的方法节中有说明。

## 9. 复核与更正说明

- `00` 初稿曾误判「宿主没有 Emby 定向刷新私有方法」，复核后更正：宿主源码写作 `def __refresh_emby_library_by_id`（`/workspace/moviepilot/app/modules/emby/emby.py:577`），当前版本可用，已降级为「私有方法依赖 + 失败文案误报」的 P2。
- P0-1 由 `02` 发现，Lead 用独立脚本复现后维持 P0 定级。
- `04` 用 MoviePilot-Frontend v2.15.6 release 实测 `plugin_icon/Moviepilot_A.png` 存在，关闭了 `05` 的 icon 待确认项。
- `03` 的线上抽查发生在 2026-11-11 18:11–18:21 UTC，URL/返回码记录在其附录 A；`03` 同时指出 `00` 初稿「仅静态+fixture」的说法对 03 不适用。
- 仍待真机确认：115 aps 超 1200 项的真实回包、`/2.0/ufile/search` 字段风格、P115StrmHelper cookie 键名、`p115client` 版本与签名、详情页 textarea 的实际提交行为。

## 10. 报告位置与局限

- 本报告位于 [wyh137815840/av-review](https://github.com/wyh137815840/av-review) 仓库的 `reviews/mp-115-v1.5.8/` 目录（分支 `review/mp-115-v1.5.8`）；该仓库 `main` 属于另一个进行中的项目（`av_review`），本报告未改动 `main`。
- 没有真实 115/Emby 账号，未做完整线上链路回归；所有「待确认」项都需要真机或宿主前端版本再验证一次。
