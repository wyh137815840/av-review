# 03 · JavBus/FC2 刮削与 nfo/封面/strm 生成审计

- 审计对象：`/workspace/mp-av`（`wyh137815840/mp-115` 的 clone），插件 `Av115Scraper` v1.5.8，HEAD `3394c3f`（`Create 115 directories with webapi fs_mkdir (v1.5.8) (#4)`），工作区干净。被审仓库全程只读。
- 宿主对照：`/workspace/moviepilot`（MoviePilot v2.15.6 精简检出）；HTTP 工具签名见 `app/utils/http.py`。
- 审计范围：
  - `plugins.v2/av115scraper/av115_javbus.py` 全文（`Movie`、`parse_detail`、`detail_urls`、`JavBusClient.scrape/download_cover`、`default_fetch`、`download_bytes`）。
  - `plugins.v2/av115scraper/av115_fc2.py` 全文（`article_id`、`cmadb` Inertia 解析、演员二次请求、官网/Wayback/备用站降级、`Movie` 构造、失败返回）。
  - `plugins.v2/av115scraper/av115_library.py` 全文（`build_play_url`、`build_nfo`、`crop_poster`、`find_existing`/`destination`、`write_library`、`safe_name`）。
  - `plugins.v2/av115scraper/av115_scan.py` 中 `_metadata_client` / `_uniqueid_type` / `_nfo` / `_cover` / `_row`，以及 `_handle_video` 里与刮削、落盘、覆盖语义相关的分支。
  - `plugins.v2/av115scraper/__init__.py` 中 `_client()` / `_fc2_client()` 注入、配置默认值（与刮削相关部分）。
  - `tests/` 中与上述模块相关的覆盖情况；README 与 `package.v2.json` 的相应声明。

## 方法

1. 静态通读上述文件，逐条核对 README「规则」「更新后怎么生效」中的声明。
2. 跑基线单测：`cd /workspace/mp-av && python3 -m unittest discover -s tests -v` → **Ran 51 tests, OK (skipped=1)**（skip 为 `AV115_FC2_LIVE`），与 Lead 记录一致。
3. 在 `/tmp/avaudit` 写脚本直接调用 `parse_detail` / `movie_from_cmadb` / `movie_from_official` / `build_nfo` / `build_play_url` / `write_library` / `_handle_video` 等纯函数与可注入方法，构造 HTML/JSON/假客户端复现边界行为；不修改被审仓库。
4. **限量线上抽查**（任务允许，且仓库内 `tests/` 没有任何真实 JavBus 页面 fixture，选择器/正则只能靠线上文档验证）：2026-11-11 18:11–18:21 UTC，经 `http://192.168.1.9:7890` 代理，用与插件一致的 UA / `existmag=all; age=verified` cookie / Referer 请求少量 JavBus 与 FC2 页面。所有 URL 与返回码记在文末「附录 A」，本次抽查只覆盖 4 个 JavBus 番号与 2 个 FC2 番号，不是全站回归。抽查后未再联网。
5. 交叉核对宿主：`/workspace/moviepilot/app/utils/http.py`（`RequestUtils` 的 `proxies: dict` 参数、`verify=False`）、`requirements.in`（`requests[socks]`、`pillow`），以及上游 `requests` 源码中 proxies 的处理方式。

> 说明：Lead 的 `00-lead-crosscut.md` 写「JavBus/FC2 解析结论基于静态代码 + fixture」。本模块额外补了上述限量线上抽查，JavBus 选择器、FC2 fc2cmadb Inertia JSON、演员二次请求、Wayback CDX 均按当前线上页面核实过一次；这些结论有时效性。

## 总体结论

**没有发现 P0**：没有会让已入库云盘/本地库整体不可用、或在大范围默认配置下必然损坏数据的缺陷。但有两个 P1 会**静默损坏/静默降级本地刮削数据**，且都落在 README 明确推荐的使用流程里：

1. **P1-1**：「覆盖已有 nfo 和封面」打开后，只要本轮刮削失败，就会把目录里已有的完整 `movie.nfo` 覆盖成「只含番号」的 nfo；而 nfo 一旦存在，后续默认只更新 strm、不再重刮（README 第「更新后怎么生效」节正好让用户用这个开关回填 FC2）。
2. **P1-2**：FC2 降到官网/Wayback/备用站时只拿到 `og:title`/`og:image`，返回的 `Movie` 缺演员、发售日、标签、时长，但 `scrape()` 返回的 reason 是空串，扫描标 `status="ok"`、`scraped+1`；目录落在 `<strm_root>/<番号>` 而不是 README 声明的 `<演员>/<番号>`，nfo 落盘后默认跳过，缺字段长期不补。官网页面其实含 `販売日` 与 `タグ`，当前解析器没取。

JavBus 主链路（有码 + 三种无码）与 FC2 fc2cmadb 主链路**当前线上实测可用**；nfo 的 XML 转义、`uniqueid` 类型、strm 写入形状、FC2 不裁封面、已有 nfo 只更新 strm 等均有实证。主要 P2 是：配置代理后所有请求实际走 `urllib` 兜底（状态码信息丢失、宿主 SOCKS 代理不可用）、JavBus 无码 URL 顺序反了（每部多打一次 404）、JavBus host 未补 scheme、FC2 演员二次请求失败被静默吞掉、strm URL 里空格/方括号不编码，以及 JavBus 解析/库函数零单测。

结论速览：

