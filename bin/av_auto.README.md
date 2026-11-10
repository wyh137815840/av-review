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
av_auto submit --code 番号 --url 磁链或ed2k     # 交到 /av-auto/_staging/<番号>/
av_auto wait --code 番号 [--timeout 秒]
av_auto clean --code 番号                      # 只搬正片进 /av-auto/<番号>/，清空残留
av_auto strm <番号...>                         # 只给正片写 strm，不给广告/杂质建
av_auto frames <番号>                          # 加密抽帧 + 默认有效性预检（DeepSeek-flash）
av_auto frames --strm /path/file.strm --cleanup
av_auto frames --no-precheck                   # 只抽不预检（官方库对时点时用）
av_auto review <番号>                          # 仅有效帧双模型终审（DeepSeek-flash + gpt-6-luna）
av_auto intake --code 番号 --url URI [--frames --review --cleanup]  # 不 promote
av_auto promote [--dry-run] <番号...>          # 显式搬家；验证条不要自动调用
```

清杂质：只保留 ≥ min_size_mb（默认 300）的正片大视频（UC/AI/4K 等正片变体可留）；广告包、apk、宣传图、过小 sample/预告/trailer、文件名带广告/推广、嵌套垃圾夹全删。inbox 里若没有达标正片则不搬不删，便于排查。

抽帧：从 2 分钟起约每 8 分钟一帧，再补约 85%（避开片尾），单部最多 8 帧有效帧；某一处 seek 过慢则降级跳过，不堵整批。默认用 DeepSeek-flash 一家做有效性预检：完全没人或完全不色（空景/字幕/胡聊等）判无效，在该点附近换时间重抽，单点最多再试 3 次，仍无效则丢弃该点，不送双模型终审。看不清或预检调用失败先当作有效交给终审，避免误杀。`--cleanup` 删临时帧，保留时间点清单。终审仍是 DeepSeek-flash + gpt-6-luna，不上 MiniMax/Grok，不改 media_llm_route。Magpie Key 由入口脚本临时放入运行时文件，用完删除，不入库。

约束：用 p115client，不用 CloudDrive2；不改 media_llm_route；不刮削。验证条不要自动 promote。
