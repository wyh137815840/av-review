# 04 · 播放分流 / redirect / Emby 刷新与 MoviePilot V2 宿主契约审计

- 被审仓库：`/workspace/mp-av`（`wyh137815840/mp-115` 的 clone，main 干净，`3394c3f` "Create 115 directories with webapi fs_mkdir (v1.5.8)"）
- 插件：`plugins.v2/av115scraper/`，`plugin_version = "1.5.8"`，`package.v2.json` version `1.5.8`
- 宿主对照源码：`/workspace/moviepilot`，`version.py APP_VERSION = 'v2.15.6'`（`FRONTEND_VERSION = 'v2.15.6'`）
- 审计员：auditor-play-host（任务 D / `task-4`）
- 审计范围：`av115_play.py` 全部；`__init__.py` 中 `api_redirect` / `_download_url` / `_refresh_emby_library` / `init_plugin` / `stop_service` / `get_form` / `get_page` / `get_api` / `get_service` / `get_command`；`package.v2.json` 与 README 的一致性；与 MoviePilot v2.15.6 实际实现逐项核对
- 结论一句话：**未发现 P0；本模块新增 P1 2 条、P2 8 条**；另有 1 条与 Lead `00-L2` / 质检 `05-E-P1-05` 同根因（不重复计数）。宿主契约（`_PluginBase` / `PluginManager` / `StorageChain` / `ModuleManager` / `EmbyModule` / API 注册 / 调度器 / 热重载）逐项核对**全部匹配**，主要风险集中在「X-Forwarded-* 判定边界」「播放模板无校验」「Emby 多实例只刷一台」「默认值写死作者环境」，以及一个用户可见的死按钮。

---

## 1. 方法与可复现命令

1. **测试基线**（实测）：
   ```bash
   cd /workspace/mp-av && python3 -m unittest discover -s tests -v
   # Ran 51 tests in 0.424s
   # OK (skipped=1)          # Fc2LiveTest，需 AV115_FC2_LIVE=1
   ```
   与 Lead 记录一致（51/50 过/1 skip）。注意 `tests/` 里**没有任何一例** import 插件主类 `__init__.py`，本模块涉及的 `api_redirect` / `_download_url` / `_refresh_emby_library` / `get_api` 契约均无测试覆盖。
2. **纯函数矩阵实验**：脚本 `/tmp/t_play.py`（`sys.path` 指向插件目录，直接调用 `redirect_location` / `origin_of` / `build_play_url`），覆盖 14 组 Host / X-Forwarded-Host / X-Forwarded-For / X-Real-IP / peer 组合与 4 组畸形模板，结果见第 2、3 节与附录 A。
3. **宿主源码逐项核对**：阅读 `/workspace/moviepilot` 中 `app/plugins/__init__.py`、`app/core/plugin.py`、`app/api/endpoints/plugin.py`、`app/core/module.py`、`app/core/security.py`、`app/chain/storage.py`、`app/schemas/file.py`、`app/schemas/types.py`、`app/modules/emby/*`、`app/scheduler.py`、`app/helper/plugin.py`、`requirements.in`、`docker/nginx.common.conf`、`version.py`。
4. **外部对照**（通过 HTTP 代理抓取，非本仓库内容）：
   - PyPI `p115client==0.0.9.7.2`（P115StrmHelper 当前 pin 的版本）sdist 与 GitHub main 分支 `p115client/client.py`，核对 `download_url` 签名；
   - DDSRem-Dev/MoviePilot-Plugins 的 `plugins.v2/p115strmhelper`（`requirements.txt`、`api.py`、`core/config.py`），作为同类 302 插件的既有实现对照；
   - `MoviePilot-Frontend` v2.15.6 的 release `dist.zip`（Dockerfile 会解压到 `/public`，见 `docker/Dockerfile:100-102`）与 `src/components/render/PageRender.vue`、`FormRender.vue`、`src/components/cards/PluginCard.vue`。
5. **局限**：容器里没有 `fastapi` / `p115client` / `apscheduler` / 真实 115/Emby 凭据，宿主侧只做**静态源码核对**，未跑真实宿主；插件主类无法在容器内 import，`api_redirect` / Emby 刷新为代码路径推演 + 宿主源码确认。凡是无法直接实测的均标「待确认」。

---

## 2. 问题清单

### 2.1 P0

**未发现 P0。** 本模块范围内没有「功能完全不可用且无替代路径」或「数据丢失/严重安全」级问题（播放依赖缺失见 D-P1-03，属于环境前提缺失，且有 P115StrmHelper 时可工作）。

### 2.2 P1

#### D-P1-01 · 详情页「加入任务」是死按钮：输入的番号/磁力/ed2k 根本不会提交

- **证据（插件侧）**：`plugins.v2/av115scraper/__init__.py:323-332` 在 `get_page()` 里返回 `VTextarea`，只有 `props.model = "text"`（无值、无绑定）；`:333-343` 的 `VBtn` 事件为
  ```json
  "events": {"click": {"api": "plugin/Av115Scraper/task", "method": "post"}}
  ```
  没有 `params`，也没有任何字段携带 textarea 内容；页面文案 `:315-321` 明确宣称「把番号、磁力或 ed2k 贴在下面，点「加入任务」」。
