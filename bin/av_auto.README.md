# av-auto 通路

## 路径
- 115 离线暂存 inbox：`/av-auto/_staging/<番号>/`（p115client 交磁/ed2k 落这里）
- 115 复核 staging：`/av-auto/<番号>/`（清杂质后只留正片；不在 `/AV` 下）
- 115 正式库：`/AV/<番号>/`（只有显式 `promote` 才搬）
- 本地 staging strm：`/mnt/user/media/av-auto/<番号>/`（Emby 未扫；只给正片建 strm）
- 本地正式 strm：`/mnt/user/media/AV/...`（Emby 库 `AV`）

夹命名按番号，与正式库一致，方便整夹搬家。

## 入口
- grokbot-vm: `/home/yuanhao/av-review/bin/av_auto`
- Unraid: `/mnt/user/appdata/findav/bin/av_auto`
- 实际脚本: moviepilot 容器内 `/config/scripts/av_auto.py`

## 用法
```
av_auto setup
av_auto status
av_auto submit --code 番号 --url 磁链或ed2k     # 交到 /av-auto/_staging/<番号>/；ed2k 的 | 不会被 ssh 拆开
av_auto wait --code 番号 [--timeout 秒]
av_auto clean --code 番号                      # 只搬正片进 /av-auto/<番号>/，清空残留
av_auto strm <番号...>                         # 只给正片写 strm，不给广告/杂质建
av_auto frames <番号>                          # 加密抽帧 + 默认有效性预检（DeepSeek-flash）
av_auto frames --strm /path/file.strm --cleanup
av_auto frames --no-precheck                   # 只抽不预检（官方库对时点时用）
av_auto review <番号>                          # 仅有效帧双模型终审（DeepSeek-flash + gpt-6-luna）
av_auto grok-review <番号> --frames-dir <帧目录>  # 仅 uncertain / 分歧时的 Grok 复审
av_auto process <番号...>                      # 离线已完成后的官方后处理，见下文
av_auto share-floor                            # 只读：配置 floor、shfs 是否早于 media.cfg、可用空间。不 reload
av_auto intake --code 番号 --url URI [--frames --review --cleanup]  # 不 promote
av_auto promote [--dry-run] <番号...>          # 显式搬家；验证条不要自动调用
```

`submit` 和 `intake` 的 `--url` 不会原样拼进 ssh 命令行。ed2k 里的 `|`、magnet 里的 `&` 若被远程 shell 再解析一次，会变成管道或后台命令（退出码 126 这一类）。入口把链接写到 Unraid 上 mode 600 的临时文件，远程用 `URI=$(cat file)` 和 `--url "$URI"` 传给 `docker exec`，用完删除。标准输出和 ssh 命令行都不打印链接。

## 离线完成后：`process`

115 离线已经落到 `/av-auto/_staging/<番号>/` 之后，只用这一条。命令在前，番号在后。可以一次带多个番号。不重新交磁，不刮削，不搬进 `/AV`。

```bash
av_auto process ABP-073
av_auto process ABP-073 ABP-108 ABP-246 ABP-313 ABP-383
```

`av_auto process --help` 也印出上面这条命令。

顺序固定为 clean → strm → frames → review → grok-review。清杂质规则与单独的 `clean` 相同（只留大体积正片，UC/AI/4K 变体可留，广告/apk/宣传/过小 sample 删掉）。抽帧仍是约 2 分钟起、每约 8 分钟一帧、再补约 85%，最多 8 帧，并且默认做有效性预检；没人或完全不色的帧会重抽，仍无效则丢弃，不送终审。终审仍是 DeepSeek-flash + gpt-6-luna。只有 uncertain 或两家分歧才调用 `grok-review`，写同目录 `grok_review.json`，不覆盖 `review.json`。

帧、`review.json` 和若有的 `grok_review.json` 在命令报完成之前写入持久目录 `/config/scripts/av-auto-fs/frames/<番号>/`（宿主机 `/mnt/user/appdata/moviepilot/config/scripts/av-auto-fs/frames/<番号>/`）。容器里的 `/tmp/av-auto-frames/<番号>/` 不会单独算完成；写不进持久目录就是 FAIL。下一轮 process 复用这份持久结果。`frames` / `review` / `grok-review` 单独跑完也会落到同一目录。

已经做过的步骤会跳过：正片已在 `/av-auto/<番号>/`（inbox 夹若还在，只补清残留，不当成失败）、staging strm 已在、预检有效帧已在、双模型 `review.json` 已在、以及未决时对应的 `grok_review.json` 已在。不会因为重跑而再次交离线。

`/mnt/user/media` 返回 ENOSPC（errno 28）时，mkdir / strm 仍写入 `av-auto-fs/pending`（`kind` / `path` / `data` 三个文件，给 Unraid 上的 `av_auto_apply_fs` 用）。官方入口在 python 退出之后一定会跑 `av_auto_apply_fs`，即使 process 非 0；以前 `set -e` 会把这一步跳过，人就得手工 apply。同一轮里若 pending 已有该番号的 strm 内容，抽帧直接用这份内容，不再报「找不到 strm」。若这时 `/media` 已经能写，会先把 pending 落到盘上再抽。

ENOSPC 不再一律说成 shareFloor 没重载，也不把还有大量剩余空间的情况说成磁盘满。日志对照三件事：可用空间、`media.cfg` 的 shareFloor、以及 shfs 启动时间是否早于这份 cfg（或运行中的 floor 是否仍高于配置）。可用空间低于 1GiB 才是磁盘满；可用空间低于配置 floor 是预留，reload 也不会放宽；可用空间不低于配置 floor 且 shfs 还没读到新 cfg，才是过期 floor。`av_auto share-floor` 只打印这个诊断，不会 reload shfs，也不会写盘。process 不会自动 reload。