| 审计项 | 结论 |
| --- | --- |
| JavBus 有码详情解析（`SSIS-001` 实测） | ✅ 标题/封面/发行日/时长/导演/制作商/发行商/系列/genre/演员全部命中 |
| JavBus 无码 `HEYZO-1234` / `010115-001` / `010115_001` 实测 | ✅ 解析正确（但先打 `/uncensored/<code>` 404，见 P2-2） |
| JavBus 404/缺页 | ✅ 不崩、继续下一个 URL；⚠️ 配代理后 reason 退化成 `HTTP Error 404: Not Found`（P2-1） |
| JavBus 封面下载 | ✅ 站点有防盗链：无 Referer 403，插件按 host 带 Referer → 200 JPEG |
| FC2 `fc2cmadb` Inertia 解析 / 演员二次请求 | ✅ 实测 `component=Articles/Show`、版本号、`props.article`、`X-Inertia-Partial-Data: actresses` 均按预期工作 |
| FC2 官网 / 已删除商品判定 | ✅ 实测 `og:title`/`og:image` 可解析，删除商品页被 `is_deleted` 识别 |
| FC2 降级顺序（cmadb → 官网 → Wayback → 备用站） | ✅ 顺序与代码一致，失败 reason 逐级聚合；⚠️ 降级结果是半截数据却算成功（P1-2） |
| FC2 封面 storage URL | ✅ 缩略图正确还原为 `https://storage*.contents.fc2.com/file/...`，实测可下载 JPEG（无 Referer 也 200） |
| nfo 生成（转义/uniqueid/字段） | ✅ ElementTree 转义正确；`uniqueid` 类型 `javbus`/`fc2`；`premiered/year/runtime/actor/genre/tag/plot` 正常 |
| 封面裁切（JavBus 裁右侧 vs FC2 不裁） | ✅ FC2 不裁；⚠️ 无码 JavBus 封面是 16:9 普通横图，仍按「右侧竖图」裁（P2-7，观感待确认） |
| 写入形状 `<演员>/<番号>/{strm,nfo,poster,fanart}` | ✅ 刮削成功时成立；降级/失败时落到 `<番号>/`（P1-2） |
| 已有 `movie.nfo` 默认只更新 strm、空 nfo 默认跳过 | ✅ 与 README 一致 |
| 「覆盖已有 nfo 和封面」开关 | ❌ 刮削失败时覆盖会清空已有 nfo（P1-1） |
| `safe_name`（Windows 非法字符/保留名/长度） | ✅ 非法字符、`CON`/`COM1` 等保留名、空名处理正确 |
| `build_play_url` | ✅ 默认模板替换正确；⚠️ 文件名里的空格与 `[]()` 不编码（P2-6） |
| 代理/UA/Referer 注入（`_client`/`_fc2_client`） | ⚠️ 代理以 `str` 传给宿主 `RequestUtils(proxies: dict)`，全部请求实际落到 urllib 兜底（P2-1） |
| 测试覆盖 | ⚠️ JavBus 解析、library 纯函数、覆盖语义零直测（P2-8） |

---

## P0

无。

> 最接近 P0 的是 P1-1（覆盖模式 + 一次刮削失败即清空已刮好的 nfo）。本报告按「需要用户主动打开覆盖开关、且元数据可重刮恢复」定为 P1；若评审口径把「README 推荐的回填流程会覆盖丢失本地元数据」视为 blocker，可由 Lead 升级。

---

## P1

### P1-1 覆盖模式 + 刮削失败：已有完整 `movie.nfo` 被覆盖成只含番号的 nfo，且之后默认跳过不再重刮

- **证据**：
  - `/workspace/mp-av/plugins.v2/av115scraper/av115_scan.py:278-300`：`need_meta = existing is None or options.overwrite or not (existing / "movie.nfo").is_file()`；命中 `need_meta` 后无论刮削成败都走同一段写盘：
    ```python
    movie, reason = client.scrape(parsed.code, parsed.kind)      # :283
    item_scraped = True                                          # :284
    if movie is None:
        messages.append(f"{label}：{reason}，已写仅含番号的 nfo")
        nfo_text = _nfo(parsed, None, uid)                       # :288
        result.failed += 1
        status = "partial"
    ...
    write_library(..., nfo_text=nfo_text, cover=cover, force=options.overwrite, ...)  # :307-320
    ```
  - `/workspace/mp-av/plugins.v2/av115scraper/av115_library.py:159-160`：`force=True` 时直接覆盖：
    ```python
    if nfo_text and (force or not (dest / "movie.nfo").is_file()):
        (dest / "movie.nfo").write_text(nfo_text, encoding="utf-8")
    ```
  - 由于覆盖模式下 `existing` 已被 `find_existing` 找到、`dest = existing`（`av115_scan.py:262-263`），写盘目标就是已有完整 nfo 的目录。
- **最小复现**（`/tmp/avaudit/repro/overwrite_wipe.py`，已跑通）：预置 `音田絵凛/FC2-PPV-4751586/movie.nfo`（含标题/发售日/演员），`ScanOptions(overwrite=True)` + 一个 `scrape()` 返回 `(None, "资料站被拦截")` 的客户端，再次 `_handle_video`。输出：
  ```
  item: {'status': 'partial', ..., 'message': 'FC2：资料站被拦截，已写仅含番号的 nfo', 'scraped': True, ...}
  --- nfo after ---
  <movie><title>FC2-PPV-4751586</title><originaltitle>FC2-PPV-4751586</originaltitle>
  <sorttitle>FC2-PPV-4751586</sorttitle><num>FC2-PPV-4751586</num>
  <uniqueid type="fc2" default="true">FC2-PPV-4751586</uniqueid></movie>
  ```
  原来的 `色白スレンダー美乳` / `2025-08-29` / `音田絵凛` 全部丢失；`poster.jpg` 因为 `cover=None` 未被覆盖（只有 nfo 被清空）。
- **影响**：README「更新后怎么生效」明确让用户打开「覆盖已有 nfo 和封面」来回填 FC2 空 nfo。FC2 链的失败概率不低（实测：Wayback 快照 429、备用站 403，见附录 A），一次瞬时失败就把此前刮好的标题/演员/发售日/标签/时长 nfo 覆盖成空；随后 `find_existing` 认定该目录已有 nfo，默认流程永远跳过重刮（`av115_scan.py:278`），只能再次打开覆盖重试或手工修。属于「用户照 README 操作会静默丢元数据」。
- **建议修复**：
  - nfo 覆盖只应在「本轮拿到 movie」时发生：给 `write_library` 传 `force_nfo = options.overwrite and movie is not None`（或让 scan 在 `movie is None and (dest/"movie.nfo").is_file()` 时保留旧 nfo、只写 strm）；
  - 失败/降级时保留旧 nfo，并在 `item`/`ScanResult` 里显式记 `partial`（可另写 `<code>.nfo.partial` 或只更新日志，避免污染 Emby）；
  - 若确实想覆盖为空 nfo，也应只针对「旧 nfo 本身就是空 nfo」的情况。