- **证据（宿主前端）**：本仓库 `/workspace/moviepilot` **不含**前端静态资源，前端在 `MoviePilot-Frontend` v2.15.6（`dist.zip` 即宿主 Docker 部署产物，`docker/Dockerfile:100-102`）。其 `src/components/render/PageRender.vue:36-53`（`commonAction` 只把 `attr['params']` 当 POST body，GET 也一样）、`:56-67`（events 组装只取 `api`/`method`/`params`）、`:71`（`v-bind="config?.props"` 原样透传，不处理 `props.model`）；插件详情页由 `src/components/dialog/PluginDataDialog.vue:156` 直接渲染 `PageRender`。对比配置页的 `FormRender.vue:29-34` 才把 `props.model` 映射成 `modelValue`/`onUpdate:modelValue`（`PluginConfigDialog.vue:207` 使用）。所以页面里的 `model: "text"` 只被当成普通 DOM attr，输入内容无人读取。
- **影响**：用户在插件详情页输入并点「加入任务」时，实际发出 `POST /api/v1/plugin/Av115Scraper/task` 且 body 为 `{}`；`_read_body`（`:879-892`）得到空 payload → `_payload_text`（`:906-923`）返回 `[]` → `_accept_text("")`（`:416-437`）返回 `{"ok": false, "error": "没有收下任何番号、磁力或 ed2k"}`。任务列表不变，用户以为已提交。唯一可用入口退化为「配置 → 番号/磁力/ed2k → 保存」（`:243-246`、`:143-147`）。
- **建议修复**：三选一——(a) 删掉 `get_page` 里的输入框/按钮，改成「去配置页填写」引导（成本最低、与现状一致）；(b) 若确实要详情页提交，改用 Vue 联邦页面（`get_render_mode() == "vue"`）自行绑定状态；(c) 若坚持 Vuetify JSON 页面，需要前端支持的可携带页面状态的事件约定（当前 v2.15.6 的 `PageRender` 没有，标「待确认」），不能只写 `api/method`。同时修正 `:315-321` 的文案。
- **可验证方式**：打开插件详情页，输入 `SSIS-001` 点「加入任务」，浏览器 Network 中该 POST body 为 `{}`，响应 `ok:false`；`get_data("tasks")` 前后不变。服务端单测也可覆盖：直接以空 body 调用 `api_add_task` 断言 `accepted=[]`。

#### D-P1-02 · 播放默认模板写死作者自己的公网域名与内网 IP，并会写入新用户配置

- **证据（插件侧）**：
  - `plugins.v2/av115scraper/av115_play.py:26-29` `WAN_PLAY_BASE = "https://mp.yuanhot.top/api/v1/plugin/Av115Scraper/redirect?..."`；`:30-33` `LAN_PLAY_BASE = "http://192.168.1.9:3000/api/v1/plugin/Av115Scraper/redirect?..."`；`:22-25` 旧默认同样指向 `mp.yuanhot.top` 上的 P115StrmHelper。
  - `__init__.py:69-71` 类级默认、`:99-130` 首次 `init_plugin` 在配置缺省时把上述常量写回配置（`config["play_base"] = self._play_base`；`play_base_lan`/`lan_networks`/`strm_use_lan` 缺省时 `update_config` 持久化）；`get_form` 的模型默认值 `:289-292` 同值；`_VIDEO_NOTE` `:40-48` 也直接写「内网播放走 http://192.168.1.9:3000 … 外网播放走 https://mp.yuanhot.top」。
  - `README.md:11-12` 把这两个作者环境地址作为「默认」列给读者，`:5`、`:18`、`:28`、`:30` 也全是 `192.168.1.9` / `mp.yuanhot.top`。
- **影响**：新装用户只要不手工改配置，就会得到这两个**别人的地址**：默认 `strm_use_lan=True`，扫描生成的 strm 写 `http://192.168.1.9:3000`——对任何非作者网段都不可达（内网播放直接失败）；一旦按 README 为外网好友关掉 `strm_use_lan`（`README.md:30`），strm 改为 `https://mp.yuanhot.top`（作者/第三方的 MoviePilot 公网入口），播放时客户端会把 17 位 pickcode 与 `file_name`（片名）作为 URL 参数请求该域名——**文件标识和片名泄漏给域名所有者**，而该域名没有该用户 115 的 Cookie，正常 404/502，播放仍然失败。也就是说默认值在「谁的环境」下都不对，且会把用户请求导向第三方。
- **建议修复**：两个模板默认留空（`run_scan` 已有「没有填写播放地址」的拦截，见 `av115_scan.py:175-177`，可直接复用），在首次保存/首次运行时提示必须填写；把 `_VIDEO_NOTE` 和 README 改成通用占位（如 `https://你的MP域名/api/v1/plugin/Av115Scraper/redirect?...`）；若保留迁移旧常量（LEGACY）可以，但迁移目标必须是用户自己的域名而不是作者域名。
- **可验证方式**：清空插件配置后触发一次 `init_plugin`，读 `get_plugin_config("Av115Scraper")`，可见 `play_base=WAN_PLAY_BASE`、`play_base_lan=LAN_PLAY_BASE`；跑一遍扫描后打开任意 `.strm`（默认开关下内容为 `192.168.1.9:3000`，关掉「strm 写内网地址」后为 `mp.yuanhot.top`）。

#### D-P1-03 ·（与 `00-L2` / `05-E-P1-05` 同根因，不重复计数）`p115client` 未声明，`/redirect` 播放链路在没有该依赖的环境必然 502

