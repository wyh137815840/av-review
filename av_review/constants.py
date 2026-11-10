from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

UNRAID = "root@192.168.1.9"
UNRAID_HOST = "192.168.1.9"
EMBY = "http://192.168.1.9:8096"
MAGPIE = "http://192.168.1.9:3430"
FINDAV_DB_CONTAINER = "findav-db"

PROFILE_REMOTE = "/mnt/user/appdata/findav/profile"
ROUTE_REMOTE = PROFILE_REMOTE + "/media_llm_route.json"
USER_PROFILE_REMOTE = PROFILE_REMOTE + "/user_profile.json"
EMBY_ENV_REMOTE = "/mnt/user/appdata/findav/emby.env"
CONNECTION_ENV_REMOTE = "/mnt/user/appdata/findav/connection.env"
MAGPIE_KEYS_REMOTE = "/mnt/user/appdata/magpie/config/magpie/caller-keys.json"
USAGE_REMOTE = "/mnt/user/appdata/magpie/config/magpie/usage.jsonl"
AV_ITEMS_REMOTE = PROFILE_REMOTE + "/av_items_raw.json"
MEDIA_AV_REMOTE = "/mnt/user/media/AV"

# Expected media_llm_route.json fingerprint (must not be written by this tool)
EXPECTED_ROUTE_MD5 = "62746acf6786ad824279e2ba12fef646"
EXPECTED_ROUTE_MTIME_PREFIX = "2026-11-10 09:05:53"

HOME = Path("/home/yuanhao/av-review")
XFER = Path("/home/yuanhao/agent-xfer")
SECRETS = HOME / "secrets"
RUN_DIR = HOME / "run"
LOG_DIR = HOME / "logs"
# Work dirs MUST be distinct from the in-flight rest scan (/tmp/rest-vision-*)
WORK = Path("/tmp/av-review-work")
UNRAID_WORK = "/tmp/av-review-work"
FF_BATCH_REMOTE = "/tmp/av_review_ff_batch.sh"
FF_BATCH_LOCAL = HOME / "bin" / "av_review_ff_batch.sh"

VISION_MODELS = ["deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna"]
GROK_MODEL = "cursor/grok-4.7"
FORBIDDEN_MODELS = ["minimax-cn/", "minimax/", "MiniMax"]

HARD_SKIP = frozenset({"HPET-022", "MFYD-175"})

# Never overwrite these historical result files
PROTECTED_BASENAMES = frozenset({
    "av_mismatch_scan.json", "av_mismatch_scan.md",
    "av_mismatch_vision_pilot5.json", "av_mismatch_vision_pilot5.md",
    "av_mismatch_vision_mid.json", "av_mismatch_vision_mid.md",
    "av_mismatch_vision_low.json", "av_mismatch_vision_low.md",
    "av_mismatch_vision_grok4.json", "av_mismatch_vision_grok4.md",
    "av_mismatch_vision_grok_low5.json", "av_mismatch_vision_grok_low5.md",
    "av_mismatch_vision_rest.json", "av_mismatch_vision_rest.md",
    "media_llm_route.json",
    "user_profile.json", "user_profile.md",
    "av_items_raw.json",
    "emby_raw_stats.json",
})

# Files whose item codes count as "already vision-scanned"
VISION_RESULT_PREFIXES = (
    "av_mismatch_vision_pilot5",
    "av_mismatch_vision_mid",
    "av_mismatch_vision_low",
    "av_mismatch_vision_grok4",
    "av_mismatch_vision_grok_low5",
    "av_mismatch_vision_rest",
    "av_review_vision_",
)

API_HOST_DEFAULT = "0.0.0.0"
API_PORT_DEFAULT = 18765
API_TOKEN_PATH = SECRETS / "api.token"

BUSY_PATTERNS = (
    "run_rest_scan.py",
    "run_rest_scan_fast.py",
    "run_rest_repair_last16.py",
    "run_mid_scan.py",
    "run_low_scan.py",
    "run_grok4_review.py",
    "run_grok_low5_review.py",
    "av_review.*review",
    "python3 -m av_review review",
    "python3 -m av_review daily",
)

PROVEN_SYSTEM = (
    "你是影视库筛选助手。用户偏好：身材要好、不要干扁；不要显老，要年轻、皮肤白、水嫩；"
    "预览/封面不要狰狞表情（「色不是难受」）。"
    "色情题材、裸露、性行为本身不判负。"
    "只根据所附图片里女优的可见外观判断，不要因为片名、类型标签（如人妻、熟女）本身判负。"
    "判定维度仅限：dry_thin（身材干扁）、looks_old（显老）、poor_skin（皮肤差/暗沉、明显不白不水嫩）、"
    "grimace（狰狞或难受表情，含看起来痛苦的夸张白目/失神）。"
    "スレンダー不要单独判成干扁；人妻/熟女/老片/蜜色不单独判负。"
    "只输出一个 JSON 对象，不要 Markdown，不要其它文字。字段："
    '{"verdict":"pass|fail|uncertain","hits":[],"reason":"一句短理由","confidence":"high|mid|low"}。'
    "verdict=fail：至少一项维度在图中较明确；pass：未见明显不符；uncertain：看不清、P图/角度限制、审核/无图或证据冲突。"
)

CATALOG_JUDGE_SYSTEM = (
    "你是片单初筛助手。根据元数据（番号、演员、已有评分/分级、厂牌）判断该条目是否值得按用户画像收录。"
    "用户偏好：好身材、年轻、皮肤白水嫩；不要干扁、显老、狰狞难受预览。"
    "スレンダー不要单独判成干扁；人妻/熟女/老片/蜜色不单独判负；色情题材本身不判负。"
    "厂牌/女优偏好只作加分，不是唯一依据。没有封面时不要假装看过外观。"
    "只输出一个 JSON 对象，字段："
    '{"verdict":"pass|fail|uncertain","hits":[],"reason":"一句短理由","confidence":"high|mid|low"}。'
)

CHUNK_SIZE = 16
MAX_WORKERS = 3
MAX_RETRIES = 3
PROBE_TIMEOUT = 90
FRAME_TIMEOUT = 180