- **可验证方式**：`python3 /tmp/avaudit/repro/overwrite_wipe.py`；或写单测：预置完整 nfo → `overwrite=True` + 失败 client → 断言 nfo 未被清空。

### P1-2 FC2 官网/Wayback/备用站降级返回「只有标题+封面」的半截数据，却被当作完整成功

- **证据**：
  - `/workspace/mp-av/plugins.v2/av115scraper/av115_fc2.py:232-244`（`movie_from_official`）只解析 `og:title` / `og:description` / `og:image`：
    ```python
    title = clean_title(parse_og(html, "title"), ident)
    cover = unwrap_fc2_image(_absolute(page_url, parse_og(html, "image")))
    ...
    return _movie(code, title, cover)      # release/length/studio/genres/actresses 全缺省
    ```
  - `av115_fc2.py:290-294`：`_official` 一旦返回非 None 就直接 `return movie, ""`，不再尝试 Wayback 补字段；`_html_page`（`:388-403`）/`_wayback`（`:348-367`）/`_backups`（`:369-386`）都复用 `movie_from_official`。
  - `av115_scan.py:291-300`：
    ```python
    actress = movie.actresses[0] if movie.actresses else ""
    if existing is None:
        dest = destination(strm_root, parsed.labeled, actress)   # actress 为空 -> <strm_root>/<番号>
    nfo_text = _nfo(parsed, movie, uid)
    ...
    result.scraped += 1
    status = "ok"
    ```
    没有任何「来源/字段完整度」标记；`_nfo` 里缺的字段直接不写。
- **最小复现**（`/tmp/avaudit/repro/fc2_partial.py`，已跑通）：用只含 `og:title`/`og:image` 的官网片段：
  ```
  official fallback movie: Movie(code='FC2-PPV-4751586', title='サンプルタイトル',
    cover='https://storage200000.contents.fc2.com/file/...11.jpg', release='',
    length='', director='', studio='', publisher='FC2', series='', genres=(), actresses=())
  item: {'status': 'ok', ..., 'message': '完成', 'output': '.../FC2-PPV-4751586', 'scraped': True, 'wrote': True}
  dirs: ['FC2-PPV-4751586/FC2-PPV-4751586.strm', '.../fanart.jpg', '.../folder.jpg', '.../movie.nfo', '.../poster.jpg']
  nfo: <movie><title>FC2-PPV-4751586 サンプルタイトル</title>...<uniqueid type="fc2">FC2-PPV-4751586</uniqueid>
       <publisher>FC2</publisher><plot>サンプルタイトル</plot></movie>
  ```
  即：`status=ok`、无演员目录、nfo 无 `premiered/year/runtime/actor/genre/studio`。
- **补充实测（说明这不是纯理论）**：2026-11-11 18:20:33 UTC 用插件 `Fc2Client` 对一个不存在的番号跑完整链，reason 为
  `HTTP Error 404: Not Found；官网页面没有影片信息；Wayback HTTP Error 429: Too Many Requests；备用站HTTP Error 403: Forbidden`——Wayback 429 / 备用站 403 是本次抽查里真实出现的；只要此时官网这一级能出 `og:title`，就会拿到上文的半截 Movie 并标 ok。
  另外官网页面**并非只有 og 标签**：`/tmp/avaudit/official_nock.html` 里存在 `販売日 : 2025/08/29` 与 `タグ` 锚点（`ハメ撮り` 等），当前解析器没有取。
- **影响**：
  - cmadb 被 Cloudflare 拦 / 超时，但官网可访问时，整批 FC2 的 nfo 缺演员、发售日、标签、时长，目录不进 `<演员>/`，与 README「资料站给出标题、封面、演员、发售日和标签」「本地目录形如 `<演员>/<番号>`」不符；
  - 用户看到 `status=ok`、`scraped` 计数增加，无法察觉是降级结果；
  - nfo 落盘后默认被 `find_existing` 认定「已刮过」，之后即使 cmadb 恢复也不会自动补全（除非开覆盖）；
  - 即使开覆盖重刮成功，`dest = existing`（`av115_scan.py:292-294` 只在 `existing is None` 时用演员目录），**目录也不会从 `<番号>` 迁到 `<演员>/<番号>`**，扁平结构会长期保留。
- **建议修复**（任选其一/组合）：
  1. `Movie` 增加 `source`/`partial`（或记录缺失字段集合）；官网/Wayback/备用站结果一律让 scan 标 `partial`、`failed+1`，并在 message 里写「来源=官网，缺演员/发售日」；
  2. 给 `movie_from_official` 补解析官网已有的 `販売日`、`タグ`（必要时演员），让降级结果比现在完整；若仍缺演员，可继续走 Wayback 合并字段而不是直接成功返回；
  3. 允许「nfo 不完整」时下次自动补刮：`need_meta` 判断增加「nfo 无 actor/premiered」条件，或为降级结果写独立标记文件。
- **可验证方式**：`python3 /tmp/avaudit/repro/fc2_partial.py`；或直接 `movie_from_official(OFFICIAL_HTML, ...)` 观察返回 Movie 的空字段，再用 `_handle_video` 复现 `status=ok`。

---

## P2

### P2-1 配置代理后所有抓取实际走 `urllib` 兜底：宿主 `RequestUtils(proxies: dict)` 收到的是 `str`，状态码信息丢失、SOCKS 代理不可用