- **证据**：插件目录没有 `requirements.txt`（`plugins.v2/av115scraper/` 仅 9 个 `.py`）；`__init__.py:561` `from p115client import P115Client`（`_download_url`）、`av115_pan.py:77`（`_web()`，建目录/getid）、`av115_pan.py:171`（`add_magnet`）三处运行期依赖。宿主需求 `requirements.in` 中没有 `p115client`（`grep -i p115 requirements.in` 无结果），即 MoviePilot v2.15.6 本身不提供；插件的依赖声明机制是插件目录下的 `requirements.txt`（`app/helper/plugin.py:896-915`），本插件没有。
- **本模块补充的影响面**：播放（`/redirect`）是 302 插件的核心功能，异常被 `__init__.py:400-405` 吞成 `502 resolve failed: No module named 'p115client'`；只有装了 P115StrmHelper（其 `requirements.txt` 当前 pin `p115client==0.0.9.7.2`，见第 1 节外部对照）或其他带 p115client 的插件时才是「能跑」状态。`_cookie_value`（`:584-599`）回退读 P115StrmHelper 的 cookie 也说明作者环境里 P115StrmHelper 存在——这是环境巧合，不是声明。
- **建议修复 / 验证方式**：见 `00-L2`、`05-E-P1-05`（新增 `plugins.v2/av115scraper/requirements.txt`、README 写明、`import` 失败转可读错误）。本文件不重复展开。

### 2.3 P2

#### D-P2-01 · `X-Forwarded-Host` 被外层反代固定成内网地址时，302 会自己跳自己（回环防护有缺口）

- **证据**：`av115_play.py:143-145` 用 `_apparent_host(host, forwarded_host)` 决定「当前入口」；`:221-224` `_apparent_host` **无条件优先** `X-Forwarded-Host`；`:170-193` `_should_send_wan` 的放行条件只要求 `forwarded_host` 非空（`:192`）。实测（`/tmp/t_play.py`）：
  ```
  host=mp.yuanhot.top, X-Forwarded-Host=192.168.1.9:3000, X-Forwarded-For=8.8.8.8
  hop1 -> https://mp.yuanhot.top/api/v1/plugin/Av115Scraper/redirect?pickcode=...&file_name=x.mp4
  hop2 -> （同一组 header 再请求）完全相同的 URL      # hop1 == hop2
  ```
  只要外层反代把 `X-Forwarded-Host` 写死成内网入口（例如 `proxy_set_header X-Forwarded-Host 192.168.1.9:3000;`）而 `Host` 是公网域名、`X-Forwarded-For` 是公网地址，就会无限 302。`README.md:20` 要求「公网反代要保留原始域名（X-Forwarded-Host 或 Host）」，但配置写错时的失败模式是死循环（浏览器/Emby 报 too many redirects、日志刷屏），不是降级。
- **影响**：特定但真实的反代配置下，所有经该入口的播放请求 302 死循环，115 直链永远取不到。
- **建议修复**：302 前做自跳保护——计算目标 URL 的 origin 后与本次请求的 `Host` 和 `X-Forwarded-Host` 分别比较，任一命中就不跳；或 `_apparent_host` 采信 XFH 时要求它确实是 `play_base`/`play_base_lan` 之一的 origin，否则回退 `Host`。
- **可验证方式**：构造上述 3 个 header 请求 `/redirect`，断言 `Location` 不等于请求 URL；修复前 `hop1 == hop2` 可复现循环。

#### D-P2-02 · 内网判定只信 `X-Forwarded-For` 首跳；Docker 部署下该分支实际不可达，LAN 反代漏设 XFF 时会把外网好友当内网

- **证据（插件侧）**：`av115_play.py:196-218` `_client_is_lan`——有 XFF（或 X-Real-IP）就用 `_first_ip()`（`:234-247`，取最左一项）判断，完全无转发头才看 `peer`；`:211-215`。`host_matches(host, wan_origin)` 的反代保护（`:216-217`）只在「peer 是私网且无 XFF」时才可能生效。
- **证据（宿主部署）**：MoviePilot 官方 Docker 镜像必经自带 nginx，`docker/nginx.common.conf:60-74` 的 `location /api` 无条件 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`（`:72`）、`proxy_set_header Host $http_host;`（`:73`）、`X-Real-IP`（`:71`）。因此后端收到的 XFF **永远非空**：直连 LAN 客户端时首跳是真实内网 IP（判定正确）；外层反代在 LAN 内且没设置 XFF 时，MP nginx 注入的 XFF 就是反代地址（私网）→ 外网好友被误判为内网 → `redirect_location` 第一分支（`:146-155`）把他们 302 到只有内网能访问的 `play_base_lan`，播放失败。插件注释里「公网域名上的对端是私网地址又没有 X-Forwarded-For，多半是反代」的保护（`:206-210`）在官方部署形态下几乎不可触发。
- **附带事实**：MP nginx 只**追加**、不清理客户端自带 XFF（`$proxy_add_x_forwarded_for`），首跳可由请求方伪造（`curl -H 'X-Forwarded-For: 192.168.1.20' https://公网入口/...` 会被 302 到内网模板）。服务端不会去请求该内网地址，且重定向目标由管理员配置，所以影响仅是「外网请求被引导到内网入口」（低），但说明「谁都能宣称自己是内网」。
- **影响**：README 把 XFF 当成交付前提（`README.md:20`），但插件不能验证、也不能区分反代地址与真实客户端；漏配时会静默把公网用户跳到内网地址。
- **建议修复**：增加 `trusted_proxies`（可信反代网段）配置：若 XFF 最左地址命中可信反代 CIDR，则继续取下一跳；否则把首跳当作客户端。或至少把「外层反代必须设置 X-Forwarded-For」写成安装必做步骤并在日志里提示。
- **可验证方式**：`curl -H 'X-Forwarded-For: 10.1.2.3' 'https://<公网>/api/v1/plugin/Av115Scraper/redirect?pickcode=<17位>'` 观察 302 到 `play_base_lan`；在 LAN 反代后模拟不带 XFF 的请求，观察 `_client_is_lan` 判定。

