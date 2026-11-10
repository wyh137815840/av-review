# av_review — findav 正式入口

在 **grokbot-vm** 上跑（`/home/yuanhao/av-review`）。镜像副本在 Unraid  
`/mnt/user/appdata/findav/bin/av-review/` 与 `/home/yuanhao/agent-xfer/av-review/`。

复用已跑通的 mid / low / rest 识图流程（Primary+Thumb+Backdrop[0] 800q80 data URI；`.strm` 经 1 跳 115 CDN 后在 embyserver 内 ffprobe，再抽 180s / 900s / 中后段）。默认模型：`deepseek/deepseek-flash` + `chatgpt-api/gpt-6-luna`。禁止默认 MiniMax / Grok。不改 `media_llm_route.json`。不删库、不移库、不动原片、不自动交磁/入库。Key 只从 Magpie `caller-keys.json` 与 findav `emby.env` / `connection.env` 读取，不进 URL、不进日志、不进结果文件。

工作目录是 `/tmp/av-review-work`，与正在跑的 `/tmp/rest-vision-*` 隔离。结果写到 `/mnt/user/appdata/findav/profile/` 新文件名（`av_review_vision_YYYYMMDD_HHMMSS.*`、`av_catalog_missing_YYYYMMDD_HHMMSS.*` 及 `av_catalog_missing_latest.*`），**不会覆盖** `av_mismatch_scan`、`av_mismatch_vision_{pilot5,mid,low,grok4,grok_low5,rest}`。

## 命令

```bash
/home/yuanhao/av-review/bin/av_review --help
/home/yuanhao/av-review/bin/av_review health
```

### 1) Emby 已入库复查

默认跳过已在 mid/low/pilot/rest/grok / 以往 `av_review_vision_*` 里出现过的番号，并跳过 HPET-022、MFYD-175。

```bash
# 只出计划（不取图、不抽帧、不调 Magpie）
/home/yuanhao/av-review/bin/av_review review --dry-run
/home/yuanhao/av-review/bin/av_review review --limit 0

# 指定番号（真正识图前请确认没有其它识图在跑）
/home/yuanhao/av-review/bin/av_review review --codes IPX-750,MIDV-786

# 限制条数
/home/yuanhao/av-review/bin/av_review review --limit 8 --skip-scanned

# 仅当明确需要时，才对两家分歧加 Grok 复审（默认关）
/home/yuanhao/av-review/bin/av_review review --codes DASS-056 --grok-review
```

### 2) 库外候选（findav 有、Emby 还没有）

findav 数据库是 Unraid 上的 MariaDB 11.4（容器 `findav-db`，端口 3316，库名 `findav`，表 `av_title` 等）。`emby_item_id` 目前全空，对照按番号。默认先出短名单，**不调 Magpie、不交磁**。规则预筛（厂牌/女优偏好、beauty_score）只用于排序，不是最终判断。

```bash
/home/yuanhao/av-review/bin/av_review catalog              # 写短名单，待判断
/home/yuanhao/av-review/bin/av_review catalog --dry-run    # 只打印摘要
/home/yuanhao/av-review/bin/av_review catalog --judge      # 文本判断（等识图空档；仍不交磁）
```

最新短名单：`/mnt/user/appdata/findav/profile/av_catalog_missing_latest.md`

### 3) 每日定时（默认 19:30 Asia/Shanghai）

本机已是 CST。crontab 来自 `cron/av-review.cron`：

```
30 19 * * * /home/yuanhao/av-review/bin/av_review daily --skip-if-busy
```

行为：只处理增量复查 + 刷新库外短名单。若当时有识图在跑，则跳过识图和 `--judge`，仍刷新短名单。daily **不会**自动开 Grok，不会交磁。

改周期：编辑 `/home/yuanhao/av-review/cron/av-review.cron`，再执行

```bash
crontab /home/yuanhao/av-review/cron/av-review.cron
```

示例：`30 21 * * *` 改为每天 21:30；`0 20 */2 * *` 隔天 20:00。请继续避开工作日 9:00–12:00 与 14:00–18:00 高峰。日志：`/home/yuanhao/av-review/logs/daily.log`。

手动：`/home/yuanhao/av-review/bin/av_review daily --skip-if-busy --dry-run`

### 4) 局域网 API

默认监听 `0.0.0.0:18765`（本机局域网地址以 `ip -4 addr` 为准；当前为 **192.168.1.17**，不是 192.168.1.19）。  
Token 在 `/home/yuanhao/av-review/secrets/api.token`（600），用请求头传递，**不要放进 URL 或日志**。

```bash
TOKEN=$(cat /home/yuanhao/av-review/secrets/api.token)   # 不要把值贴到聊天
curl -sS http://192.168.1.17:18765/health
curl -sS -H "Authorization: Bearer $TOKEN" http://192.168.1.17:18765/review \
  -H 'Content-Type: application/json' -d '{"dry":true,"limit":0}'
curl -sS -H "Authorization: Bearer $TOKEN" http://192.168.1.17:18765/catalog \
  -H 'Content-Type: application/json' -d '{"dry":true}'
```

- `GET /health`：健康检查（不需要 token；不含密钥）
- `POST /review`：触发复查。`dry` / `limit=0` 为计划-only。真正识图若正忙会 409。
- `POST /catalog`：库外短名单。默认不判断；`"judge":true` 仅空档使用。不交磁。
- `POST /daily`：与 cron 同一套流程。

systemd 用户服务：`av-review-api.service`（linger 已开的话开机自启）。也可用 crontab `@reboot` 调 `bin/av_review-api-start.sh`。

## 约束备忘

- 正在跑的 `/tmp/rest-vision-build/run_rest_scan.py` 不要改、不要杀。
- 不要改 `media_llm_route.json`（期望 mtime 2026-11-10 09:05:53 +0800）。
- 不要手改 MoviePilot 插件源码。
- `av_auto review` 仍然只跑 DeepSeek-flash + gpt-6-luna。双模型 uncertain 或分歧时，用单独的 `av_auto grok-review`（见 `bin/av_auto.README.md`），写同目录 `grok_review.json`，不覆盖 `review.json`，也不要把 Grok 放进默认 review。