需要 Magpie 的命令每次使用自己的 `llm_runtime.<运行号>.json`，通过 `AV_AUTO_LLM_FILE` 交给容器，退出时只删自己的文件。重叠的官方 process 不会把对方的运行时文件删掉。Key 仍只从 Magpie caller-keys 临时复制，不写进仓库，也不写进永久脚本。

番号不能当子命令。下面这种写法会立刻失败，并提示正确的 `av_auto process ...`，不会把番号当成 cmd：

```bash
av_auto ABP-073 clean
av_auto ABP-073 process
```

清杂质：只保留 ≥ min_size_mb（默认 300）的正片大视频（UC/AI/4K 等正片变体可留）；广告包、apk、宣传图、过小 sample/预告/trailer、文件名带广告/推广、嵌套垃圾夹全删。inbox 里若没有达标正片则不搬不删，便于排查。

抽帧：从 2 分钟起约每 8 分钟一帧，再补约 85%（避开片尾），单部最多 8 帧有效帧；某一处 seek 过慢则降级跳过，不堵整批。HTTP(S) 上的 fragmented MP4（ftyp + 很小的 moov/mvex + 一串 moof/mdat）不再把 URL 直接交给 `ffmpeg -ss`。那条路径会发不闭合的 `Range: bytes=offset-`，并按渐进式整段 moov 去 seek，源上会报 `partial file`、一个包都写不出；只用闭合 Range 把开头约 2MB 下到本地却能解出帧。`av_auto.py` 与同目录的 `fmp4_http.py` 改成闭合 Range：跟随 redirect 并保住 cookie，用 sidx 或文件尾 mfra（都没有就只读 moof 头、跳过 mdat）定位时间点，只取对应 moof/mdat；分片过大或 sample 偏移是绝对文件位置时，只取该 GOP 的 sample 再在本地拼一个小片段。不整文件下载。默认用 DeepSeek-flash 一家做有效性预检：完全没人或完全不色（空景/字幕/胡聊等）判无效，在该点附近换时间重抽，单点最多再试 3 次，仍无效则丢弃该点，不送双模型终审。看不清或预检调用失败先当作有效交给终审，避免误杀。`--cleanup` 删临时帧，保留时间点清单。终审仍是 DeepSeek-flash + gpt-6-luna，不上 MiniMax/Grok，不改 media_llm_route。uncertain 或两家分歧时另跑 `grok-review`，不要把 Grok 放进默认 `review`。Magpie Key 由入口脚本临时放入运行时文件，用完删除，不入库。

## Grok 复审

`av_auto review` 的模型列表不变，仍然只有 DeepSeek-flash + gpt-6-luna。两家一致 pass 或一致 fail 时不要调 Grok。`review.json` 的共识是 uncertain，或两家一个 pass 一个 fail 时，用单独入口 `grok-review`。批次调度器不在这个仓库里；调度器只在结果未决后调用这条命令。

`grok-review` 只读该目录里已经抽出、且预检为有效的帧，请求级临时指定 `cursor/grok-4.7`。提示词里不写两家的结论，Grok 只看这些帧。不重跑 DeepSeek / gpt-6-luna，不送封面、剧照、Thumb、Backdrop，也不送无效帧或空帧。不刮削，不 promote 进 `/AV`，不改 `media_llm_route.json`，不覆盖 `review.json`。先跑完 `grok-review` 再对 `review` 加 `--cleanup`，否则临时帧会被删掉。若后来重跑 `review` 且两家已经一致，再跑 `grok-review` 不会改已有的 `grok_review.json`，以 `review.json` 的一致结论为准。

写出同目录的 `grok_review.json`。`verdict` / `confidence` / `reason` 是 Grok 的判断。`tiebreak.kept_verdict` 是对照两家之后保留的结论：Grok 给出 pass 或 fail 就保留 Grok；Grok 仍为 uncertain 则本条保持未决。`tiebreak.conclusion` 写明保留的是哪一边。标准输出有一行 `tiebreak KEPT <verdict>`、`tiebreak UNRESOLVED uncertain` 或 `tiebreak SKIP <agreement>/<consensus>`。结果同时落到持久帧目录，不覆盖 `review.json`。

```bash
av_auto grok-review IPX-750 --frames-dir /config/scripts/av-auto-fs/frames/IPX-750
```

外部 runner 可以先判断要不要调（这个函数不发请求）：

```python
from av_auto import grok_tiebreak_hook
hook = grok_tiebreak_hook(review, frames_dir, code)
if hook:
    # hook["trigger"] 为 disagree 或 uncertain
    # hook["argv"] 例如 ["grok-review", "IPX-750", "--frames-dir", "..."]
    ...
```

约束：用 p115client，不用 CloudDrive2；不改 media_llm_route；不刮削。验证条不要自动 promote。

## 插件接口（2026-11-12 起）

`av_auto` 不再直接 import 插件的内部模块，也不再使用 `/config/plugins/av115scraper` 旧副本：

- 只通过插件公开门面 `av115_api`（`API_VERSION=1`，需要 Av115Scraper **>= 1.5.10**）访问 `open_pan`、`safe_name`、`write_library`、`item_id/name/is_dir/size` 等。
- 插件目录按顺序解析：`AV_AUTO_PLUGIN_DIR` → MoviePilot 实际安装副本 `/app/app/plugins/av115scraper` → 旧本地副本（仅兼容）。
- 门面版本过旧或缺失时会直接报错，不再静默 import 失败。