#### D-P2-03 · 播放模板无校验：非法端口/IPv6 让 `/redirect` 500，缺 scheme 时产生相对 Location

- **证据**：`av115_play.py:76-82` `origin_of()` 直接读 `urlsplit(...).hostname/.port`，未捕获异常；`:122-145` `redirect_location` 在入口就调用它；`__init__.py:385-396` 在 `api_redirect` 里**没有 try**，`:400-405` 的 try 只包住 `_download_url`。实测：
  ```
  play_base=https://mp.yuanhot.top:99999/...  -> ValueError: Port out of range 0-65535
  play_base=http://[::1/...                   -> ValueError: Invalid IPv6 URL
  play_base_lan=192.168.1.9:3000（无 scheme） -> 返回 192.168.1.9:3000/redirect?...（相对 Location）
  ```
  `init_plugin`（`:84-154`）对 `play_base` / `play_base_lan` / `lan_networks` 均未做任何校验（`parse_networks` 对坏 CIDR 是静默跳过，`av115_play.py:57-73`）。
- **影响**：管理员把模板填错后，所有 `/redirect` 请求 `500 Internal Server Error`（FastAPI 未捕获异常），且只有一行默认 traceback，不指向「模板配置错误」；缺 scheme 时返回相对 Location，客户端会拼到当前域名下，可能 404 或跳错。`lan_networks` 全填错时会静默回退默认私网（`parse_networks`），不会提示。
- **建议修复**：在 `init_plugin` 校验两侧模板（`scheme ∈ {http,https}`、hostname 可解析、端口 0-65535、含 `{pickcode}` 或可拼接），非法时禁用分流/置空并记录明确日志；`api_redirect` 内层再包一层 try，`ValueError` 时降级为直接解析 115 直链，不要 500。
- **可验证方式**：保存 `https://host:99999/` 后请求 `/redirect?pickcode=<17位>`，当前 500；修复后应为 302/400 且日志可读。

#### D-P2-04 · 未约束 `p115client` 版本，`download_url(..., app=...)` 依赖已在新版签名中被移除的参数（待确认）

- **证据（插件侧）**：`__init__.py:577`
  `url = str(P115Client(cookie, app="android").download_url(pc, user_agent=ua, app="android"))`。
- **证据（p115client 源码，非本仓库）**：
  - P115StrmHelper 当前 pin 的 `p115client==0.0.9.7.2` 中 `download_url(self, pickcode, /, strict=True, user_agent=None, app: str = "os_windows", *, async_=..., **request_kwargs)`，**有 `app` 形参**（PyPI 包 `p115client/client.py:8683-8703`），因此当前作者环境可用；
  - p115client GitHub main 分支（晚于已发布版本）同名方法只剩 `download_url(self, pickcode, /, strict=True, user_agent=None, *, async_=..., **request_kwargs)`（`client.py:2872-2904`），`app` 会落进 `**request_kwargs` 并一路传给底层 `request(...)`，预期 `TypeError` → 被 `api_redirect` 吞成 502。
- **影响**：插件不声明 p115client 版本（同 D-P1-03），任何一方（用户手动、其他插件、未来 P115StrmHelper）把 p115client 升级到移除 `app` 的版本后，`/redirect` 播放会静默失败。当前生产可用依赖 P115StrmHelper 恰好 pin 0.0.9.7.2。
- **建议修复**：新增 requirements 时声明兼容版本（与 P115StrmHelper 的 pin 对齐，避免两个插件抢版本），或调用处先按带 `app` 调用、`TypeError` 再退化为 `download_url(pc, user_agent=ua)`；用 mock 覆盖两种签名。
- **可验证方式**：在 p115client main/未来版本的临时 venv 里请求 `/redirect`（预期 502）；或在单测里用 `inspect.signature`/`TypeError` 注入断言降级路径。**状态：待确认**（无法确认目标用户实际安装版本）。

#### D-P2-05 · Emby 刷新只处理「第一个模块实例的第一个命中库」，多 Emby 实例会漏刷（私有方法风险另见 `00-L4`）

- **证据**：`__init__.py:702-725`。外层 `for module in ModuleManager().get_running_type_modules(ModuleType.MediaServer)`（`:702`），内层 `for name, server in (module.get_instances() or {}).items()`（`:705`）；无论 `target is not None`（`:716-719`）还是没找到库（`:720-722`）都**立即 return**，不会继续遍历剩余实例。即：用户配了两台 Emby、目标媒体库在第二台时，第一台会被 `refresh_root_library()` 全量刷新然后返回「没有找到名为 AV 的媒体库，已通知第一台刷新全部媒体库」，第二台完全不动；另外失败文案无法区分「没有这个库」与「库存在但私有刷新方法不存在」。
- **宿主侧事实**：`EmbyModule.get_instances()` 返回全部已启用 Emby 实例（`app/modules/__init__.py:143-150`），`get_librarys()`（`app/modules/emby/emby.py:147-190`）返回带 `id`/`name` 的库列表，所以多实例是正常部署形态。
- **建议修复**：先收集所有实例的目标库，逐个刷新并记录每台结果；没有目标库的实例再决定是否 `refresh_root_library()`（或直接不刷）；返回内容区分「已刷新 / 找不到库 / 刷新失败」。
- **可验证方式**：Mock `ModuleManager().get_running_type_modules` 返回两个假 `EmbyModule`（各自 `get_instances()` 一台），断言两台都被调用；当前实现只调用第一台。

#### D-P2-06 · `wrote` 判定发生在 items 截断为 200 条之后，>200 部影片时可能漏刷 Emby