- **证据**：
  - `/workspace/mp-av/plugins.v2/av115scraper/av115_javbus.py:163-173`（`default_fetch`）与 `:186-195`（`download_bytes`）都写 `proxies=proxy or None`，而 `proxy` 是字符串（如 `http://192.168.1.9:7890`）。
  - 宿主 `/workspace/moviepilot/app/utils/http.py:334` 声明 `proxies: dict = None`，`:352` `self._proxies = proxies`，`:415` `kwargs.setdefault("proxies", self._proxies)`，随后原样交给 `requests`；`requests` 的 `Session.merge_environment_settings` 会执行 `proxies.get("no_proxy")`（上游源码 [requests/sessions.py `merge_environment_settings`](https://github.com/psf/requests/blob/main/src/requests/sessions.py)），**传入 str 会抛 `AttributeError`**。该异常不是 `requests.exceptions.RequestException`，宿主 `RequestUtils.request` 不捕获 → 冒泡到插件 `except Exception` → 走 `_urllib_fetch` / `_urllib_bytes`。
  - 实测证据（2026-11-11 18:20:21 UTC，经代理）：`JavBusClient.scrape("NOPE-999")` 的 reason 是 `HTTP Error 404: Not Found`，而不是 `JavBusClient` 里写的 `未找到`（`:133-135`）——只有 urllib 路径会把 404 变成异常；同一次 `SSIS-001` 等命中页正常。说明配代理时确实走的是 urllib，而不是宿主 `RequestUtils`。
- **影响**：
  - `default_fetch`/`download_bytes` 里的 `if status == 404` / `if status != 200` 分支在代理模式下基本走不到：404/403/429 都变成异常字符串。JavBus 的「未找到」文案、FC2 `_cmadb` 的「资料站没有这部片」（`av115_fc2.py:322-323`）、`is_challenge` 对 403/429 页面正文的识别都会失效（实测 reason 变成 `HTTP Error 429: Too Many Requests`、`HTTP Error 403: Forbidden`）。
  - 宿主 `requirements.in:21` 是 `requests[socks]`，即宿主本支持 SOCKS 代理；落到 urllib 后 SOCKS 代理无法使用（README 只举例 HTTP 代理，但很多 Clash 配置暴露 socks5）。另外 `verify=False`（宿主 `http.py:417`）也不会生效，自签/异常证书的镜像站会失败。
  - 每次请求先失败一次 `RequestUtils` 再 urllib 重发，多一次本地开销与异常日志噪声。
- **建议修复**：`default_fetch`/`download_bytes` 里把字符串代理转成 dict 再交给 `RequestUtils`：`proxies={"http": proxy, "https": proxy} if proxy else None`；urllib 兜底仍传字符串。同时补一条单测：stub `app.utils.http.RequestUtils` 断言收到的 `proxies` 是 dict。
- **可验证方式**：`PYTHONPATH=plugins.v2/av115scraper python3 -c "from av115_javbus import JavBusClient, default_fetch; print(JavBusClient('https://www.javbus.com', lambda u: default_fetch(u, proxy='http://192.168.1.9:7890')).scrape('NOPE-999','censored'))"`（能看到 `HTTP Error 404...`）；或读宿主 `app/utils/http.py:334/352/415` + requests 源码。

### P2-2 JavBus 无码详情先请求 `/uncensored/<code>`，当前站点该路径全部 404：每部无码多打一次失败请求

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_javbus.py:102-109`
  ```python
  uncensored = f"{root}/uncensored/{code}"
  if kind in {"fc2", "uncensored"}:
      return [uncensored, normal]
  ```
  实测（2026-11-11 18:12:05/18:12:13/18:20:21 UTC）：
  - `https://www.javbus.com/uncensored/HEYZO-1234` → **404**；`https://www.javbus.com/HEYZO-1234` → **200**
  - `https://www.javbus.com/uncensored/010115-001`、`/uncensored/010115_001` → **404**；根路径 → **200**
  - `/uncensored/search/HEYZO` 页里的详情链接全部是 `https://www.javbus.com/HEYZO-1406` 这样的**根路径**（`/tmp/avaudit/www.javbus.com_uncensored_search_HEYZO.html`），说明 `/uncensored/` 现在只用于搜索页，详情页已移到根路径。
  - 插件端到端实测：`HEYZO-1234` 请求序列为 `[.../uncensored/HEYZO-1234(404), .../HEYZO-1234(200)]`，三次无码抽查都是这个序列。
- **影响**：每部无码多一次 404（JavBus 可能计风控/限速），浪费配额；且 404 在代理模式下 reason 变成异常字符串（见 P2-1）。功能结果正确（第二跳命中），属于性能/风控风险。
- **建议修复**：`detail_urls` 对无码也按 `[normal, uncensored]` 顺序（或先 normal、仅在"页面无影片信息/无码提示"时再试 `/uncensored/`）；保留旧路径兜底。
- **可验证方式**：`python3 -c "from av115_javbus import detail_urls; print(detail_urls('https://www.javbus.com','HEYZO-1234','uncensored'))"` → 当前输出 uncensored 在前；配合上面的 curl 结果。

### P2-3 `JavBusClient` 不规范化 host：`javbus_host` 漏写 `https://` 时全部刮削失败（与 `Fc2Client` 不一致）

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_javbus.py:115-118`
  ```python
  def __init__(self, host: str, fetch, download=None):
      self.host = (host or "https://www.javbus.com").rstrip("/")
  ```
  对比 `/workspace/mp-av/plugins.v2/av115scraper/av115_fc2.py:406-412` 的 `_normalize_host` 会补 `https://`。实测：
  ```python
  JavBusClient("www.javbus.com", ...).scrape(...)  # 请求 URL 变成 "www.javbus.com/SSIS-001"
  ```
  配置表单 `__init__.py:264` 的 `javbus_host` 是自由文本输入（placeholder 带 `https://`），用户很容易只填域名。
- **影响**：漏写 scheme 时 JavBus 请求全部因 `unknown url type`/`MissingSchema` 失败，`scrape` 只会给出异常字符串，扫描把每部片都记失败并写空 nfo。
- **建议修复**：把 `Fc2Client._normalize_host` 提取成公共函数，JavBus 也调用；配置保存时校验并补全。
- **可验证方式**：`JavBusClient("www.javbus.com", lambda u:(404,''))` 后查看 `detail_urls(c.host, ...)` 输出。

### P2-4 FC2 cmadb 演员二次请求失败被静默吞掉：`scrape()` 返回「成功但无演员」，无 reason、不重试

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_fc2.py:334-342`
  ```python
  version = str(page.get("version") or "").strip()
  if not version:
      return movie, ""                                  # 无版本号 -> 直接成功、无演员、无提示
  status, body, _error = self._get(url, actress_headers(version))
  if status == 200 and body and not is_challenge(body):
      names = parse_actresses(body)
      if names:
          movie = replace(movie, actresses=names)
  return movie, ""
  ```
  （`:337` 的 `_error` 被丢弃，409/500/网络错误都不影响返回值。）
- **最小复现**（`/tmp/avaudit/repro/fc2_actress_silent.py`，已跑通）：主页面 200、演员请求返回 500，结果：
  ```
  movie: Movie(..., actresses=())
  reason: ''  actresses: ()
  REPRO OK: 演员请求失败但 scrape 返回成功且 reason 为空
  ```
- **影响**：演员请求失败（Inertia 版本 409、瞬时网络错误、被 Cloudflare 挑战）时，nfo 无 `actor`、目录不进 `<演员>/`；scan 标 ok、`scraped+1`、不重试；之后 nfo 落盘默认被跳过，缺演员长期化。与 P1-2 同类，但触发点更隐蔽。
- **建议修复**：把二次请求失败体现在返回值里（如 `reason="演员信息缺失"` 且 scan 记 partial），或返回 `movie` 同时让 scan 知道字段不完整；Inertia 409 时按 `X-Inertia-Location`/版本刷新重试一次（`_get` 目前不返回响应头，需要扩展）。
- **可验证方式**：`python3 /tmp/avaudit/repro/fc2_actress_silent.py`。

### P2-5 `item["scraped"]` 在刮削失败时仍为 `True`，并被 `run_scan` 当作「本轮刮过」用于请求间隔

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_scan.py:283-290` 先 `item_scraped = True`（`:284`）再判断 `movie is None`；`:328` 把该值塞进 `_row`；`:215` `armed = bool(item.get("scraped"))` 决定下一部片前是否 `sleep(interval_seconds)`。实测 partial 行输出 `'scraped': True`（见 `overwrite_wipe.py` 输出）。
- **影响**：刮削失败后仍按「已发起过刮削」的节奏 sleep（默认 3s，配置 `init_plugin` 缺省时 8s），把失败也计入限速；`/status` 返回的 `items[].scraped` 语义不准确（页面文本没展示该字段，仅 API/日志消费者受影响）。风险低。
- **建议修复**：`item_scraped` 改为在「movie 非 None 且写了 nfo」后才置 True（或在 partial 分支置 False）。
- **可验证方式**：任意失败 client 跑 `_handle_video`，断言 `item["scraped"]` 为 False。

### P2-6 `build_play_url` 对文件名里的空格与 `[]()` 不做百分号编码，strm 里可能出现裸空格

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_library.py:30`
  ```python
  name = quote((filename or "video").strip() or "video", safe=".-_()[] ")
  ```
  `space`、`[`、`]`、`(`、`)` 被声明为 safe。实测：
  ```
  build_play_url("https://x?file_name={filename}", "pc", "my file (1)[a].mp4")
  -> "https://x?file_name=my file (1)[a].mp4"   # 空格与方括号原样保留
  ```
- **影响**：正常流程 `actual_name` 已被改成 `番号+suffix`（ASCII 无空格），多数情况无影响；但重命名因「目标文件名已存在」被跳过（`av115_scan.py:336-343`）、或文件名含后缀空格等异常时，strm 里会写成含裸空格/方括号的 URL，部分播放器/HTTP 客户端可能拒绝或自行转义不一致。
- **建议修复**：`safe=".-_"`，让空格/括号/方括号都编码（对 Emby 显示的 `file_name` 无影响，服务端会解码）。
- **可验证方式**：上面的 `build_play_url` 单行调用。

### P2-7 `crop_poster` 对无码 JavBus 封面（16:9 普通横图）也按「右侧竖图」裁 2:3，不是注释说的右侧竖图

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_library.py:126-139`
  ```python
  if width > height:
      poster_width = min(width, max(1, int(height * 2 / 3)))
      image = image.crop((width - poster_width, 0, width, height))
  ```
  `av115_scan.py:319` 对 `parsed.kind != "fc2"` 一律 `crop=True`。实测封面尺寸（2026-11-11 18:12/18:18 UTC）：
  - 有码 `SSIS-001` 的 `/pics/cover/83ie_b.jpg`：**800×538**（右侧确实是竖版封面/包装，裁右侧 358px 合理）
  - 无码 `HEYZO-1234` 的 `/imgs/cover/17fd_b.jpg`：**800×450**（整张是普通横图，右侧没有独立竖版海报），仍会裁右侧 `int(450*2/3)=300`px
  README 只声明「FC2 封面是普通横图，海报不再按 JavBus 那样裁右侧」，但无码 JavBus 封面同样是普通横图。
- **影响**：无码影片的 `poster.jpg` 是横图右侧 300×450 的随机竖切（可能只有腿/床景），Emby 海报观感差；不影响播放/元数据。需要确认这是否是有意行为（当前 docstring 与 README 都暗示只对「右侧带竖图的 JavBus 封面」裁）。
- **建议修复**：按封面宽高比判断（例如宽高比 > 1.6 的纯横图不裁，或裁中间 2:3 区域而不是右侧），或新增按 `parsed.kind`/来源决定裁切策略。
- **可验证方式**：下载上述两张封面比较尺寸；本容器无 Pillow，用 JPEG 头解析即可得到尺寸（见附录 A）。观感结论标「待确认」。

### P2-8 测试覆盖缺口：JavBus 解析、library 纯函数、覆盖语义完全没有直测

- **证据**：
  - `grep -rn "parse_detail\|JavBusClient\|default_fetch\|download_bytes" tests/` 无命中（`tests/test_av115.py` 只覆盖 intake/pan，`tests/test_av115_fc2.py` 覆盖 FC2 与 scan 路由，`tests/test_av115_play.py` 覆盖播放模板）。
  - `safe_name` / `build_play_url` / `write_library` / `find_existing` / `overwrite` 也没有直接用例；`crop_poster` 仅被 monkeypatch 断言「FC2 不裁」（`tests/test_av115_fc2.py:305-336`）。
  - 仓库里没有任何真实 JavBus HTML fixture；FC2 的 fixture 是测试自造 JSON（`tests/test_av115_fc2.py:39-64`），与线上结构目前一致（本次抽查验证），但页面改版无回归保护。
- **影响**：P1-1（覆盖清空 nfo）、P2-2/P2-3/P2-6 这类问题本可以被 10 行单测拦住；JavBus 选择器改版会直接导致线上全部 miss 而测试全绿。
- **建议修复**：把本次线上抽查的 JavBus 页面（有码/HEYZO/加勒比/一本道）裁剪成 `<tests/fixtures/javbus_*.html>`，加 `parse_detail` 断言；为 `build_play_url`/`safe_name`/`write_library(force)`/`find_existing`/`_handle_video` 覆盖语义补纯单测。
- **可验证方式**：`grep -rn "parse_detail" tests/` 应无输出（当前）。

### P2-9 FC2 官网年龄验证检测与 `ageVerified` cookie 未能在本环境复现（待确认）

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_fc2.py:89-90` 用三个英文/类名特征判定年龄门：`"Age verification is required"`、`"ekyc_auth"`、`"error-ipblock__title"`；`__init__.py:749` 固定带 cookie `{"ageVerified": "true"}`。本次抽查（2026-11-11 18:12:49 / 18:15:47 UTC）：
  - `https://adult.contents.fc2.com/article/4751586/` 带/不带 `ageVerified=true` 都是 **200 且直接是商品页**（无年龄门），响应头里也没有年龄相关 cookie；
  - 首页 `https://adult.contents.fc2.com/` 同样没有年龄门。
  仅凭这些无法判断真实年龄门页面长什么样、`ageVerified` 是否为 FC2 认可的 cookie 名。
- **待确认的风险**：如果 FC2 对某些出口 IP/地区返回的年龄门页面不含上述三个特征串，`is_age_gate` 会漏判，`movie_from_official` 可能把门槛页的 `og:title`/`og:image`（站点 logo）当成影片数据：`title` 黑名单只挡 `FC2`/`FC2コンテンツマーケット`（`:240-241`），若 `og:image` 非空就会返回半截 Movie，最终与 P1-2 一样落成「有标题/logo 封面但无演员/日期」的 nfo。
- **建议修复**：年龄门/挑战页检测改为结构判定（例如页面无 `og:title` 且标题含店铺通用名时判失败；或检查 HTTP 状态/重定向路径 `/age`），并让「只解析到封面」的结果不算成功（与 P1-2 的完整度判定合并解决）。抓一次真实年龄门页面并固化为 fixture 后再定论。
- **可验证方式**：待有可复现年龄门的出口环境时抓取页面，跑 `is_age_gate` / `movie_from_official`。

### P2-10 实现里多出的两级备用站（`fc2ppv-db`、`missav`）在本次环境被 403 拦截且 README 未声明

- **证据**：`/workspace/mp-av/plugins.v2/av115scraper/av115_fc2.py:369-373` 在 Wayback 之后还试 `https://fc2ppv-db.com/ja/videos/{ident}` 与 `https://missav.ws/ja/fc2-ppv-{ident}`。实测（2026-11-11 18:16:51 UTC）两个 URL 都是 **403**（Cloudflare 拦截页，`is_challenge` 命中，`/tmp/avaudit/fc2ppv-db_*.html`、`missav_*.html`）；18:20:33 UTC 完整链失败 reason 里是 `备用站HTTP Error 403: Forbidden`（代理/urllib 模式下连正文都看不到，见 P2-1）。
- **影响**：这两级在当前网络基本不可用，只是多两次请求；README 与 `package.v2.json` 历史只声明 `fc2cmadb`/官网/Wayback，用户不知道还有备用站，也不知道它们大概率被墙。
- **建议修复**：保留但补文档说明；或在 `_backups` 前先判站点可用性/超时（当前每级默认 20s，全链最坏 6 次请求 × 20s，会让单部 FC2 刮削卡 1 分钟以上，且 `interval_seconds` 只控制部与部之间）。
- **可验证方式**：`curl -x <proxy> -A <UA> https://fc2ppv-db.com/ja/videos/4751586`、`https://missav.ws/ja/fc2-ppv-4751586` 看 403。

---

## 已确认正常

以下均为本次静态核对/单测/线上抽查确认，**未发现问题**：

1. **JavBus 有码详情解析（实测）**：`https://www.javbus.com/SSIS-001`（2026-11-11 18:11:53 UTC，200/49969B）→ `parse_detail` 得到 `code=SSIS-001`、完整标题、`cover=https://www.javbus.com/pics/cover/83ie_b.jpg`（`<a class="bigImage" href="/pics/...">` 的相对地址被 `urljoin(page_url)` 正确补全）、`release=2021-02-18`、`length=150`、`director=苺原`、`studio=エスワン ナンバーワンスタイル`、`publisher=S1 NO.1 STYLE`、8 个 genre、2 名演员。`_field` 的 `</p>` 终止符与当前页面 `<p><span class="header">…</span>…</p>` 结构一致。
2. **JavBus 三种无码（实测）**：`HEYZO-1234`（根路径 200）、加勒比 `010115-001`、一本道 `010115_001` 均解析出正确 `識別碼`/标题/发行日/时长/制作商/系列（加勒比有系列）/genres/演员，封面为 `/imgs/cover/*_b.jpg`。插件端到端 `JavBusClient.scrape` 四个番号全部 FOUND，封面下载为 JPEG。
3. **JavBus 404/缺页**：`NOPE-999` 两个候选 URL 都失败后 `scrape` 返回 `(None, reason)`，不抛异常、不中断扫描；缺页时不会误写标题/封面。`detail_urls` 的第二跳兜底确实生效（无码先 404 后命中根路径）。
4. **JavBus cookie/UA/Referer**：`existmag=all; age=verified` 能直接拿到完整详情页；封面防盗链实测**无 Referer 403（4547B 的 HTML 拦截页）**、带 `Referer: https://www.javbus.com/` 200（188849B，JPEG），`__init__.py:735` 的 `download` 闭包正是按配置 host 带上 Referer，属正确实现。FC2 storage 封面则无需 Referer（无 Referer 也 200/111513B）。
5. **FC2 fc2cmadb 主链（实测）**：
   - `/articles/4751586`、`/articles/3127193` 均 200，`parse_inertia` 取到 `component="Articles/Show"`、`version=fcb3b524d4c7f8f3d2c38e437b35b7a9`、`props.article`；
   - `movie_from_cmadb` 正确得到标题、`video_id` 校验、`release_date`、`duration`（`01:09:45→69`、`28:38→28`）、`writer.name→studio`、`tags→genres`（去重）、`image_url` 缩略图还原成 `https://storage*.contents.fc2.com/file/...`；
   - 演员二次请求：带 `X-Inertia-Version`、`X-Inertia-Partial-Component: Articles/Show`、`X-Inertia-Partial-Data: actresses` 实测 200/application/json，`parse_actresses` 得到 `音田絵凛`/`石川祐奈`（且只取 `name`，不把 `alias_name` 当演员）；
   - 两个 README 点名的核对番号 `FC2-PPV-4751586`、`FC2-PPV-3127193` 实测标题/演员/封面均正确，封面下载为 JPEG（111513B / 32690B）——README「可用这两个番号对标题、演员和封面」当前成立。
6. **FC2 官网与删除判定（实测）**：`adult.contents.fc2.com/article/4751586/` 200，`og:title`/`og:image` 解析正确且 `clean_title` 去掉 `FC2-PPV-4751586` 前缀；删除/不存在商品页（`/article/1/`、`/100/`、`/99999999/`）都被 `is_deleted` 的 `お探しの商品が見つかりませんでした` 命中并返回 None。`og:description` 兜底标题逻辑存在。
7. **FC2 降级顺序与失败聚合**：代码顺序 cmadb → 官网 → Wayback → 备用站（`av115_fc2.py:284-307`），与 README「优先用 fc2cmadb，失败再试 FC2 官网和 Wayback」不冲突；失败 reason 逐级聚合（实测 `HTTP Error 404…；官网页面没有影片信息；Wayback HTTP Error 429…；备用站HTTP Error 403…`），不会把失败吞成成功（除 P1-2/P2-4 的字段缺失场景）。
8. **Wayback 路径（实测）**：CDX 请求 `.../cdx/search/cdx?url=…&output=json&filter=statuscode:200&fl=timestamp&limit=-1` 200，`latest_timestamp` 正确取到最后一个快照 `20260217114341`；快照页 429 时响应体含 `suspected abusive bot traffic`，`is_challenge` 命中并降级（说明「Wayback 被拦」有识别，不会把拦截页当成功）。
9. **FC2 不走 JavBus / dry_run 不请求**：`test_av115_fc2.py` 的 `test_fc2_uses_fc2_client_and_uniqueid`、`test_censored_still_uses_javbus`、`test_fc2_miss_stays_partial_without_javbus`、`test_missing_fc2_client_does_not_fall_through`、`test_dry_run_does_not_request_fc2` 均通过；代码 `av115_scan.py:389-393` 只按 kind 选择客户端，dry_run 在 `:252-256` 提前返回，不请求不写盘。
10. **nfo 生成**：用 ElementTree 实测 `A & B <C> "D"`、`G&H`、`<g>`、演员含 `&<>` 等字符，`ET.fromstring` 回读正常，无 XML 注入/解析错误；`uniqueid` 默认 `javbus`、FC2 路径为 `fc2`（`test_av115_fc2.py` 亦断言）；`premiered/year/runtime/director/studio/maker/publisher/set/genre/tag/actor/plot` 按预期写入；`tag=-C` 追加 genre「中文字幕」、`-UC` 追加「无码破解」；movie 为 None 时生成只含番号的合法 nfo。
11. **写盘形状与覆盖基础语义**（`/tmp` 模拟实测）：新刮削成功 → `<strm_root>/<演员>/<番号>/{<番号>.strm,movie.nfo,poster.jpg,fanart.jpg,folder.jpg}`；已有 `movie.nfo` 且未开覆盖 → 只重写 strm、保留 nfo（符合 README）；`overwrite=True` 且刮削成功 → nfo/封面被替换；`find_existing` 能在一层演员目录下找回已有目录。
12. **封面裁切实现**：`crop_poster` 在 `width > height` 时裁右侧 `height*2/3`，有码 JavBus 封面 800×538 裁出 358×538（2:3），符合「右侧竖图」意图；FC2 路径 `crop=False`（`av115_scan.py:319`），单测断言 `crop_poster` 未被调用；宿主 `requirements.in:37` 有 `pillow~=12.2.0`，`crop_poster` 不会静默退化成「不裁」。
13. **`safe_name`**：`<>:"/\|?*` 与控制字符被替换，首尾点/空格被去掉，`CON/PRN/AUX/NUL/COM1-9/LPT1-9` 加前缀 `_`，空名回落 `unknown`——本次构造的 15 组输入均符合 Windows 目录名要求（唯一残留是长度截断的边界，见下方「已知限制」）。
14. **`build_play_url` 默认模板**：`https://…/redirect?pickcode={pickcode}&file_name={filename}` 两个占位符都被替换，pickcode 做了 `%` 编码，正常番号文件名（`SSIS-001.mp4`）输出与 README 示例一致；`choose_strm_template` 的内外网选择在 `tests/test_av115_play.py` 覆盖。
15. **`_client()` / `_fc2_client()` 注入**：`__init__.py:676-682` 把两个客户端显式传给 `run_scan`，`_fc2_client` 的 host/proxy 回退（`fc2_proxy` 空则用 `proxy`）与 `__init__.py:740-741` 默认值一致；dry_run 也不会构造网络请求（客户端构造本身不发请求）。

**已知限制（不单独列问题）**：
- `safe_name` 先 `rstrip(". ")` 再 `cleaned[:80]` 截断（`av115_library.py:17,23`），若输入超过 80 字符且第 80 个字符恰好是空格/点，截断后会重新出现尾随空格/点（Windows 目录名非法）。当前调用方只传演员名/番号（长度远小于 80），实际不可触发；若未来把 title 之类长文本传给 `safe_name` 需先截断再去尾。
- 已有 `movie.nfo` 但缺 `poster.jpg` 时，默认流程只更新 strm、不会补封面（`need_meta` 只看 nfo，`av115_scan.py:278`）——README 已写明「已有 `movie.nfo` 时默认只重写 strm，不重刮封面」，属已知语义，需用覆盖开关补。

---

## README 与实现逐条核对

| README 声明 | 本次核对 |
| --- | --- |
| 有码 `SSIS-001`/`ssis001`/`ABP-123C`，字幕 `-C`、破解 `-UC` | ✅ 识别层由 `av115_codes` 负责（另有审计），`av115_scan.py` 以 `parsed.labeled` 命名、`tag` 进 nfo genre。 |
| FC2 `FC2-PPV-1234567`，元数据不走 JavBus | ✅ 路由与测试确认；实测 FC2 链全部请求 `fc2cmadb`/`adult.contents.fc2.com`/`web.archive.org`/备用站。 |
| 资料站给出标题、封面、演员、发售日和标签 | ✅ fc2cmadb 实测五项齐全。 |
| 官网年龄验证或商品已删除时再试 Wayback | ✅ 顺序一致（cmadb → 官网 → Wayback）；删除判定实测有效；年龄门见 P2-9（待确认）。 |
| nfo 的 `uniqueid` 类型是 `fc2` | ✅ 单测 + 模拟写盘确认；JavBus 路径为 `javbus`。 |
| 无码 `HEYZO-1234`、加勒比 `010115-001`、一本道 `010115_001` | ✅ 三种格式实测 JavBus 命中并正确解析。 |
| 封面普通横图不裁（FC2） | ✅ 实现 `crop=parsed.kind != "fc2"`；注：实测 FC2 封面 780×780 方图（不是「横图」），但不裁的语义正确。 |
| 本地目录 `<演员>/<番号>/`，内含 `<番号>.strm`、`movie.nfo`、`poster.jpg`、`fanart.jpg` | ✅ 刮削成功时成立（多写一个 `folder.jpg`，无害）；⚠️ 降级/失败时落到 `<strm_root>/<番号>/`（P1-2/P1-1 相关）。 |
| 已有 `movie.nfo` 默认只更新 strm | ✅ 与 `_handle_video` 分支一致（`av115_scan.py:278,301-305`）。 |
| 已有空 nfo 的目录默认跳过 | ✅ `find_existing` 只看 `movie.nfo` 是否存在，空 nfo 也算「已有」。 |
| 打开「覆盖已有 nfo 和封面」才重刮 | ✅ 语义成立；❌ 但刮削失败时会把好 nfo 覆盖为空（P1-1）。 |
| 来源没有刮到时仍写 strm 和只含番号的 nfo | ✅ partial 分支实测；注意它同时 `failed+1`。 |
| 插件重载/旧 strm 不会自动改 | ✅ 只有扫描会重写 strm；本地已有 strm 文件内容由扫描改写（与 README「关掉只预览再跑一遍」一致）。 |
| 代理：内地访问 JavBus 时填写代理，FC2 留空沿用 | ⚠️ 代理能工作（实测），但实际走 urllib 兜底且丢失状态码/SOCKS 支持（P2-1）。 |

---

## 附录 A：本次线上抽查记录（2026-11-11 UTC，经 `http://192.168.1.9:7890`）

| 时间 | 请求 | 结果 |
| --- | --- | --- |
| 18:11:53 | `GET https://www.javbus.com/SSIS-001` | 200 / 49969B；`parse_detail` 全字段命中 |
| 18:12:05 | `GET https://www.javbus.com/uncensored/HEYZO-1234` | 404 / 260B（触发插件第二跳） |
| 18:12:05 | `GET https://www.javbus.com/HEYZO-1234` | 200 / 42688B；解析正确 |
| 18:12:13 | `GET https://www.javbus.com/010115-001`、`/010115_001` | 200；解析正确（系列/genres/演员） |
| 18:12:13 | `GET /uncensored/HEYZO`（搜索页） | 200；详情链接均为根路径 `/HEYZO-*` |
| 18:12:22 | `GET https://www.javbus.com/pics/cover/83ie_b.jpg`（带 Referer） | 200 / 188849B，`image/jpeg`，800×538 |
| 18:12:32 | `GET https://fc2cmadb.com/articles/4751586`、`/3127193` | 200；Inertia `component/version/props.article` 齐全 |
| 18:12:39 | `GET …/articles/4751586` + `X-Inertia*`（actresses） | 200 / 917B，`application/json`，演员 音田絵凛 |
| 18:12:43 | 插件 `Fc2Client.scrape` 两个番号 + 封面下载 | 标题/演员/日期/标签正确；封面 111513B / 32690B JPEG |
| 18:12:49 | `GET https://adult.contents.fc2.com/article/4751586/`（带/不带 `ageVerified`） | 均 200，直接商品页（未出现年龄门） |
| 18:13:26 | `GET /article/1/`、`/100/`、`/99999999/` | 200，均命中删除页文案 |
| 18:14:25 | `GET web.archive.org/cdx/.../4751586/` | 200，`[["timestamp"],["20260217114341"]]` |
| 18:14:30 | `GET web.archive.org/web/20260217114341id_/...` | 429 / 620B，正文含 `suspected abusive bot traffic`（`is_challenge` 命中） |
| 18:16:51 | `GET fc2ppv-db.com/ja/videos/4751586`、`missav.ws/ja/fc2-ppv-4751586` | 均 403，Cloudflare 拦截页 |
| 18:18:41 | `GET /pics/cover/83ie_b.jpg`（无 Referer） | 403 / 4547B（证实防盗链，插件带 Referer 是对的） |
| 18:18:41 | `GET storage200000.contents.fc2.com/file/…11.jpg`（无 Referer） | 200 / 111513B JPEG |
| 18:19:30 | `GET fc2cmadb.com/articles/4751586`（无 Accept 头） | 200，Inertia 解析正常 |
| 18:20:21 | 插件 `JavBusClient` 端到端：SSIS-001 / HEYZO-1234 / 010115-001 / 010115_001 / NOPE-999 | 前四个 FOUND（封面 JPEG），NOPE-999 MISS 且 reason=`HTTP Error 404: Not Found`（P2-1 证据） |
| 18:20:33 | 插件 `Fc2Client.scrape("FC2-PPV-9999999")` 完整链 | 失败并聚合 reason：cmadb 404 → 官网无信息 → Wayback 429 → 备用站 403 |

复现脚本（只读被审仓库，全部落在 `/tmp`）：
- `/tmp/avaudit/repro/overwrite_wipe.py` — P1-1 覆盖清空 nfo
- `/tmp/avaudit/repro/fc2_partial.py` — P1-2 官网降级半截数据当成功
- `/tmp/avaudit/repro/fc2_actress_silent.py` — P2-4 演员二次请求失败被吞
- `/tmp/avaudit/` 下保留了本次抓取到的 JavBus/FC2/官网/Wayback 页面与 `fetch.log`，供 Lead 复核；被审仓库未做任何修改（`git status` 干净）。