- **证据**：`__init__.py:683-690`：`:683` `payload = outcome.as_dict()`（全量 items，`av115_scan.py:55-65`），`:685` 先 `payload["items"] = payload["items"][:200]`，`:686` 才 `wrote = any(item.get("wrote") for item in payload["items"])`，`:687` 依据 `wrote` 决定是否刷新 Emby。若本轮扫描 >200 部、写入集中在第 201 条之后（或前 200 条全是跳过/失败），`wrote=False` → Emby 不刷新，`last_run` 也只留前 200 条。
- **影响**：大库（常见几百部）第一次全量生成 strm 后 Emby 不刷新，用户看到「已生成但 Emby 里没有」，需要再手动跑一轮或手动刷新。
- **建议修复**：在截断前计算 `wrote`（或让 `run_scan` 返回显式 `wrote_count`），截断只影响展示数据。
- **可验证方式**：Mock `run_scan` 返回 201 条 items、仅最后一条 `wrote=True`，断言 `_refresh_emby_library` 不应被调用（当前实现不会调用）。

#### D-P2-07 · strm 里的 URL 编码不规范：文件名中的空格、`[]` 未编码

- **证据**：`plugins.v2/av115scraper/av115_library.py:26-34`（`build_play_url`）：`quote(filename, safe=".-_()[] ")`（`:30`）把空格和方括号列入 safe。实测生成的 strm 内容：
  ```
  ...&file_name=SSIS-001 title.mp4          # 裸空格
  ...&file_name=ABP-123 [C].mp4             # 裸 []
  ...&file_name=a%23b%26c%3Dd.mp4           # # & = 已正确编码
  ```
  `#`/`&`/`=`/`%`/中文都已编码（`:29-30` 的 `quote` 生效），问题只在 safe 集合里的 `空格` 与 `[]`。RFC 3986 中空格不是合法 URL 字符，`[`/`]` 仅允许出现在 host，query 里应编码。第二跳的 302 Location 由 Starlette `RedirectResponse` 会再 quote（`__init__.py:399`），所以真正风险在**第一跳 strm 文件**。
- **影响**：文件名带空格时（`rename` 失败保留原名、或用户目录里原本就有空格），部分播放器/Emby 解析 strm URL 可能截断在空格处（变成 `file_name=SSIS-001`），或直接认为 URL 非法；`file_name` 目前只用于透传到 115 请求的可选参数，影响有限但属于明确的不规范编码。
- **建议修复**：`safe=".-_()"`（去掉空格与 `[]`），让空格编码为 `%20`；或统一用 `safe=".-_"`。
- **可验证方式**：`build_play_url(WAN_PLAY_BASE, "a"*17, "SSIS-001 title.mp4")` 断言结果含 `%20` 且不含裸空格；用 `urllib.parse.urlsplit` / `httpx.URL` 解析生成结果不报错。

#### D-P2-08 · `/task` 的鉴权说明与宿主 `verify_token` 实际行为不一致（按页面文案调用会 401）

- **证据（插件侧）**：`__init__.py:181-188` `/task` 的 `auth: "bear"`；`:315-318` 详情页文案「机器人调用 POST /api/v1/plugin/Av115Scraper/task，请求头带 API 令牌」；README 没有说明请求头名称（`README.md:40`）。
- **证据（宿主侧）**：`app/api/endpoints/plugin.py:157-175`：`auth_mode == "bear"` 时挂 `Depends(verify_token)`。`app/core/security.py:254-294` 的 `verify_token`：`Authorization: Bearer <token>` 只按 **JWT** 校验（`__verify_token(purpose="authentication")`），API Key 只能走 `X-API-KEY` 请求头（`:63-73`，`api_key_header` 优先）或 `apikey`/`token` 查询参数（`:37-40`）。用 `Authorization: Bearer <API_TOKEN>` 调 `/task` 会 401。
- **影响**：机器人/第三方按页面文案（「请求头带 API 令牌」）集成时不知道要发 `X-API-KEY`，第一次调用就 401；这与 `/run`、`/status`（`auth: "apikey"`，`:166-180`）也不同，容易混淆。
- **建议修复**：把文案和 README 改成明确示例：`X-API-KEY: <MoviePilot API_TOKEN>`（或 `?apikey=<API_TOKEN>`）；如希望机器人用 `Authorization: Bearer`，则 `/task` 改 `auth: "apikey"`（浏览器 JWT 场景可改用页面/表单入口）。**状态：建议待确认机器人调用方实际使用的头。**
- **可验证方式**：`curl -H "Authorization: Bearer <API_TOKEN>" -X POST .../task` → 401；`curl -H "X-API-KEY: <API_TOKEN>" -X POST .../task` → 200。

---

### 2.4 其它观察（未分级，P3 级建议）

| 编号 | 事项 | 证据 | 说明/建议 |
| --- | --- | --- | --- |
| O-1 | `normalize_play_base()` 是死代码，迁移逻辑在 `init_plugin` 里另写一份 | `av115_play.py:40-45` 仅被 `tests/test_av115_play.py` 使用；`__init__.py:99-110` 用 `play_base_migrated` 条件做迁移 | 两处对 LEGACY 常量的处理条件不同（`normalize` 无条件替换，`init_plugin` 迁移过一次后保留），建议删除 `normalize_play_base` 或让 `init_plugin` 复用它 |
| O-2 | `interval_seconds` 三处默认值不一致 | 类属性 `:80` = 3；表单默认 `:300` = 3；缺失回退 `:138` = 8 | 老配置缺该键时实际是 8，与表单显示不一致；统一为 3 |
| O-3 | URL 缓存无上限/无负缓存/懒初始化竞态 | `__init__.py:565-582`；`hasattr` 判断后建 `_url_cache`/`_url_lock`（多线程可各建一份） | 建议 `OrderedDict` 限长 + 过期清理；失败结果短期缓存避免反复打 115；600s 复用直链依赖 115 直链 `t`（过期）与 `c`（打开次数）都足够宽，**待确认** |
| O-4 | 502 把内部异常原文回给匿名调用方 | `__init__.py:400-405` | 已有 `05` 的匿名暴露面条目，此处只补充：`str(exc)` 可能含路径/依赖名 |
| O-5 | `package.v2.json` 的 `description` 与类属性 `plugin_desc` 不一致 | `package.v2.json:4`（"识别 115 里的…"）vs `__init__.py:53`（"接收番号、磁力或 ed2k，扫描 115 里的…"） | 市场页与已安装插件页文案不同，P3；建议统一 |
| O-6 | pickcode 长度硬编码 17 | `__init__.py:377` `len(pickcode) == 17 and pickcode.isalnum()` | 与上游 P115StrmHelper 的 302 服务完全一致（`p115strmhelper/api.py:816` 同样 `len==17 and isalnum`），当前 115 文件 pickcode 结构 = 前缀 1 + base36(id) 12 + 固定后缀 4 = 17（`p115pickcode` 0.0.5.4），**当前正确**；未来 id 超过 36¹²（约 4.7e18）或目录类 `fa..fe` 前缀（2 字符，总长 18）会被 400 拒绝，属未来风险，**待确认** |
| O-7 | 非法 cron 只回退空列表 + 一行 warning | `__init__.py:207-223` | 降级本身正确（不会抛异常），但 UI 仍显示插件「已启用」，用户不知道定时没生效；01/00 的时区问题见 `00-L5` |
| O-8 | 本模块无测试覆盖 | `tests/` 无 `api_redirect` / `_download_url` / `_refresh_emby_library` / `get_api` 契约用例 | 与 `05` 的覆盖率结论一致，建议补 4 个最小单测：pickcode 校验、302 分流矩阵、`wrote` 判据、Emby 多实例 |

---

## 3. 已确认正常（逐项核对）

### 3.1 宿主契约逐项核对（MoviePilot v2.15.6）

| # | 契约项 | 插件侧 | 宿主侧证据（`/workspace/moviepilot` 相对路径） | 结论 |
| --- | --- | --- | --- | --- |
| 1 | `_PluginBase` 抽象方法/属性齐全 | `init_plugin`、`get_state`、`get_api`、`get_form`、`get_page` 全部实现；`plugin_name/desc/icon/version/author/order/auth_level/config_prefix` 均声明 | `app/plugins/__init__.py:21-139`（抽象方法定义）、`:233-302`（`stop_service/update_config/get_config/save_data/get_data` 实现） | ✅ 匹配 |
| 2 | `get_form` 返回 `(components, model)`，宿主合并已存配置 | `__init__.py:225-303` | `app/api/endpoints/plugin.py:649-677`（`conf, model = get_form()`；`merged_model = {**model, **stored_config}`） | ✅ 匹配；VForm/VRow/VCol/VSwitch/VTextField/VTextarea/VAlert 由前端 `FormRender.vue:16-50` 的 `model→modelValue` 机制绑定，字段可正常保存 |
| 3 | `get_page` 返回 `List[dict]`，宿主包装为 `{render_mode, page}` | `__init__.py:305-354` | `app/api/endpoints/plugin.py:681-700` | ✅ 页面结构可渲染（**但提交按钮无效，见 D-P1-01**） |
| 4 | `get_api` 的 path/auth/methods/allow_anonymous | `:163-205`；`/redirect` 为 `GET+HEAD` 且 `allow_anonymous: true` | `app/core/plugin.py:873-903`（拼 `/{plugin_id}` 前缀、缺省 auth=apikey）；`app/api/endpoints/plugin.py:152-183`（`allow_anonymous` 优先，不挂鉴权；`bear`→`verify_token`，其它→`verify_apikey`；同时注册 `/api/v1` 与 `/api/v2`） | ✅ 匹配：`/redirect` 匿名可访问、`GET/HEAD` 均可注册、`/task` 的 Bearer 校验可用（文案问题见 D-P2-08） |
| 5 | `get_service` 返回 CronTrigger 实例 | `:207-223` 用 `CronTrigger.from_crontab(self._cron)`，禁用/空 cron 返回 `[]`，异常回退 `[]` | `app/scheduler.py:1359-1379`（`add_job(self.start, service["trigger"], ...)` 接受 Trigger 实例）；`requirements.in:27` `APScheduler~=3.11.2`（3.x 有 `from_crontab`） | ✅ 匹配；时区建议见 `00-L5` |
| 6 | `get_command` 返回命令列表 | `:159-161` 返回 `[]` | `app/core/plugin.py:844-871`（`get_plugin_commands` 容忍空列表） | ✅ 匹配 |
| 7 | `init_plugin` / `stop_service` 生命周期 | `:84-154` 每次配置写入后重新绑定 `_cancel` 并启动 watcher；`:356-359` `stop_service` 只 `set()` 取消事件 | `app/core/plugin.py:72-118`（start→`init_plugin(get_plugin_config)`）、`:121-139`（配置保存后 `init_plugin`）、`:156-192`（stop→`stop_service`）、`app/api/endpoints/plugin.py:976-989` | ✅ 匹配（watcher 线程问题见 `00-L6`，不属宿主契约问题） |
| 8 | `update_config` / `save_data` / `get_data` 语义 | `update_config(config)` 写回迁移后的配置；`save_data("last_run"/"tasks")`、`get_data(...)` | `app/plugins/__init__.py:239-247`（`systemconfig.set("plugin.<ClassName>")`，与 PluginManager `_config_key="plugin.%s"` 一致，`app/core/plugin.py:51`）、`:269-302`（`PluginDataOper.save/get_data`） | ✅ 匹配；`last_run`/`tasks` 均为 JSON 可序列化结构 |
| 9 | 热重载清理 | `__init__.py:12-18` 把插件目录插入 `sys.path`，并在 import 时 `del sys.modules["av115_*"]` | `app/core/plugin.py:654-682` `_clear_plugin_modules("Av115Scraper")` 只清 `app.plugins.av115scraper*`；插件自清 `av115_*` 正好补上宿主清不到的部分；`reload` 后重注册 API/服务（`app/api/endpoints/plugin.py:563-573` + `register_plugin`） | ✅ 两段清理组合后热重载有效（删模块不打断旧线程持有的引用，安全） |
| 10 | `StorageChain` / `FileItem`（无 Cookie 时走 `u115` 存储） | `_U115Storage` 用 `get_file_item(storage="u115", path=Path(...))`、`list_files(item, recursion=False)`、`rename_file(item, new_name)`；`_to_cloud` 读 `type/name/size/pickcode/fileid` | `app/chain/storage.py:43-48/94-98/112-116`；`app/modules/filemanager/__init__.py:220-244/326-336/362-372`（storage 必须是 `StorageSchema` 值，`u115` 合法，`app/schemas/types.py:409-417`）；`app/schemas/file.py:47-73`（FileItem 字段齐全） | ✅ 匹配；README 说「存储 ID 保持 u115」措辞略糊（`u115` 是存储**类型**不是配置名），P3 |
| 11 | `ModuleManager` / `ModuleType.MediaServer` / `EmbyModule` | `_refresh_emby_library` 用 `get_running_type_modules(ModuleType.MediaServer)`、`module.get_instances()`、`server.get_librarys()`、`refresh_root_library()`、`getattr(server, "_Emby__refresh_emby_library_by_id")` | `app/core/module.py:129-138`；`app/schemas/types.py:421-433`；`app/modules/emby/__init__.py:12`（`class EmbyModule`）、`app/modules/__init__.py:143-150`（`get_instances`）；`app/modules/emby/emby.py:147-190`（`get_librarys` 返回 `id/name`）、`:577-597`（`__refresh_emby_library_by_id`，类内名改写后即 `_Emby__refresh_emby_library_by_id`，**当前存在**）、`:599-618`（`refresh_root_library`） | ✅ 当前宿主签名/行为全部匹配；私有方法与多实例问题见 `00-L4` / D-P2-05 |
| 12 | 刷新时机：仅 `wrote` 且非 `dry_run` 且开关开启 | `__init__.py:687-690` `if self._refresh_emby and not self._dry_run and wrote:`；dry_run 分支 `:689-690` 写「预览不刷新 Emby」 | 无需宿主契约；`av115_scan.py:250-256` dry_run 直接返回不写盘，行内无 `wrote` | ✅ 逻辑正确（截断问题见 D-P2-06） |
| 13 | `package.v2.json` 元数据 | `version 1.5.8` == `plugin_version` == history 最新键；`name/icon` 与类属性一致；`system_version: ">=2.0.0,<3"`；`level: 1`；`history` 为 dict | `app/helper/plugin.py:159-193`（`SpecifierSet` 校验，当前 `v2.15.6` 满足 `>=2.0.0,<3`）；`app/core/plugin.py:1852-1887`（`level 1` 合法，所有用户可见）；`app/schemas/plugin.py:47`（`history: Optional[dict]`） | ✅ 匹配（`description` 与 `plugin_desc` 文案不一致见 O-5） |
| 14 | 图标 `Moviepilot_A.png` | `package.v2.json:7`、`__init__.py:54` | 宿主 `/workspace/moviepilot` 本身**不含** png 资源（`find . -name '*.png'` = 0）；但宿主 Docker 会解压前端 release 的 `dist.zip` 到 `/public`（`docker/Dockerfile:100-102`），从 `MoviePilot-Frontend` v2.15.6 release `dist.zip` 中实测存在 `dist/plugin_icon/Moviepilot_A.png`，前端 `PluginCard.vue:191-199` 取 `./plugin_icon/<icon>` 正好命中 | ✅ **可确认显示正常**（`05` E-P2-10 的「待确认」可以用这条关闭；官方市场 `MoviePilot-Plugins/icons/Moviepilot_A.png` 也存在） |

### 3.2 播放分流逻辑（`av115_play.py`）已确认正常项

15. **XFF/Host/XFH/Real-IP 常规组合正确**（实测 14 组）：LAN 客户端经公网入口带 `X-Forwarded-For` → 302 到内网模板；WAN 客户端 → 不跳、直接 302 115 直链；LAN 直连内网入口 → 不跳；公网客户端访问内网入口 → 302 公网模板；多级 XFF（`192.168.1.8, 172.17.0.1`）取首跳正确；`::ffff:192.168.1.9` 经 `ipv4_mapped` 归一后正确判 LAN（`av115_play.py:250-258`）；`Host` 大小写/尾点被 `_split_host` 归一（`:99-119`）；`X-Forwarded-Host` 链取首项（`:227-231`）。
16. **同一请求内不会自跳**：`redirect_location` 两个分支互斥（`:146-155` 要求 `on_wan and not on_lan`；`:156-166` 经 `_should_send_wan` 要求 `on_lan` 且非 `on_wan`），`play_base == play_base_lan` 时两个分支都不触发（实测 `test_same_origin_does_not_bounce` 通过）。跨请求回环风险见 D-P2-01。
17. **反代未改写 Host 时的回环保护有效**：`Host` 被改成内网、`X-Forwarded-Host` 缺失、XFF 公网的请求不会被再 302 回公网（`_should_send_wan` `:192` 要求 `forwarded_host` 非空）——`test_rewritten_host_without_forwarded_host_does_not_loop` 通过。
18. **模板选择/迁移基本正确**：`choose_strm_template`（`:48-54`）在 `strm_use_lan` 开且内网模板非空时写内网，否则回退外网/内网；`init_plugin` 迁移 LEGACY 默认一次并持久化 `play_base_migrated`；`parse_networks` 对空值/全非法输入回退内置私网（`:57-73`），实测通过。
19. **`_download_url` 的 UA 语义与缓存 key 正确**：用请求方 UA 生成 115 直链（115 直链 `f=1` 时要求请求 UA 一致，`p115client` 文档），缓存 key 为 `(pickcode_lower, ua)`（`__init__.py:563-581`），缓存 600s；`P115Client(cookie, app="android")` 的 cookie 字符串解析在 0.0.9.7.2 可用（`update_cookies` 接受字符串）。版本兼容风险见 D-P2-04。
20. **`_cookie_value` 的 P115StrmHelper 回退键名正确**：`__init__.py:584-599` 依次尝试 `cookies/cookie/pan_cookie/pan_cookies`；P115StrmHelper v2.8.79 的配置键正是 `cookies`（其 `core/config.py:336`），本机 `PluginManager().get_plugin_config` 返回 `plugin.P115StrmHelper` 配置（`app/core/plugin.py:776-789`），回退链路成立（但说明这是跨插件耦合，不是声明依赖）。
21. **`api_redirect` 的 pickcode/HEAD/异常基本行为符合预期**：`:377` 17 位 alnum 校验对当前 115 文件形态成立（见 O-6，与上游实现一致）；`methods` 含 HEAD（`:200`）；`_download_url` 异常转 502（`:400-405`）；`RedirectResponse(302)` 的 Location 由 Starlette 再编码（`:399`、`:405`）。
22. **`get_api` 与 README 的路径一致**：`/api/v1/plugin/Av115Scraper/{run,status,task,redirect}` 由宿主前缀机制生成（`app/core/plugin.py:873-903`），README `:40`、`:49` 与页面文案一致。
23. **`dry_run` 不写盘、不刷新 Emby**：`av115_scan.py:250-256` dry_run 直接返回、不写文件；`__init__.py:687-690` 刷新被 `not self._dry_run` 拦下。与 README「只预览」表述一致。

---

## 附录 A · 分流矩阵实测（`/tmp/t_play.py`）

`play_base=https://mp.yuanhot.top/...`，`play_base_lan=http://192.168.1.9:3000/...`，`pickcode=abc123pickcodeXYZ`，`filename=SSIS-001.mp4`：

| 场景（host / XFH / peer / XFF / real_ip） | 结果 |
| --- | --- |
| 公网入口 + XFF=192.168.1.20 | 302 内网模板 ✅ |
| 公网入口 + XFF=8.8.8.8 | None（直接取直链）✅ |
| 公网入口 + XFF 多级 `192.168.1.8, 172.17.0.1` | 302 内网 ✅ |
| 内网入口 + peer=192.168.1.20 | None ✅ |
| 内网入口 + peer=8.8.8.8 | 302 公网模板 ✅ |
| 内网入口 + peer=8.8.8.8 + XFH=mp.yuanhot.top + XFF=8.8.8.8 | None（不再跳）✅ |
| 内网入口 + XFF=1.1.1.1 + 无 XFH | None（回环保护）✅ |
| 公网入口 + peer=172.17.0.1 + 无 XFF | None（反代不当内网）✅ |
| real_ip=`::ffff:192.168.1.9` | 302 内网 ✅（IPv4-mapped 归一） |
| XFF=`2001:db8::1` | None ✅ |
| `play_base == play_base_lan` | None ✅（不自跳） |
| **XFF=8.8.8.8 + Host=mp.yuanhot.top + XFH=192.168.1.9:3000** | **返回与当前 URL 相同的公网 URL → 循环（D-P2-01）** |

畸形模板实测（`/tmp`）：

| play_base | 结果 |
| --- | --- |
| `https://mp.yuanhot.top:99999/...` | `ValueError: Port out of range 0-65535` → `/redirect` 500（D-P2-03） |
| `http://[::1/...` | `ValueError: Invalid IPv6 URL` → 500（D-P2-03） |
| `192.168.1.9:3000`（无 scheme） | `origin_of` 为 None，可能返回相对 Location（D-P2-03） |

---

## 附录 B · 版本与文件清单

- 插件版本：`package.v2.json` 1.5.8 = `plugin_version` 1.5.8；最新提交标题含 v1.5.8。
- 宿主版本：`/workspace/moviepilot/version.py:1` `APP_VERSION='v2.15.6'`，`FRONTEND_VERSION='v2.15.6'`。
- 本模块涉及文件：`plugins.v2/av115scraper/av115_play.py`（全 258 行）、`plugins.v2/av115scraper/__init__.py`（934 行中第 84-405、560-725 行）、`plugins.v2/av115scraper/av115_library.py:26-34`、`plugins.v2/av115scraper/av115_scan.py:45-65/175-177/196-256/308-330`、`package.v2.json`、`README.md`。
- 交叉引用：`00-lead-crosscut.md` L2/L4/L5/L6，`05-quality-tests.md` E-P1-05 / E-P2-10（本文件 D-P1-03 与 D-P2-05 不再重复计数；icon 一项可用本文件 3.1 #14 关闭）。
