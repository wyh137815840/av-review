#!/usr/bin/env python3
"""115 av-auto staging ↔ official /AV promote, plus strm/frame helpers.

Run inside the moviepilot container (p115client + plugin + /media mounts).
Never print cookies/tokens. Do not change media_llm_route.

Flow for intake: p115client submit into /av-auto/_staging/<code>/ → wait offline
→ junk-clean (keep feature videos only, move into /av-auto/<code>/, delete leftovers)
→ write strm only for keepers → dense-frame from those strm.
Promote is explicit and never automatic.

After offline is already finished, `process` is the only end-to-end entry:
clean → strm → frames → dual-model review → grok-review when uncertain or split.
It does not submit, scrape, or promote. A catalog code is never a subcommand.
Completed frames, review.json, and any grok_review.json are written to
/config/scripts/av-auto-fs/frames/<code>/ before the run reports success.
A container /tmp/av-auto-frames copy alone is not success.

Default final review stays DeepSeek-flash + gpt-6-luna. `grok-review` is a
separate Magpie tiebreaker for uncertain or split results: it pins
cursor/grok-4.7 for that call only, writes grok_review.json, and does not
widen the review ban or touch review.json / media_llm_route.json.
"""
from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from fmp4_http import Fmp4RangeError, open_fmp4_session

PLUGIN_DIR = Path("/config/plugins/av115scraper")
USER_DB = Path(os.environ.get("MP_USER_DB", "/config/user.db"))
STAGING_115_DEFAULT = "/av-auto"
STAGING_STRM = Path("/media/av-auto")
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".iso", ".m4v", ".flv"}
PROBE_TO = int(os.environ.get("AV_AUTO_PROBE_TIMEOUT", "90"))
FRAME_TO = int(os.environ.get("AV_AUTO_FRAME_TIMEOUT", "180"))

STAGING_INBOX = "/av-auto/_staging"
MIN_SIZE_MB_DEFAULT = 300
PREVIEW_WORDS = ("预告", "sample", "trailer", "preview")
JUNK_NAME_WORDS = ("广告", "advert", "advertisement", "推广")
SUBMIT_GAP_SEC = int(os.environ.get("AV_AUTO_SUBMIT_GAP", "48"))
WAIT_POLL_SEC = int(os.environ.get("AV_AUTO_WAIT_POLL", "20"))
SEEK_START = 120          # 2 minutes
SEEK_STEP = 480           # 8 minutes
SEEK_MAX_FRAMES = 8
SEEK_TAIL_GUARD = 90      # avoid end credits
SEEK_DEDUP = 30
PRECHECK_RETRIES = 3      # plus the first attempt
LLM_RUNTIME = Path("/config/scripts/av-auto-fs/llm_runtime.json")
# Host bind: /mnt/user/appdata/moviepilot/config/scripts/av-auto-fs/frames/
OFFICIAL_FRAMES_ROOT = Path("/config/scripts/av-auto-fs/frames")
TMP_FRAMES_ROOT = Path("/tmp/av-auto-frames")
_PERSIST_JSON = {"frames_manifest.json", "review.json", "grok_review.json", "probe.json"}
MAGPIE_DEFAULT = "http://192.168.1.9:3430"
PRECHECK_MODEL = "deepseek/deepseek-flash"
REVIEW_MODELS = ("deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna")
# Tiebreaker only. Do not add this to REVIEW_MODELS or drop "grok" from the ban.
GROK_TIEBREAK_MODEL = "cursor/grok-4.7"
FORBIDDEN_MODEL_BITS = ("minimax", "grok")
_VERDICTS = ("pass", "fail", "uncertain")
_FRAME_EXT = {".jpg", ".jpeg", ".png", ".webp"}
# Covers, stills, Thumb, Backdrop — never send these to the tiebreaker.
_BLOCKED_FRAME_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:covers?|stills?|thumbs?|thumbnails?|backdrops?)(?:[^a-z0-9]|$)"
)
PRECHECK_TO = int(os.environ.get("AV_AUTO_PRECHECK_TIMEOUT", "90"))
REVIEW_TO = int(os.environ.get("AV_AUTO_REVIEW_TIMEOUT", "180"))
TZ = ZoneInfo("Asia/Shanghai")
# A token in the command slot. Keep in sync with CATALOG_CODE_RE in bin/av_auto.
CATALOG_CODE_RE = re.compile(
    r"(?i)^(?:FC2(?:[-_]?PPV)?[-_]?\d{5,}|[A-Z]{2,10}-\d{2,5}|[0-9]{6}[-_]\d{2,3})(?:[-_][A-Z0-9]+)*$"
)
PROCESS_EXAMPLE = "av_auto process ABP-073 ABP-108 ABP-246 ABP-313 ABP-383"

PRECHECK_SYSTEM = (
    "你是抽帧有效性预检器，不是终审。只根据这一张图判断。"
    "无效（valid=false）仅当以下任一明确成立："
    "1) 画面里完全没有人（空景、黑屏、纯字幕卡、纯商标等）；"
    "2) 画面完全不色：没有色情、裸露、性行为，也没有明显色气的身体展示，只是日常空景、对白/胡聊、字幕、片头片尾等。"
    "其余情况 valid=true（包括看不清或不确定——交给终审，不要误杀）。"
    "只输出一个 JSON 对象，不要 Markdown，不要其它文字："
    '{"valid":true或false,"reason":"一句短理由","confidence":"high|mid|low"}。'
)

REVIEW_SYSTEM = (
    "你是影视库筛选助手。只根据所附抽帧里女优的可见外观判断，不要因为片名或类型标签（人妻、熟女、凌辱、强奸、SM）本身判负。"
    "色情题材、裸露、性行为本身不判负。スレンダー不要单独判成干扁；人妻/熟女/老片/蜜色不单独判负。"
    "用户口味可以否决「看起来还行」：皮肤白、年轻感、身材有曲线，或观感上的高颜值，都不能抵消任一较明确的命中。"
    "判定维度仅限："
    "dry_thin（身材干扁：瘦到没肉。纤细有曲线不算）、"
    "looks_old（显老：看起来比应有的年轻感老，面部松弛、纹路或气色显年纪。标签不算；画面里确实显老就要打。皮肤白或身材丰满不能抵消）、"
    "poor_skin（皮肤差：暗沉，明显不白、不水嫩）、"
    "not_pretty（不漂亮：按用户个人口味脸不好看、缺乏吸引力。年轻、皮肤白、身材不干扁、表情正常，仍然可以因为不好看单独 fail。"
    "不要把「挑不出干扁/显老/皮肤差/狰狞」当成 pass。已经明显好看、有吸引力的不要打）、"
    "soft_chest（胸部不够挺拔：下垂、软塌、不够立。丰满或罩杯大不等于挺拔，不能用「不干扁」抵消。挺的、只是大的，不要打）、"
    "implants（假胸：明显隆胸或假体，轮廓僵硬、圆形不自然、上缘鼓包，一看就是假。自然的大胸和挺拔的真胸不要打。看不准是不是假体就不要打）、"
    "grimace（狰狞：只有明显痛苦、难看、像在受苦的表情，见下）。"
    "「色不是难受」只指明显痛苦狰狞，不指色情高潮或 SM/调教里的用力脸。"
    "grimace 先分两种脸。"
    "色情用力脸：皱眉、露齿、眯眼、张嘴、眉眼紧绷、嘴微张、闭眼、轻微失神或白眼。"
    "出现在性行为、捆绑、拘束、SM、调教、强制高潮里时，视为色，不打 grimace，不要 fail。"
    "单帧或末段近景的紧皱露齿、眯眼张嘴、眉眼紧蹙嘴微张，不够。"
    "痛苦狰狞脸：多帧都是夸张的痛苦皱眉张口，或五官痛到扭曲、哭喊；单独一帧已经明显是受苦而不是高潮，也要打 grimace 并 fail。"
    "不要把这种脸改口说成快感。"
    "不要因为题材是 SM 就把用力脸打成狰狞，也不要因为题材是凌辱或强奸就把已经明显痛苦的脸放过。"
    "只输出一个 JSON 对象，不要 Markdown，不要其它文字。字段："
    '{"verdict":"pass|fail|uncertain","hits":[],"reason":"一句短理由","confidence":"high|mid|low"}。'
    "hits 只能用上述英文维度名，可多项。"
    "verdict=fail：至少一项在图中较明确，包括不漂亮、显老、胸部不够挺拔、假胸，或符合上面口径的狰狞。高颜值观感不能改成 pass。"
    "pass：这些维度都未见明显不符。"
    "uncertain：看不清、角度限制或证据冲突。"
    "只使用有效抽帧；不要根据无效空景或纯字幕想象内容。"
)


DROP_ROOT = Path("/config/scripts/av-auto-fs/pending")
# Below this, a failed write is a full disk. Above it, ENOSPC is a floor or reservation.
FULL_DISK_BYTES = 1024 ** 3


class _LeavePending(Exception):
    """Queued job is not applied in-container. Leave it for av_auto_apply_fs."""


def _is_media_path(path: Path) -> bool:
    s = str(path)
    return s.startswith("/media/") or s.startswith("/mnt/user/media/")


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f}MiB"
    return f"{n}B"


def _env_int(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if raw.isdigit():
        return int(raw)
    return None


def parse_share_floor_kb(text: str) -> int | None:
    """media.cfg stores shareFloor in KB. Return that integer, or None."""
    match = re.search(r"shareFloor\s*=\s*\"?(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1))


def _share_cfg_path() -> Path | None:
    raw = (os.environ.get("AV_AUTO_SHARE_CFG") or "").strip()
    candidates = [Path(raw)] if raw else []
    candidates.append(Path("/boot/config/shares/media.cfg"))
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _configured_floor_bytes() -> int | None:
    kb = _env_int("AV_AUTO_SHARE_FLOOR_KB")
    if kb is None:
        cfg = _share_cfg_path()
        if cfg is not None:
            try:
                kb = parse_share_floor_kb(cfg.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                kb = None
    if kb is None:
        return None
    return kb * 1024


def _live_floor_bytes() -> int | None:
    kb = _env_int("AV_AUTO_LIVE_FLOOR_KB")
    if kb is None:
        return None
    return kb * 1024


def _cfg_mtime() -> float | None:
    forced = _env_int("AV_AUTO_SHARE_CFG_MTIME")
    if forced is not None:
        return float(forced)
    cfg = _share_cfg_path()
    if cfg is None:
        return None
    try:
        return cfg.stat().st_mtime
    except OSError:
        return None


def _shfs_start() -> float | None:
    forced = _env_int("AV_AUTO_SHFS_START")
    if forced is None:
        return None
    return float(forced)


def _stat_free_bytes(path: Path) -> int | None:
    cur = path
    for _ in range(8):
        try:
            if cur.exists():
                st = os.statvfs(cur)
                return int(st.f_bavail) * int(st.f_frsize)
        except OSError:
            return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def classify_enospc(
    *,
    free_bytes: int | None,
    configured_floor_bytes: int | None = None,
    live_floor_bytes: int | None = None,
    cfg_mtime: float | None = None,
    shfs_start: float | None = None,
    path: str = "",
) -> dict:
    """Explain errno 28 without calling a large free share a full disk.

    kind is full, reservation, stale_floor, not_full, or unknown.
    A configured floor that is already above free space is a reservation:
    reloading shfs would still refuse the write. A higher live floor, or a
    media.cfg newer than the running shfs, is a stale in-memory floor.
    """
    where = f" 路径: {path}" if path else ""
    floor_blocks = (
        free_bytes is not None
        and configured_floor_bytes is not None
        and free_bytes < configured_floor_bytes
    )
    above_config = configured_floor_bytes is None or free_bytes is None or free_bytes >= configured_floor_bytes
    stale_live = (
        live_floor_bytes is not None
        and configured_floor_bytes is not None
        and live_floor_bytes > configured_floor_bytes
        and above_config
    )
    stale_mtime = (
        cfg_mtime is not None
        and shfs_start is not None
        and cfg_mtime > shfs_start
        and configured_floor_bytes is not None
        and above_config
    )
    if free_bytes is not None and free_bytes < FULL_DISK_BYTES:
        kind = "full"
        message = (
            f"这是磁盘满：可用 {_fmt_bytes(free_bytes)}，低于 1GiB。"
            "不是 shareFloor 未重载。已排队，但腾出空间之前 /mnt/user/media 上的新目录和 strm 写不进去。"
        )
    elif floor_blocks:
        kind = "reservation"
        message = (
            f"不是磁盘满。可用 {_fmt_bytes(free_bytes)} 低于配置 shareFloor {_fmt_bytes(configured_floor_bytes)}，"
            "写入被 shareFloor 预留拒绝。已排队。reload shfs 不会放宽这份配置。"
            "pending 里已有的 strm 内容仍可抽帧；新的 /mnt/user/media 写入要等可用空间高于 floor，"
            "或人改小 floor 并让 shfs 重新加载。本命令不会 reload。"
        )
    elif stale_live or stale_mtime:
        kind = "stale_floor"
        live_bit = ""
        if live_floor_bytes is not None:
            live_bit = f"运行中 floor {_fmt_bytes(live_floor_bytes)} 高于配置。"
        mtime_bit = ""
        if cfg_mtime is not None and shfs_start is not None and cfg_mtime > shfs_start:
            mtime_bit = "shfs 启动早于 media.cfg。"
        message = (
            f"不是磁盘满。可用 {_fmt_bytes(free_bytes)}，配置 shareFloor {_fmt_bytes(configured_floor_bytes)}。"
            f"{live_bit}{mtime_bit}运行中的 shfs 还没吃到这份 floor。已排队经 cache 落盘。"
            "挡住的是 /mnt/user/media 上的新目录和 strm；pending 里的 strm 内容仍可抽帧。"
            "官方入口即使 python 非 0 也会跑 av_auto_apply_fs。"
            "需要人让 shfs 重新读 media.cfg。本命令不会 reload。诊断: av_auto share-floor。"
        )
    elif free_bytes is not None and free_bytes >= FULL_DISK_BYTES:
        kind = "not_full"
        floor_bit = ""
        if configured_floor_bytes is not None:
            floor_bit = f"配置 shareFloor {_fmt_bytes(configured_floor_bytes)}，可用空间不低于该 floor。"
        message = (
            f"不是磁盘满。可用 {_fmt_bytes(free_bytes)}，写入仍返回 ENOSPC。{floor_bit}"
            "这是 shareFloor 或写入预留。不能靠这句话判断 shfs 是否还拿着旧 floor。"
            "已排队。诊断: av_auto share-floor。本命令不会 reload shfs。"
            "pending 里的 strm 内容仍可抽帧；官方入口结束时会跑 av_auto_apply_fs。"
        )
    else:
        kind = "unknown"
        message = (
            "ENOSPC（errno 28），但没有读到可用空间，不能判断是磁盘满还是 shareFloor。"
            "不要把它当成磁盘满。已排队。诊断: av_auto share-floor。本命令不会 reload shfs。"
        )
    return {"kind": kind, "message": f"NOTE: {message}{where}"}


def _enospc_note(path: Path) -> str:
    return classify_enospc(
        free_bytes=_stat_free_bytes(path),
        configured_floor_bytes=_configured_floor_bytes(),
        live_floor_bytes=_live_floor_bytes(),
        cfg_mtime=_cfg_mtime(),
        shfs_start=_shfs_start(),
        path=str(path),
    )["message"]


def _queue_fs(kind: str, path: Path, text: str | None = None) -> None:
    DROP_ROOT.mkdir(parents=True, exist_ok=True)
    job: Path | None = None
    for _ in range(5):
        candidate = DROP_ROOT / f"{time.time_ns()}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        job = candidate
        break
    if job is None:
        raise OSError(errno.ENOSPC, f"pending queue collision for {path}")
    (job / "kind").write_text(kind, encoding="utf-8")
    (job / "path").write_text(str(path), encoding="utf-8")
    if text is not None:
        (job / "data").write_text(text, encoding="utf-8")
    print(_enospc_note(path), flush=True)


def _pending_jobs() -> list[Path]:
    root = DROP_ROOT
    try:
        if not root.is_dir():
            return []
        jobs = [
            p for p in root.iterdir()
            if p.is_dir() and (p / "kind").is_file() and (p / "path").is_file()
        ]
    except OSError:
        return []
    return sorted(jobs, key=lambda p: p.name)


def _perform_pending_job(job: Path) -> None:
    kind = (job / "kind").read_text(encoding="utf-8").strip()
    path = Path((job / "path").read_text(encoding="utf-8").strip())
    if not _is_media_path(path):
        raise _LeavePending()
    data = ""
    data_path = job / "data"
    if data_path.is_file():
        data = data_path.read_text(encoding="utf-8")
    if kind == "mkdir":
        path.mkdir(parents=True, exist_ok=True)
    elif kind == "write_text":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
    elif kind == "rmtree":
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    else:
        raise _LeavePending()


def apply_pending_queue() -> tuple[int, int]:
    """Retry queued media writes. ENOSPC leaves the job for host apply_fs.

    Does not reload shfs. Paths outside /media and /mnt/user/media stay queued.
    """
    applied = 0
    blocked = 0
    for job in _pending_jobs():
        try:
            _perform_pending_job(job)
        except _LeavePending:
            continue
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                blocked += 1
                continue
            print(f"WARN: pending {job.name} 未能应用: errno={exc.errno}", flush=True)
            continue
        shutil.rmtree(job, ignore_errors=True)
        applied += 1
    return applied, blocked


def _queued_path_is_strm(queued: str, code: str) -> bool:
    name = Path(queued).name
    if not name.lower().endswith(".strm"):
        return False
    stem = Path(name).stem.upper()
    parent = Path(queued).parent.name.upper()
    return stem == code or parent == code


def _pending_strm_payload(code: str) -> tuple[str, str] | None:
    code = _norm_code(code)
    best: tuple[str, str, str] | None = None
    for job in _pending_jobs():
        try:
            kind = (job / "kind").read_text(encoding="utf-8").strip()
            queued = (job / "path").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if kind != "write_text" or not _queued_path_is_strm(queued, code):
            continue
        data_path = job / "data"
        if not data_path.is_file():
            continue
        try:
            text = data_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "http" not in text:
            continue
        if best is None or job.name > best[0]:
            best = (job.name, queued, text)
    if best is None:
        return None
    return best[1], best[2]


def _materialize_pending_strm(code: str) -> Path | None:
    found = _pending_strm_payload(code)
    if found is None:
        return None
    queued, text = found
    code = _norm_code(code)
    root = Path(os.environ.get("AV_AUTO_PENDING_STRM_ROOT", "/tmp/av-auto-pending-strm"))
    dest = root / code / f"{code}.strm"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    print(
        f"NOTE: {code} 的 strm 还在 pending（{queued}），不报缺失。"
        f"已用排队内容写到 {dest} 再抽帧。"
        "/media 上的目录和 strm 仍等 av_auto_apply_fs（process 非 0 也会跑）。",
        flush=True,
    )
    return dest


def _resolve_strm_for_code(code: str, paths: dict) -> Path | None:
    """Use a real strm if a retry just landed it; otherwise the pending payload."""
    apply_pending_queue()
    try:
        found = _find_strm_for_code(code, paths)
    except (ImportError, ModuleNotFoundError):
        found = None
    if found is not None:
        return found
    return _materialize_pending_strm(code)


def _strm_for_frames(code: str, paths: dict) -> Path:
    found = _resolve_strm_for_code(code, paths)
    if found is None:
        _die(
            f"找不到 {code} 的 strm（staging {paths['staging_strm']}/{code} 或正式库）。"
            "空目录请不要为测试拷片。"
        )
    return found


def media_mkdir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if exc.errno != 28 or not _is_media_path(path):
            raise
        _queue_fs("mkdir", path)


def media_write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        if exc.errno != 28 or not _is_media_path(path):
            raise
        _queue_fs("write_text", path, text)


def media_rmtree(path: Path) -> None:
    if path.is_dir():
        try:
            shutil.rmtree(path)
            return
        except OSError as exc:
            if exc.errno != 28:
                raise
    if _is_media_path(path):
        _queue_fs("rmtree", path)



def _die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _plugin_cfg() -> dict:
    con = sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True)
    row = con.execute(
        "SELECT value FROM systemconfig WHERE key=?", ("plugin.Av115Scraper",)
    ).fetchone()
    con.close()
    if not row:
        _die("MoviePilot 里没有 Av115Scraper 配置")
    obj = json.loads(row[0])
    return obj if isinstance(obj, dict) else {}


def _load_cookie() -> str:
    con = sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True)
    cookie = ""
    for key, field in (
        ("plugin.Av115Scraper", "cookie"),
        ("plugin.Anime115", "cookie"),
        ("plugin.P115Disk", "cookie"),
        ("plugin.P115StrmHelper", "cookies"),
        ("plugin.P115StrgmSub", "cookies"),
    ):
        row = con.execute("SELECT value FROM systemconfig WHERE key=?", (key,)).fetchone()
        if not row:
            continue
        try:
            obj = json.loads(row[0])
        except Exception:
            continue
        ck = str(obj.get(field) or "").strip()
        if "UID=" in ck:
            cookie = ck
            break
    con.close()
    if "UID=" not in cookie:
        _die("systemconfig 里没有可用的 115 cookie")
    return cookie


def _emby_auth() -> tuple[str, str]:
    con = sqlite3.connect(f"file:{USER_DB}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM systemconfig WHERE key=?", ("MediaServers",)).fetchone()
    con.close()
    host, key = "http://192.168.1.9:8096", ""
    if row:
        try:
            servers = json.loads(row[0])
        except Exception:
            servers = []
        for srv in servers or []:
            if not isinstance(srv, dict):
                continue
            cfg = srv.get("config") or {}
            h = str(cfg.get("host") or "").strip()
            k = str(cfg.get("apikey") or "").strip()
            if h and k:
                host, key = h.rstrip("/"), k
                if str(srv.get("type") or "").lower() == "emby":
                    break
    if not key:
        _die("MediaServers 里没有 Emby API key")
    return host, key


def _paths(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else _plugin_cfg()
    official_115 = (str(cfg.get("cloud_paths") or "/AV").splitlines()[0].strip() or "/AV")
    official_strm = Path(str(cfg.get("strm_root") or "/media/AV").strip() or "/media/AV")
    play = str(
        cfg.get("play_base_lan")
        or cfg.get("play_base")
        or "http://192.168.1.9:3000/api/v1/plugin/Av115Scraper/redirect?pickcode={pickcode}&file_name={filename}"
    )
    return {
        "official_115": official_115.rstrip("/") or "/AV",
        "official_strm": official_strm,
        "staging_115": STAGING_115_DEFAULT,
        "staging_inbox": STAGING_INBOX,
        "staging_strm": STAGING_STRM,
        "play_base": play,
        "emby_library": str(cfg.get("emby_library") or "AV").strip() or "AV",
        "offline_dir": str(cfg.get("offline_dir") or "/云下载"),
        "min_size_mb": int(cfg.get("min_size_mb") or MIN_SIZE_MB_DEFAULT),
    }


def _import_plugin():
    sys.path.insert(0, str(PLUGIN_DIR))
    from av115_library import build_play_url, destination, find_existing, safe_name, write_library
    from av115_pan import Pan115

    return Pan115, build_play_url, destination, find_existing, safe_name, write_library


def _pan():
    Pan115, *rest = _import_plugin()
    return Pan115(_load_cookie()), rest


def _id_ok(value: str | None) -> bool:
    return bool(value) and str(value) not in ("0", "None")


def _p115():
    from p115client import P115Client

    return P115Client(_load_cookie(), app="web")


def _min_bytes(paths: dict | None = None) -> int:
    mb = MIN_SIZE_MB_DEFAULT
    if paths:
        try:
            mb = int(paths.get("min_size_mb") or MIN_SIZE_MB_DEFAULT)
        except (TypeError, ValueError):
            mb = MIN_SIZE_MB_DEFAULT
    return max(0, mb) * 1024 * 1024


def _is_keeper(name: str, size: int, min_bytes: int) -> bool:
    ext = Path(name or "").suffix.lower()
    if ext not in VIDEO_EXT:
        return False
    if size and size < min_bytes:
        return False
    if not size and min_bytes > 0:
        return False
    lower = (name or "").lower()
    if any(word in lower for word in PREVIEW_WORDS):
        return False
    if any(word in lower for word in JUNK_NAME_WORDS):
        return False
    return True


def _item_fields(item: dict) -> tuple[str, str, bool, int, str]:
    from av115_pan import _file_id, _is_directory, _name, _size

    name = _name(item)
    fid = _file_id(item)
    is_dir = _is_directory(item)
    size = _size(item)
    pc = str(item.get("pc") or item.get("pickcode") or item.get("pick_code") or "")
    return name, fid, is_dir, size, pc


def _walk(pan, folder_id: str) -> list[dict]:
    out: list[dict] = []
    if not _id_ok(folder_id):
        return out
    for item in pan.list_children(folder_id):
        name, fid, is_dir, size, pc = _item_fields(item)
        rec = {"name": name, "id": fid, "is_dir": is_dir, "size": size, "pickcode": pc, "item": item}
        out.append(rec)
        if is_dir and _id_ok(fid):
            out.extend(_walk(pan, fid))
    return out


def _child_names(pan, directory_id: str) -> list[tuple[str, bool, str]]:
    out = []
    for item in pan.list_children(directory_id):
        name = str(item.get("n") or item.get("name") or item.get("fn") or "")
        fid = str(item.get("fid") or item.get("cid") or item.get("id") or item.get("file_id") or "")
        # _is_directory / _name / _file_id live in av115_pan
        try:
            from av115_pan import _file_id, _is_directory, _name

            name = _name(item)
            fid = _file_id(item)
            is_dir = _is_directory(item)
        except Exception:
            is_dir = bool(item.get("fid") in (None, "", "0") or item.get("fc") == "0" or item.get("is_dir"))
        if name:
            out.append((name, is_dir, fid))
    return out


def cmd_setup(_args) -> int:
    paths = _paths()
    staging_115 = paths["staging_115"]
    official_115 = paths["official_115"]
    if staging_115.rstrip("/") == official_115.rstrip("/") or staging_115.startswith(official_115.rstrip("/") + "/"):
        _die(f"staging 路径 {staging_115} 不能落在正式库 {official_115} 里")
    pan, _ = _pan()
    official_id = pan.dir_id(official_115) or pan.ensure_dir(official_115)
    if not _id_ok(official_id):
        _die(f"正式库 {official_115} 在 115 上不可用")
    existed = _id_ok(pan.dir_id(staging_115))
    staging_id = pan.ensure_dir(staging_115)
    if not _id_ok(staging_id):
        _die(f"无法创建 115 目录 {staging_115}")
    inbox = paths["staging_inbox"]
    inbox_id = pan.ensure_dir(inbox)
    if not _id_ok(inbox_id):
        _die(f"无法创建 115 目录 {inbox}")
    media_mkdir(paths["staging_strm"])
    root = _child_names(pan, "0")
    print(f"115 正式库: {official_115}  (id_ok={_id_ok(official_id)})")
    print(f"115 staging: {staging_115}  ({'已存在' if existed else '新创建'}, id_ok={_id_ok(staging_id)})")
    print(f"115 inbox: {inbox}  (id_ok={_id_ok(inbox_id)})")
    print(f"本地正式 strm: {paths['official_strm']}")
    print(f"本地 staging strm: {paths['staging_strm']}")
    print("115 根目录:", ", ".join(n + ("/" if d else "") for n, d, _ in root) or "(空)")
    return 0


def cmd_status(_args) -> int:
    paths = _paths()
    pan, _ = _pan()
    staging_id = pan.dir_id(paths["staging_115"])
    official_id = pan.dir_id(paths["official_115"])
    children = _child_names(pan, staging_id) if _id_ok(staging_id) else []
    inbox_id = pan.dir_id(paths["staging_inbox"])
    inbox_children = _child_names(pan, inbox_id) if _id_ok(inbox_id) else []
    code_dirs = [n for n, d, _ in children if d and n not in ("_staging",)]
    local = []
    root = paths["staging_strm"]
    if root.exists():
        local = sorted(p.name for p in root.iterdir() if p.is_dir())
    print(f"115 正式库: {paths['official_115']}  exists={_id_ok(official_id)}")
    print(f"115 staging: {paths['staging_115']}  exists={_id_ok(staging_id)}  番号夹={len(code_dirs)}")
    if code_dirs:
        print("  115 夹:", ", ".join(code_dirs))
    print(f"115 inbox: {paths['staging_inbox']}  exists={_id_ok(inbox_id)}  夹数={len(inbox_children)}")
    if inbox_children:
        print("  inbox 夹:", ", ".join(n for n, d, _ in inbox_children if d) or "(无子夹)")
    print(f"本地正式 strm: {paths['official_strm']}  exists={paths['official_strm'].is_dir()}")
    print(f"本地 staging strm: {paths['staging_strm']}  exists={root.is_dir()}  夹数={len(local)}")
    if local:
        print("  本地夹:", ", ".join(local))
    print(f"Emby 库名: {paths['emby_library']}")
    print("夹命名: /av-auto/<番号> → 搬家后 /AV/<番号>，与正式库一致")
    return 0


def _videos_in(pan, folder_id: str, min_bytes: int | None = None) -> list[dict]:
    if min_bytes is None:
        min_bytes = _min_bytes(_paths())
    videos = []
    for rec in _walk(pan, folder_id):
        if rec["is_dir"]:
            continue
        if not _is_keeper(rec["name"], rec["size"], min_bytes):
            continue
        videos.append(
            {
                "name": rec["name"],
                "id": rec["id"],
                "pickcode": rec["pickcode"],
                "size": rec["size"],
            }
        )
    videos.sort(key=lambda v: v["size"], reverse=True)
    return videos


def _write_strm_dir(dest: Path, play_base: str, build_play_url, safe_name, videos: list[dict], code: str) -> list[Path]:
    media_mkdir(dest)
    written = []
    for vid in videos:
        pc = vid.get("pickcode") or ""
        if not pc:
            print(f"WARN: {vid['name']} 无 pickcode，跳过 strm")
            continue
        fname = vid["name"]
        url = build_play_url(play_base, pc, fname)
        # single-file folders use <code>.strm to match official Av115 style
        stem = safe_name(code if len(videos) == 1 else Path(fname).stem)
        path = dest / f"{stem}.strm"
        media_write_text(path, url.strip() + "\n")
        written.append(path)
    return written


def cmd_strm(args) -> int:
    codes = [_norm_code(c) for c in (args.codes or [])]
    if not codes:
        _die("用法: av_auto strm 番号 [番号...]")
    pan, rest = _pan()
    build_play_url, _destination, _find_existing, safe_name, _write_library = rest
    paths = _paths()
    rc = 0
    for code in codes:
        folder = f"{paths['staging_115'].rstrip('/')}/{code}"
        fid = pan.dir_id(folder)
        if not _id_ok(fid):
            print(f"SKIP {code}: 115 上没有 {folder}")
            rc = 1
            continue
        videos = _videos_in(pan, fid)
        if not videos:
            print(f"SKIP {code}: {folder} 里没有成片，不写空 strm")
            rc = 1
            continue
        dest = paths["staging_strm"] / safe_name(code)
        written = _write_strm_dir(dest, paths["play_base"], build_play_url, safe_name, videos, code)
        print(f"OK {code}: 写了 {len(written)} 个 staging strm → {dest}")
    return rc


def _norm_code(c: str) -> str:
    return str(c or "").strip().upper()


def looks_like_catalog_code(token: str) -> bool:
    """True when token is a catalog code, so it must not be parsed as a subcommand."""
    return bool(CATALOG_CODE_RE.fullmatch(str(token or "").strip()))


def _command_tokens(argv: list[str]) -> list[str]:
    tokens = list(argv)
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    return tokens


def code_as_command_message(argv: list[str]) -> str:
    """Usage error when a catalog code was placed where the subcommand belongs."""
    tokens = _command_tokens(argv)
    codes: list[str] = []
    for token in tokens:
        if not looks_like_catalog_code(token):
            break
        codes.append(_norm_code(token))
    if not codes:
        return f"用法: av_auto process 番号 [番号...]  例如: {PROCESS_EXAMPLE}"
    shown = " ".join(codes)
    typed = " ".join(tokens)
    return (
        f"'{codes[0]}' 是番号，不是子命令。"
        f"不要把番号放在子命令前面（av_auto {typed}）。"
        f"115 离线完成后请用: av_auto process {shown}"
        "（不交磁、不刮削、不 promote）"
    )


def reject_code_as_command(argv: list[str]) -> None:
    """Refuse `av_auto ABP-073 clean` before argparse can treat the code as cmd."""
    tokens = _command_tokens(argv)
    if not tokens or tokens[0].startswith("-"):
        return
    if looks_like_catalog_code(tokens[0]):
        _die(code_as_command_message(tokens), 2)


def _read_strm_url(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("http"):
            return line
    _die(f"无法从 {path} 读到播放地址")
    return ""


def _resolve_cdn(url: str) -> str:
    """Follow exactly one redirect hop from Av115 /redirect to a 115 CDN URL."""
    import httpx

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    with httpx.Client(follow_redirects=False, timeout=30.0, headers=headers) as client:
        for method in ("HEAD", "GET"):
            try:
                if method == "GET":
                    r = client.get(url, headers={**headers, "Range": "bytes=0-0"})
                else:
                    r = client.head(url)
            except Exception as exc:
                last = str(exc)
                continue
            loc = r.headers.get("location") or r.headers.get("Location")
            if r.status_code in (301, 302, 303, 307, 308) and loc:
                if loc.startswith("/"):
                    p = urlparse(url)
                    loc = f"{p.scheme}://{p.netloc}{loc}"
                return loc
            if r.status_code in (200, 206):
                # Already a media URL (or redirect endpoint streamed the file).
                return url
            last = f"HTTP {r.status_code}"
        _die(f"跟 1 跳 CDN 失败: {last}")
    return url


def _host_only(url: str) -> str:
    p = urlparse(url)
    return p.netloc or "(none)"


def _redact_media(text: str) -> str:
    """Strip URLs/query tokens from tool output so we never print 115 links."""
    text = text or ""
    text = re.sub(r"https?://\S+", lambda m: f"http(s)://{urlparse(m.group(0)).netloc}/...", text)
    text = re.sub(r"ed2k://\S+", "ed2k://...", text)
    text = re.sub(r"magnet:\S+", "magnet:...", text)
    return text



def _t85(duration: float) -> float:
    t = duration * 0.85
    if t > duration - SEEK_TAIL_GUARD:
        t = max(duration - SEEK_TAIL_GUARD, duration * 0.80)
    return t


def seek_points(duration: float | None) -> tuple[list[tuple[str, int]], dict]:
    """Dense default: from 2min every ~8min, plus ~85% (avoid credits), max 8."""
    meta = {"capped": False, "grid": 0, "rule": "dense_2min_8min_85pct"}
    if not duration or duration <= 10:
        return [], meta
    pts: list[tuple[str, int]] = []

    def add(label: str, t: float) -> None:
        t = int(round(t))
        if 1 <= t < duration - 1 and all(abs(t - x[1]) >= SEEK_DEDUP for x in pts):
            if len(pts) < SEEK_MAX_FRAMES:
                pts.append((label, t))

    if duration < SEEK_START:
        add("frame_early", min(max(5, duration * 0.15), duration - 3))
        add("frame_85pct", _t85(duration))
        meta["grid"] = 0
        return pts, meta

    t85 = _t85(duration)
    raw_grid: list[int] = []
    t = float(SEEK_START)
    while t < duration - 1:
        ss = int(round(t))
        if ss <= duration - SEEK_TAIL_GUARD and ss < t85 - SEEK_DEDUP:
            raw_grid.append(ss)
        t += SEEK_STEP
    # reserve 1 slot for 85%
    meta["grid"] = len(raw_grid)
    meta["capped"] = (len(raw_grid) + 1) > SEEK_MAX_FRAMES
    for ss in raw_grid[: SEEK_MAX_FRAMES - 1]:
        add(f"frame_{ss}s", ss)
    add("frame_85pct", t85)
    return pts, meta


def _run_ff(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _assert_allowed_model(name: str) -> str:
    """Reject MiniMax and Grok for precheck and the default dual-model review.

    `grok-review` must not widen this gate. It pins cursor/grok-4.7 in
    `_grok_tiebreak_once` and never goes through `av_auto review`.
    """
    n = (name or "").strip()
    low = n.lower()
    if any(bit in low for bit in FORBIDDEN_MODEL_BITS):
        _die(f"拒绝模型 {n}：不上 MiniMax/Grok，不改 media_llm_route")
    return n


def _load_llm_runtime() -> dict:
    """Read Magpie caller key from a short-lived runtime file. Never print secrets."""
    path = Path(os.environ.get("AV_AUTO_LLM_FILE") or LLM_RUNTIME)
    if not path.is_file():
        _die(f"找不到 LLM 运行时文件 {path}。请用官方入口 av_auto（会临时放入 Magpie caller-keys），不要把手写 Key 写进脚本。")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _die("LLM 运行时文件无法解析（不打印内容）")
    key = ""
    url = MAGPIE_DEFAULT
    precheck_model = PRECHECK_MODEL
    review_models = list(REVIEW_MODELS)
    if isinstance(raw, dict):
        url = (raw.get("magpie_url") or raw.get("base_url") or url).rstrip("/")
        key = (raw.get("api_key") or raw.get("secret") or raw.get("key") or "").strip()
        precheck_model = raw.get("precheck_model") or precheck_model
        if raw.get("review_models"):
            review_models = list(raw["review_models"])
        items = raw.get("keys") or raw.get("caller_keys") or []
    else:
        items = raw if isinstance(raw, list) else []
    if not key:
        for it in items:
            if not isinstance(it, dict):
                continue
            secret = (it.get("secret") or it.get("key") or it.get("api_key") or "").strip()
            if secret and it.get("enabled", True) is not False:
                key = secret
                break
    if not key:
        _die("LLM 运行时文件里没有可用的 Magpie Key")
    precheck_model = _assert_allowed_model(precheck_model)
    review_models = [_assert_allowed_model(m) for m in review_models]
    return {"url": url, "key": key, "precheck_model": precheck_model, "review_models": review_models}


def _parse_json_obj(text: str) -> dict | None:
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _ds_peak(ts: datetime | None = None) -> bool:
    t = ts or datetime.now(TZ)
    if t.weekday() >= 5:
        return False
    return (9 <= t.hour < 12) or (14 <= t.hour < 18)


def _cost_of(usages: list[dict], started: datetime | None = None) -> dict:
    """Currency only in summaries; keep raw usage in the manifest for local audit."""
    peak = _ds_peak(started)
    ds_in = ds_cache = ds_out = 0
    lu_in = lu_out = 0
    for u in usages:
        if not isinstance(u, dict):
            continue
        model = (u.get("model") or u.get("model_requested") or "").lower()
        prompt = int(u.get("prompt_tokens") or 0)
        details = u.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
        uncached = max(prompt - cached, 0)
        out = int(u.get("completion_tokens") or 0)
        is_ds = "deepseek-flash" in model or model.endswith("/deepseek-flash") or model == "deepseek-flash"
        is_lu = "gpt-6-luna" in model or "luna" in model
        if is_ds:
            ds_in += uncached
            ds_cache += cached
            ds_out += out
        elif is_lu:
            lu_in += prompt
            lu_out += out
    mul = 1.0 if peak else 0.5
    ds_cny = ds_in * 2.0 * mul / 1e6 + ds_cache * 0.04 * mul / 1e6 + ds_out * 8.0 * mul / 1e6
    lu_usd = lu_in * 0.10 / 1e6 + lu_out * 0.50 / 1e6
    return {
        "peak": peak,
        "deepseek_cny": round(ds_cny, 4),
        "luna_usd_est": round(lu_usd, 4),
    }


def _data_uri(path: Path) -> str:
    raw = path.read_bytes()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _post_magpie_chat(cfg: dict, model: str, images: list[Path], system: str, user_text: str, timeout: int, max_tokens: int) -> dict:
    """POST one Magpie chat. This function is not a model allowlist.

    Precheck and `av_auto review` must call `_magpie_chat`, which still rejects
    MiniMax and Grok. The tiebreaker calls this only after pinning
    `cursor/grok-4.7` and does not write media_llm_route.json.
    """
    if "minimax" in (model or "").lower():
        _die(f"拒绝模型 {model}：不用 MiniMax，不改 media_llm_route")
    content = [{"type": "text", "text": user_text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": _data_uri(img)}})
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    req = urllib.request.Request(
        cfg["url"] + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
            msg = ((payload.get("choices") or [{}])[0].get("message") or {})
            raw = msg.get("content") or ""
            parsed = _parse_json_obj(raw)
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                usage = dict(usage)
                usage["model"] = payload.get("model") or model
            return {
                "ok": True,
                "http": r.status,
                "elapsed_s": round(time.time() - t0, 2),
                "model_requested": model,
                "model_echo": payload.get("model"),
                "raw_content": (raw or "")[:2000],
                "parsed": parsed,
                "usage": usage,
            }
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")[:300]
        return {
            "ok": False,
            "http": e.code,
            "elapsed_s": round(time.time() - t0, 2),
            "model_requested": model,
            "error": f"HTTP {e.code} {_redact_media(err)}",
            "retryable": e.code in (422, 429, 500, 502, 503, 504),
        }
    except Exception as e:
        return {
            "ok": False,
            "http": None,
            "elapsed_s": round(time.time() - t0, 2),
            "model_requested": model,
            "error": _redact_media(f"{type(e).__name__}: {e}")[:300],
            "retryable": "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower(),
        }


def _magpie_chat(cfg: dict, model: str, images: list[Path], system: str, user_text: str, timeout: int, max_tokens: int) -> dict:
    model = _assert_allowed_model(model)
    return _post_magpie_chat(cfg, model, images, system, user_text, timeout, max_tokens)


def _chat_retry(cfg, model, images, system, user_text, timeout, max_tokens, tries=3) -> dict:
    last = {}
    for i in range(1, tries + 1):
        rec = _magpie_chat(cfg, model, images, system, user_text, timeout, max_tokens)
        rec["attempts"] = i
        last = rec
        if rec.get("ok") and rec.get("parsed"):
            return rec
        if not rec.get("retryable") and not (rec.get("ok") and not rec.get("parsed")):
            return rec
        time.sleep(2 * i)
    return last


def _precheck_image(cfg: dict, path: Path, code: str, label: str, ss: int) -> dict:
    user = f"番号 {code} 抽帧 {label} @{ss}s。请判断这一张是否为有效帧，只输出 JSON。"
    rec = _chat_retry(cfg, cfg["precheck_model"], [path], PRECHECK_SYSTEM, user, PRECHECK_TO, 200)
    parsed = rec.get("parsed") or {}
    valid = parsed.get("valid")
    # API/parse failure: do not discard the frame; final review can still see it.
    if valid is not True and valid is not False:
        if rec.get("ok"):
            valid = True
            reason = "预检响应无法解析，先当作有效交给终审"
            confidence = "low"
            error = "parse"
        else:
            valid = True
            reason = f"预检调用失败，先当作有效交给终审（{rec.get('error') or 'error'}）"
            confidence = "low"
            error = rec.get("error") or "call"
        uncertain = True
    else:
        reason = parsed.get("reason") or ""
        confidence = parsed.get("confidence") or ""
        error = ""
        uncertain = False
    return {
        "valid": bool(valid),
        "reason": reason,
        "confidence": confidence,
        "model": rec.get("model_echo") or rec.get("model_requested"),
        "elapsed_s": rec.get("elapsed_s"),
        "usage": rec.get("usage") or {},
        "error": error,
        "uncertain": uncertain,
        "http": rec.get("http"),
        "attempts": rec.get("attempts") or 1,
    }


def _nearby_times(ss: int, nxt: int | None, duration: float, used: set[int]) -> list[int]:
    """Up to 3 later points between ss and the next planned time (or tail guard)."""
    cap = int(duration - SEEK_TAIL_GUARD) if duration else ss + 180
    if cap <= ss + 1:
        cap = int(duration - 2) if duration else ss + 60
    hi = cap
    if nxt is not None:
        hi = min(hi, int(nxt) - SEEK_DEDUP)
    out: list[int] = []

    def ok(t: int) -> bool:
        if t <= ss or t >= (duration or t + 1) - 1:
            return False
        if t > cap:
            return False
        if any(abs(t - u) < SEEK_DEDUP for u in used):
            return False
        if any(abs(t - x) < SEEK_DEDUP for x in out):
            return False
        return True

    span = hi - ss
    fracs = (0.25, 0.50, 0.75) if span >= 3 * SEEK_DEDUP else (0.33, 0.66, 0.90)
    for frac in fracs:
        t = int(round(ss + max(span, SEEK_DEDUP * 2) * frac))
        if ok(t):
            out.append(t)
        if len(out) >= PRECHECK_RETRIES:
            return out
    # tight gap: walk forward in 45s steps, then a little backward if still short
    t = ss + 45
    while len(out) < PRECHECK_RETRIES and t <= cap:
        if ok(t):
            out.append(t)
        t += 45
    t = ss - 45
    floor = SEEK_START if duration and duration >= SEEK_START else 5
    while len(out) < PRECHECK_RETRIES and t >= floor:
        if ok(t):
            out.append(t)
        t -= 45
    return out[:PRECHECK_RETRIES]


def _is_partial_file_error(text: str) -> bool:
    return "partial file" in (text or "").lower()


def _ffmpeg_one_frame(media: str, dest: Path, ss: float, *, output_seek: bool = False) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    row = {"ss": ss, "ok": False, "degraded": False, "bytes": 0, "error": ""}
    seek = ["-ss", f"{float(ss):.3f}"]
    if output_seek:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", media, *seek]
    else:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *seek, "-i", media]
    cmd += ["-frames:v", "1", "-q:v", "2", "-vf", "scale=w='min(800,iw)':h=-2", str(dest)]
    try:
        fr = _run_ff(cmd, FRAME_TO)
    except subprocess.TimeoutExpired:
        row["degraded"] = True
        row["error"] = f"seek timeout {FRAME_TO}s"
        return row
    if fr.returncode == 0 and dest.is_file() and dest.stat().st_size > 500:
        row["ok"] = True
        row["bytes"] = dest.stat().st_size
        return row
    row["degraded"] = True
    row["error"] = _redact_media((fr.stderr or "")[-200:]) or f"ffmpeg rc={fr.returncode}"
    if dest.exists():
        dest.unlink()
    return row


def _snippet_seek(path: Path, ss: float, frag_t0: float) -> float:
    """Zero-based offset into a local init+fragment file.

    The snippet's ffprobe start_time is often the absolute movie time of the
    moof. Seeking to that absolute time (start_time + offset) makes ffmpeg
    drop every frame and exit 0 with an empty jpeg. Seeking to the offset
    within the fragment, counting from 0, lands on the right picture.
    """
    local = max(0.0, float(ss) - float(frag_t0))
    try:
        probed = _run_ff(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            PROBE_TO,
        )
        info = json.loads(probed.stdout or "{}")
        dur = float((info.get("format") or {}).get("duration") or 0)
    except (subprocess.TimeoutExpired, ValueError, TypeError):
        dur = 0.0
    # A one-GOP snippet is short. Don't clamp against a duration that is
    # really the absolute end timestamp of a fragment that started later.
    if 0.05 < dur <= 2.0 and local >= dur:
        local = max(0.0, dur - 0.05)
    return local


def _extract_fmp4_frame(session, dest: Path, ss: float) -> dict:
    """Extract one frame from a fragmented HTTP MP4 without reading the whole object."""
    snip = dest.with_name(dest.stem + ".snip.mp4")
    try:
        try:
            blob, t0 = session.snippet_for(ss)
        except Fmp4RangeError as exc:
            return {"ss": ss, "ok": False, "degraded": True, "bytes": 0, "error": str(exc)[:300], "via": "fmp4-range"}
        snip.write_bytes(blob)
        seek = _snippet_seek(snip, float(ss), t0)
        got = _ffmpeg_one_frame(str(snip), dest, seek)
        if not got["ok"]:
            got = _ffmpeg_one_frame(str(snip), dest, seek, output_seek=True)
        if not got["ok"] and seek != 0:
            got = _ffmpeg_one_frame(str(snip), dest, 0)
        got["via"] = "fmp4-range"
        return got
    finally:
        if snip.exists():
            snip.unlink()


def _extract_one(media: str, dest: Path, ss: int, session=None) -> dict:
    if session is not None:
        return _extract_fmp4_frame(session, dest, ss)
    row = _ffmpeg_one_frame(media, dest, ss)
    if row["ok"] or not _is_partial_file_error(row.get("error") or ""):
        return row
    if not str(media).startswith(("http://", "https://")):
        return row
    # Direct HTTP seek reported a partial file. Retry once via closed ranges.
    try:
        fallback = open_fmp4_session(media)
    except Fmp4RangeError as exc:
        row["error"] = (row.get("error") or "")[:180] + " | " + str(exc)[:120]
        return row
    if fallback is None:
        return row
    try:
        retry = _extract_fmp4_frame(fallback, dest, ss)
    finally:
        fallback.close()
    if retry.get("ok"):
        return retry
    row["error"] = ((row.get("error") or "") + " | " + (retry.get("error") or ""))[:300]
    return row


def _find_strm_for_code(code: str, paths: dict) -> Path | None:
    sys.path.insert(0, str(PLUGIN_DIR))
    from av115_library import safe_name

    name = safe_name(code)
    candidates = [
        paths["staging_strm"] / name / f"{name}.strm",
        paths["official_strm"] / name / f"{name}.strm",
    ]
    staging = paths["staging_strm"] / name
    if staging.is_dir():
        candidates.extend(sorted(staging.glob("*.strm")))
    official = paths["official_strm"]
    direct = official / name
    if direct.is_dir():
        candidates.extend(sorted(direct.glob("*.strm")))
    if official.is_dir():
        for child in official.iterdir():
            nested = child / name
            if nested.is_dir():
                candidates.extend(sorted(nested.glob("*.strm")))
    for p in candidates:
        if p.is_file():
            return p
    return None


def cmd_frames(args) -> int:
    paths = _paths()
    strm: Path | None = None
    code = _norm_code(args.code) if args.code else ""
    if args.strm:
        strm = Path(args.strm)
    elif code:
        strm = _strm_for_frames(code, paths)
    else:
        _die("用法: av_auto frames 番号  或  av_auto frames --strm /path/file.strm")
    if not strm.is_file():
        _die(f"strm 不存在: {strm}")
    code = code or _norm_code(strm.stem) or strm.stem
    explicit = str(getattr(args, "out", "") or "").strip()
    if explicit:
        out = Path(explicit)
    else:
        out, rc = _select_frames_dir(code)
        if rc != 0:
            return rc
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"FAIL {code}: 未能创建帧目录 {out}（{exc.strerror or exc}）", file=sys.stderr)
        return 1
    precheck = not bool(getattr(args, "no_precheck", False))
    reuse = not bool(getattr(args, "no_reuse", False))
    llm = _load_llm_runtime() if precheck else None
    src = _read_strm_url(strm)
    print(f"strm: {strm}")
    print(f"redirect host: {_host_only(src)}")
    hop = ""
    try:
        hop = _resolve_cdn(src)
        print(f"cdn host (1 hop): {_host_only(hop)}")
    except Exception as exc:
        print(f"cdn hop 探测失败（ffmpeg 会自行跟随 redirect）: {_redact_media(str(exc))}")
    # 手动打开 CDN 链会 403（缺跳转 cookie）。渐进式 MP4 仍把 Av115 redirect 交给 ffmpeg
    # 自己跟 1 跳。fragmented MP4 则自己跟跳并保住 cookie，只用闭合 Range 取目标分片。
    media = src
    probe = out / "probe.json"
    session = None
    access = "direct"
    if media.startswith(("http://", "https://")):
        try:
            session = open_fmp4_session(media)
        except Fmp4RangeError as exc:
            print(f"fragmented mp4 Range 索引失败，退回 ffmpeg 直连: {exc}")
            session = None
    try:
        if session is not None:
            access = "fmp4-range:" + session.index.source
            duration = float(session.index.duration or 0)
            probe_doc = {
                "format": {
                    "duration": f"{duration:.6f}",
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "size": str(session.remote.size),
                },
                "streams": session.index.streams,
                "fragmented": True,
                "index": session.index.source,
            }
            probe.write_text(json.dumps(probe_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(
                f"fragmented mp4: {session.index.source} 索引，duration {duration:.1f}s，"
                f"大小 {session.remote.size} 字节；闭合 Range 取 moof/mdat，不整文件下载"
            )
        else:
            try:
                r = _run_ff(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration,format_name",
                        "-show_entries",
                        "stream=codec_name,codec_type,width,height",
                        "-of",
                        "json",
                        media,
                    ],
                    PROBE_TO,
                )
            except subprocess.TimeoutExpired:
                _die(f"ffprobe 超时 {PROBE_TO}s")
            if r.returncode != 0:
                _die(f"ffprobe 失败: {_redact_media((r.stderr or '')[-300:])}")
            probe.write_text(r.stdout or "{}", encoding="utf-8")
            try:
                info = json.loads(r.stdout or "{}")
                duration = float((info.get("format") or {}).get("duration") or 0)
            except Exception:
                duration = 0
            print(f"duration: {duration:.1f}s" if duration else "duration: unknown")
        pts, meta = seek_points(duration)
        if not pts:
            print("WARN: 片长不足，无法按加密规则抽帧")
            if args.cleanup:
                shutil.rmtree(out, ignore_errors=True)
            return 1
        labels = ", ".join(f"{l}@{ss}s" for l, ss in pts)
        cap_note = " 触顶8帧" if meta.get("capped") else ""
        print(f"seek: {labels}{cap_note}")
        if precheck:
            print(f"precheck: {llm['precheck_model']} 一家；无效则在该点附近重抽，单点最多 {PRECHECK_RETRIES} 次")
        ok = 0
        valid_n = 0
        discarded = []
        degraded = []
        shots = []
        usages = []
        used: set[int] = set()
        started = datetime.now(TZ)
        for i, (label, ss) in enumerate(pts):
            nxt = pts[i + 1][1] if i + 1 < len(pts) else None
            canonical = out / f"{code}_{label}.jpg"
            candidates = [ss] + _nearby_times(ss, nxt, duration, used | {ss})
            attempts = []
            kept = None
            for attempt_i, t in enumerate(candidates[: 1 + PRECHECK_RETRIES]):
                dest = canonical if attempt_i == 0 else out / f"{code}_{label}_r{attempt_i}.jpg"
                extracted = False
                if reuse and dest.is_file() and dest.stat().st_size > 500 and attempt_i == 0:
                    ext = {"ss": t, "ok": True, "degraded": False, "bytes": dest.stat().st_size, "error": "", "reused": True}
                    extracted = True
                else:
                    ext = _extract_one(media, dest, t, session=session)
                    ext["reused"] = False
                    extracted = bool(ext.get("ok"))
                used.add(t)
                attempt = {"ss": t, "try": attempt_i, "extract_ok": extracted, "bytes": ext.get("bytes") or 0, "reused": ext.get("reused")}
                if not extracted:
                    attempt["degraded"] = True
                    attempt["error"] = ext.get("error") or "extract fail"
                    attempts.append(attempt)
                    print(f"DEGRADE {label} @{t}s try={attempt_i} {ext.get('error') or 'fail'}，换附近时间点")
                    continue
                if not precheck:
                    if dest != canonical:
                        if canonical.exists():
                            canonical.unlink()
                        dest.replace(canonical)
                        dest = canonical
                    attempt["valid"] = None
                    attempts.append(attempt)
                    kept = {"label": label, "ss": t, "ok": True, "valid": None, "degraded": False, "bytes": dest.stat().st_size, "file": dest.name, "precheck": None, "attempts": attempts}
                    print(f"OK {label} @{t}s bytes={dest.stat().st_size}（未预检）")
                    break
                pc = _precheck_image(llm, dest, code, label, t)
                if pc.get("usage"):
                    usages.append(pc["usage"])
                attempt["valid"] = pc["valid"]
                attempt["reason"] = pc["reason"]
                attempt["confidence"] = pc["confidence"]
                attempts.append(attempt)
                if pc["valid"]:
                    if dest != canonical:
                        if canonical.exists():
                            canonical.unlink()
                        dest.replace(canonical)
                        dest = canonical
                    kept = {
                        "label": label,
                        "ss": t,
                        "ok": True,
                        "valid": True,
                        "degraded": False,
                        "bytes": dest.stat().st_size,
                        "file": dest.name,
                        "precheck": pc,
                        "attempts": attempts,
                        "retries": attempt_i,
                        "planned_ss": ss,
                    }
                    note = " 预检不确定，先留给终审" if pc.get("uncertain") else ""
                    print(f"VALID {label} @{t}s bytes={dest.stat().st_size} try={attempt_i} {pc.get('reason') or ''}{note}")
                    break
                print(f"INVALID {label} @{t}s try={attempt_i} {pc.get('reason') or '无效帧'}，不送终审，重抽")
                if dest.exists():
                    dest.unlink()
            if kept:
                shots.append(kept)
                ok += 1
                if kept.get("valid") is not False:
                    valid_n += 1
            else:
                reasons = [a.get("reason") or a.get("error") or "" for a in attempts]
                discarded.append({"label": label, "planned_ss": ss, "attempts": attempts})
                degraded.append(f"{label}@{ss}s discarded")
                shots.append({
                    "label": label,
                    "ss": ss,
                    "ok": False,
                    "valid": False,
                    "degraded": True,
                    "bytes": 0,
                    "file": "",
                    "precheck": None,
                    "attempts": attempts,
                    "retries": max(0, len(attempts) - 1),
                    "planned_ss": ss,
                    "discarded": True,
                    "reason": "；".join(x for x in reasons if x)[:300],
                })
                print(f"DISCARD {label} 计划 @{ss}s，{len(attempts)} 次后仍无效或抽不到，丢弃该点")
            if canonical.exists() and not (kept and kept.get("ok")):
                canonical.unlink()
        cost = _cost_of(usages, started) if usages else {"peak": _ds_peak(started), "deepseek_cny": 0.0, "luna_usd_est": 0.0}
        manifest = {
            "code": code,
            "strm": str(strm),
            "duration": duration,
            "rule": meta.get("rule"),
            "capped": bool(meta.get("capped")),
            "precheck": precheck,
            "precheck_model": (llm or {}).get("precheck_model") if precheck else "",
            "planned": [{"label": l, "ss": ss} for l, ss in pts],
            "shots": shots,
            "ok": ok,
            "valid": valid_n,
            "planned_n": len(pts),
            "discarded_n": len(discarded),
            "discarded": discarded,
            "degraded": degraded,
            "cost": cost,
            "media_access": access,
            "range_bytes": int(session.remote.fetched) if session is not None else 0,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(TZ).isoformat(),
        }
        (out / "frames_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        produced = valid_n if precheck else ok
        summary = (
            f"frames 抽出 {ok}/{len(pts)} 有效 {valid_n}/{len(pts)} → {out}"
            + (f"  丢弃 {len(discarded)}" if discarded else "")
            + (f"  预检 DeepSeek ¥{cost['deepseek_cny']:.4f}" if precheck else "")
        )
        try:
            keep = _tmp_frames_root() / f"{code}_manifest.json"
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        except OSError as exc:
            print(f"NOTE: 临时清单未写到容器 tmp（{exc.strerror or exc}）", file=sys.stderr)
        if not produced:
            print(summary)
            return 1
        if _persist_frames_dir(code, out) != 0:
            return 1
        print(summary)
        if args.cleanup:
            if _same_frames_path(out, _durable_frames_dir(code)):
                print(f"保留持久帧 {out}")
            else:
                for child in out.iterdir():
                    if child.name != "frames_manifest.json":
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                print("已清理临时帧（保留 frames_manifest.json）")
        return 0
    finally:
        if session is not None:
            fetched = int(session.remote.fetched)
            session.close()
            print(f"range 实际读取 {fetched} 字节（未整文件下载）")



def _load_manifest(out: Path, code: str) -> dict:
    path = out / "frames_manifest.json"
    if not path.is_file():
        _die(f"找不到 {path}，请先跑 av_auto frames")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_review(args) -> int:
    """Dual-model final review on valid frames only. Never sends invalid shots."""
    code = _norm_code(args.code)
    explicit = str(getattr(args, "out", "") or "").strip()
    if explicit:
        out = Path(explicit)
    else:
        out, rc = _resolve_marker_dir(code, "frames_manifest.json")
        if rc != 0:
            return rc
    manifest = _load_manifest(out, code)
    if not manifest.get("precheck"):
        _die("清单未经有效性预检。请先跑 av_auto frames（默认会预检），不要把未预检帧送进终审。")
    shots = [s for s in (manifest.get("shots") or []) if s.get("ok") and s.get("valid") is True and s.get("file")]
    files = []
    for s in shots:
        pth = out / s["file"]
        if pth.is_file() and pth.stat().st_size > 500:
            files.append((s, pth))
    if not files:
        _die(f"{code} 没有可送终审的有效帧（无效帧已丢弃）")
    llm = _load_llm_runtime()
    images = [p for _, p in files]
    captions = "、".join(f"{s['label']}@{s['ss']}s" for s, _ in files)
    user = f"番号 {code}。本请求只附有效抽帧（均为本地缩图的 data URI，不是网址）：{captions}。请综合这些有效帧给出 JSON 判断。"
    started = datetime.now(TZ)
    models = {}
    usages = []
    print(f"review {code}: {len(files)} 有效帧 → {', '.join(llm['review_models'])}；无效帧不送")
    for model in llm["review_models"]:
        rec = _chat_retry(llm, model, images, REVIEW_SYSTEM, user, REVIEW_TO, 800)
        parsed = rec.get("parsed") or {}
        verdict = parsed.get("verdict") if parsed.get("verdict") in ("pass", "fail", "uncertain") else "uncertain"
        if rec.get("usage"):
            usages.append(rec["usage"])
        models[model] = {
            "ok": bool(rec.get("ok") and parsed),
            "verdict": verdict,
            "hits": parsed.get("hits") or [],
            "reason": parsed.get("reason") or rec.get("error") or "",
            "confidence": parsed.get("confidence") or "",
            "elapsed_s": rec.get("elapsed_s"),
            "http": rec.get("http"),
            "attempts": rec.get("attempts") or 1,
            "usage": rec.get("usage") or {},
        }
        print(f"  {model}: {verdict} {models[model]['reason']}".rstrip())
    vs = [models[m]["verdict"] for m in llm["review_models"]]
    if vs and all(v == vs[0] for v in vs):
        agreement = "agree"
        consensus = vs[0]
    elif "fail" in vs and "pass" in vs:
        agreement = "disagree"
        consensus = "uncertain"
    else:
        agreement = "partial"
        consensus = "uncertain" if "uncertain" in vs else vs[0]
    cost = _cost_of(usages, started)
    result = {
        "code": code,
        "frames_dir": str(out),
        "valid_frames": [{"label": s["label"], "ss": s["ss"], "file": s["file"]} for s, _ in files],
        "valid_n": len(files),
        "models": models,
        "agreement": agreement,
        "consensus": consensus,
        "cost": cost,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(TZ).isoformat(),
        "note": "invalid frames were not sent",
    }
    dest = out / "review.json"
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    if _persist_frames_dir(code, out, require_review=True) != 0:
        return 1
    official_review = _durable_frames_dir(code) / "review.json"
    print(
        f"review {code}: {agreement}/{consensus}  DeepSeek ¥{cost['deepseek_cny']:.4f}"
        f"  luna 约 ${cost['luna_usd_est']:.4f} → {official_review}"
    )
    if args.cleanup:
        if _same_frames_path(out, _durable_frames_dir(code)):
            print(f"保留持久帧 {out}")
        else:
            for child in out.iterdir():
                if child.name in {"frames_manifest.json", "review.json"}:
                    continue
                if child.is_file() and child.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                    child.unlink()
            print("已清理临时帧（保留 frames_manifest.json 与 review.json）")
    return 0


def _model_short(model: str) -> str:
    name = str(model or "").rsplit("/", 1)[-1]
    return name or str(model or "")


def _frame_blocked(name: str) -> bool:
    """True for covers, stills, Thumb, Backdrop and similar non-frame names."""
    text = str(name or "")
    base = Path(text).name
    return bool(_BLOCKED_FRAME_RE.search(base) or _BLOCKED_FRAME_RE.search(text))


def _read_json_dict(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _die(f"无法解析 {path.name}（不打印内容）")
    if not isinstance(obj, dict):
        _die(f"{path.name} 不是 JSON 对象")
    return obj


def prior_model_verdicts(review: dict) -> dict[str, dict]:
    """Dual-model rows from review.json, excluding any Grok or MiniMax entry."""
    models = review.get("models") if isinstance(review, dict) else None
    out: dict[str, dict] = {}
    if not isinstance(models, dict):
        return out
    for mid, row in models.items():
        name = str(mid)
        low = name.lower()
        if "grok" in low or "minimax" in low:
            continue
        if not isinstance(row, dict):
            continue
        verdict = row.get("verdict")
        if verdict not in _VERDICTS:
            verdict = "uncertain"
        hits = row.get("hits") if isinstance(row.get("hits"), list) else []
        out[name] = {
            "verdict": verdict,
            "confidence": row.get("confidence") or "",
            "reason": _redact_media(str(row.get("reason") or ""))[:500],
            "hits": hits,
            "ok": bool(row.get("ok")),
        }
    return out


def tiebreak_trigger(review: dict) -> str:
    """'disagree' or 'uncertain' when Grok should step in, else ''."""
    if not isinstance(review, dict):
        return ""
    verdicts = [row["verdict"] for row in prior_model_verdicts(review).values()]
    agreement = str(review.get("agreement") or "")
    consensus = str(review.get("consensus") or "")
    if len(verdicts) >= 2:
        if "pass" in verdicts and "fail" in verdicts:
            return "disagree"
        if any(v == "uncertain" for v in verdicts):
            return "uncertain"
        if len(set(verdicts)) == 1 and verdicts[0] in ("pass", "fail"):
            return ""
    if agreement == "disagree":
        return "disagree"
    if agreement == "partial" or consensus == "uncertain":
        return "uncertain"
    return ""


def review_needs_tiebreak(review: dict) -> bool:
    """True when a dual-model review is uncertain or the two models disagree."""
    return tiebreak_trigger(review) != ""


def decide_tiebreak(prior: dict[str, str], grok_verdict: str) -> dict:
    """Pick the verdict to keep. A Grok pass/fail settles an unresolved review."""
    verdict = grok_verdict if grok_verdict in _VERDICTS else "uncertain"
    labels = {mid: _model_short(mid) for mid in prior}
    aligned = [mid for mid, value in prior.items() if value == verdict and verdict in ("pass", "fail")]
    values = [prior[mid] for mid in prior]
    split = "pass" in values and "fail" in values
    if verdict in ("pass", "fail"):
        kept = verdict
        kept_from = GROK_TIEBREAK_MODEL
        resolved = True
        if split and aligned:
            who = "、".join(labels[mid] for mid in aligned)
            conclusion = f"两家分歧。Grok 判 {verdict}，与 {who} 一致，保留 {verdict}。"
        elif values and all(value == "uncertain" for value in values):
            conclusion = f"两家均为 uncertain。Grok 判 {verdict}，保留 {verdict}。"
        elif aligned:
            who = "、".join(labels[mid] for mid in aligned)
            conclusion = f"双模型结果未决。Grok 判 {verdict}，与 {who} 一致，保留 {verdict}。"
        else:
            conclusion = f"双模型结果未决。Grok 判 {verdict}，以此为准，保留 {verdict}。"
    else:
        kept = "uncertain"
        kept_from = ""
        aligned = []
        resolved = False
        if split:
            conclusion = "两家分歧。Grok 仍为 uncertain，未能打破平局，保留 uncertain。"
        else:
            conclusion = "Grok 仍为 uncertain，本条保持未决，保留 uncertain。"
    return {
        "kept_verdict": kept,
        "kept_from": kept_from,
        "aligned_with": aligned,
        "resolved": resolved,
        "conclusion": conclusion,
        "prior_verdicts": {mid: prior[mid] for mid in prior},
        "grok_verdict": verdict,
    }


def grok_tiebreak_hook(review: dict, frames_dir: str, code: str = "") -> dict | None:
    """Generic argv hint for an external runner, or None when Grok stays out.

    Does not call Magpie, read secrets, scrape, or promote. It does not know
    about any batch orchestrator.
    """
    if not isinstance(review, dict):
        return None
    trigger = tiebreak_trigger(review)
    if not trigger:
        return None
    resolved = _norm_code(code or review.get("code") or "")
    argv = ["grok-review"]
    if resolved:
        argv.append(resolved)
    argv.extend(["--frames-dir", str(frames_dir)])
    return {"needed": True, "trigger": trigger, "argv": argv}


def select_tiebreak_frames(frames_dir: Path, manifest: dict, review: dict) -> list[dict]:
    """Already-extracted valid frames only.

    Invalid, empty, cover, still, Thumb, and Backdrop files are left out,
    even if they sit in the same directory.
    """
    allowed: set[str] | None = None
    listed = review.get("valid_frames") if isinstance(review, dict) else None
    if isinstance(listed, list):
        allowed = set()
        for item in listed:
            if isinstance(item, dict):
                name = str(item.get("file") or "")
            else:
                name = str(item or "")
            name = Path(name).name
            if name and not _frame_blocked(name):
                allowed.add(name)
    root = frames_dir.resolve()
    chosen: list[dict] = []
    seen: set[str] = set()
    shots = manifest.get("shots") if isinstance(manifest, dict) else None
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        if not shot.get("ok") or shot.get("valid") is not True or shot.get("discarded"):
            continue
        raw_name = str(shot.get("file") or "")
        name = Path(raw_name).name
        label = str(shot.get("label") or "")
        if not name or name != raw_name or name in seen:
            continue
        if _frame_blocked(name) or _frame_blocked(label):
            continue
        if Path(name).suffix.lower() not in _FRAME_EXT:
            continue
        if allowed is not None and name not in allowed:
            continue
        path = frames_dir / name
        try:
            resolved = path.resolve()
            size = resolved.stat().st_size
        except (OSError, RuntimeError):
            continue
        if resolved.parent != root or not resolved.is_file() or size <= 500:
            continue
        seen.add(name)
        chosen.append({"label": label, "ss": shot.get("ss"), "file": name, "path": resolved})
    return chosen


def build_grok_review_document(
    *,
    code: str,
    frames_dir: str,
    trigger: str,
    review: dict,
    prior: dict[str, dict],
    frames: list[dict],
    grok: dict,
    started_at: str,
    finished_at: str,
) -> dict:
    decision = decide_tiebreak({mid: row["verdict"] for mid, row in prior.items()}, grok.get("verdict") or "")
    decision["trigger"] = trigger
    return {
        "code": code,
        "role": "tiebreak",
        "model": GROK_TIEBREAK_MODEL,
        "frames_dir": frames_dir,
        "source_review": "review.json",
        "valid_frames": [{"label": fr.get("label"), "ss": fr.get("ss"), "file": fr.get("file")} for fr in frames],
        "valid_n": len(frames),
        "trigger": trigger,
        "verdict": grok.get("verdict") or "uncertain",
        "confidence": grok.get("confidence") or "",
        "reason": grok.get("reason") or "",
        "kept_verdict": decision["kept_verdict"],
        "conclusion": decision["conclusion"],
        "prior": {
            "agreement": (review or {}).get("agreement") or "",
            "consensus": (review or {}).get("consensus") or "",
            "models": prior,
        },
        "grok": grok,
        "tiebreak": decision,
        "started_at": started_at,
        "finished_at": finished_at,
        "notes": [
            "未重跑 DeepSeek-flash / gpt-6-luna。",
            "未改 review.json，未改 media_llm_route.json。",
            "未使用 MiniMax，未刮削，未 promote。",
            "只送已抽出的有效帧，不含封面、剧照、Thumb、Backdrop，不含无效帧。",
        ],
    }


def _grok_result_fields(rec: dict) -> dict:
    parsed = rec.get("parsed") if isinstance(rec.get("parsed"), dict) else {}
    raw_verdict = parsed.get("verdict")
    verdict = raw_verdict if raw_verdict in _VERDICTS else "uncertain"
    reason = parsed.get("reason") or rec.get("error") or ""
    if raw_verdict not in _VERDICTS:
        reason = reason or "响应无法解析为约定 JSON"
    hits = parsed.get("hits") if isinstance(parsed.get("hits"), list) else []
    return {
        "model": GROK_TIEBREAK_MODEL,
        "ok": bool(rec.get("ok") and raw_verdict in _VERDICTS),
        "verdict": verdict,
        "hits": hits,
        "reason": _redact_media(str(reason or ""))[:500],
        "confidence": parsed.get("confidence") or "",
        "elapsed_s": rec.get("elapsed_s"),
        "http": rec.get("http"),
        "attempts": rec.get("attempts") or 1,
        "usage": rec.get("usage") if isinstance(rec.get("usage"), dict) else {},
    }


def _grok_tiebreak_once(cfg: dict, images: list[Path], user_text: str) -> dict:
    model = GROK_TIEBREAK_MODEL
    if model != "cursor/grok-4.7" or "minimax" in model.lower():
        _die("Grok 复审只允许临时指定 cursor/grok-4.7，不改 media_llm_route，不用 MiniMax")
    return _post_magpie_chat(cfg, model, images, REVIEW_SYSTEM, user_text, REVIEW_TO, 800)


def _grok_tiebreak_chat(cfg: dict, images: list[Path], user_text: str, tries: int = 3) -> dict:
    last: dict = {}
    for i in range(1, tries + 1):
        rec = _grok_tiebreak_once(cfg, images, user_text)
        rec["attempts"] = i
        last = rec
        parsed = rec.get("parsed") if isinstance(rec.get("parsed"), dict) else None
        if rec.get("ok") and parsed and parsed.get("verdict") in _VERDICTS:
            return rec
        if not rec.get("retryable") and not (rec.get("ok") and not parsed):
            return rec
        time.sleep(2 * i)
    return last


def _grok_frames_dir(args) -> tuple[str, Path, int]:
    code = _norm_code(getattr(args, "code", "") or "")
    raw = str(getattr(args, "frames_dir", "") or getattr(args, "out", "") or "").strip()
    if raw:
        return code, Path(raw), 0
    if not code:
        _die("请指定番号，或用 --frames-dir 指向已有 review.json 的帧目录")
    out, rc = _resolve_marker_dir(code, "review.json")
    return code, out, rc


def _write_grok_review(frames_dir: Path, doc: dict, secret: str) -> Path:
    dest = frames_dir / "grok_review.json"
    review_path = frames_dir / "review.json"
    if dest.name != "grok_review.json":
        _die("Grok 复审只能写 grok_review.json")
    if review_path.exists() and dest.resolve() == review_path.resolve():
        _die("拒绝覆盖 review.json")
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if secret and secret in text:
        _die("结果里出现了 Key，已中止写入")
    tmp = frames_dir / "grok_review.json.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    return dest


def cmd_grok_review(args) -> int:
    """Tiebreak one existing dual-model review with cursor/grok-4.7.

    Does not rerun DeepSeek or gpt-6-luna, does not change media_llm_route.json,
    does not overwrite review.json, and does not promote.
    """
    code, out, dir_rc = _grok_frames_dir(args)
    if dir_rc != 0:
        return dir_rc
    review_path = out / "review.json"
    if not review_path.is_file():
        _die(f"找不到 {review_path}。请先跑 av_auto review；Grok 不重跑 DeepSeek / gpt-6-luna。")
    before = review_path.read_bytes()
    review = _read_json_dict(review_path)
    file_code = _norm_code(str(review.get("code") or ""))
    if code and file_code and code != file_code:
        _die(f"番号不一致：参数 {code}，review.json 是 {file_code}")
    code = code or file_code
    if not code:
        _die("review.json 里没有番号")
    trigger = tiebreak_trigger(review)
    if not trigger:
        if _persist_frames_dir(code, out) != 0:
            return 1
        agreement = review.get("agreement") or ""
        consensus = review.get("consensus") or ""
        stale = ""
        if (out / "grok_review.json").is_file():
            stale = " 已有 grok_review.json 未改动，以这次一致结论为准。"
        print(f"tiebreak SKIP {agreement}/{consensus}  两家已有明确一致结论，不调用 Grok，不写 grok_review.json。{stale}")
        return 0
    manifest_path = out / "frames_manifest.json"
    if not manifest_path.is_file():
        _die(f"找不到 {manifest_path}，无法确认有效帧。请使用 av_auto frames 预检后的目录。")
    manifest = _read_json_dict(manifest_path)
    if not manifest.get("precheck"):
        _die("清单未经有效性预检。不要把未预检帧送给 Grok。")
    frames = select_tiebreak_frames(out, manifest, review)
    if not frames:
        _die(f"{code} 没有可送 Grok 的有效帧（无效帧、空帧、封面、剧照、Thumb、Backdrop 都不送）")
    llm = _load_llm_runtime()
    captions = "、".join(f"{fr['label']}@{fr['ss']}s" for fr in frames)
    user = (
        f"番号 {code}。本请求只附已经抽出的有效抽帧（本地缩图的 data URI，不是网址；"
        f"不含封面、剧照、Thumb、Backdrop，也不含预检无效的帧）：{captions}。"
        "请只根据这些帧给出 JSON 判断。"
    )
    prior = prior_model_verdicts(review)
    prior_bits = " ".join(f"{_model_short(mid)}:{row['verdict']}" for mid, row in prior.items())
    print(
        f"grok-review {code}: trigger={trigger} prior={prior_bits} frames={len(frames)} → {GROK_TIEBREAK_MODEL}"
    )
    started = datetime.now(TZ)
    rec = _grok_tiebreak_chat(llm, [fr["path"] for fr in frames], user)
    grok = _grok_result_fields(rec)
    finished = datetime.now(TZ)
    doc = build_grok_review_document(
        code=code,
        frames_dir=str(out),
        trigger=trigger,
        review=review,
        prior=prior,
        frames=frames,
        grok=grok,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
    )
    if review_path.read_bytes() != before:
        _die("review.json 被改动，已中止，不写 grok_review.json")
    written = _write_grok_review(out, doc, str(llm.get("key") or ""))
    if review_path.read_bytes() != before:
        _die("review.json 被改动")
    if _persist_frames_dir(code, out) != 0:
        return 1
    if review_path.read_bytes() != before:
        _die("review.json 被改动")
    official_grok = _durable_frames_dir(code) / written.name
    tag = "KEPT" if doc["tiebreak"]["resolved"] else "UNRESOLVED"
    print(f"  {GROK_TIEBREAK_MODEL}: {grok['verdict']} {grok['confidence']} {grok['reason']}".rstrip())
    print(f"tiebreak {tag} {doc['tiebreak']['kept_verdict']}  {doc['tiebreak']['conclusion']}")
    print(f"→ {official_grok}")
    return 0


def _emby_refresh(paths: dict, dest: Path) -> str:
    import httpx

    host, key = _emby_auth()
    headers = {"X-Emby-Token": key, "Content-Type": "application/json"}
    notes = []
    with httpx.Client(timeout=30.0) as client:
        # Tell Emby the new/updated folder appeared.
        r = client.post(
            f"{host}/Library/Media/Updated",
            headers=headers,
            json={"Updates": [{"Path": str(dest), "UpdateType": "Created"}]},
        )
        notes.append(f"Media/Updated {r.status_code}")
        libs = client.get(f"{host}/Library/VirtualFolders", headers=headers)
        item_id = ""
        wanted = paths["emby_library"].casefold()
        if libs.status_code == 200:
            for lib in libs.json() or []:
                if str(lib.get("Name") or "").casefold() == wanted:
                    item_id = str(lib.get("ItemId") or lib.get("Guid") or lib.get("Id") or "")
                    break
        if item_id:
            r2 = client.post(
                f"{host}/Items/{item_id}/Refresh",
                headers=headers,
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "Default",
                    "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false",
                    "Reload": "true",
                },
            )
            notes.append(f"Refresh {paths['emby_library']} {r2.status_code}")
        else:
            notes.append(f"未找到 Emby 库 {paths['emby_library']}，只做了路径通知")
    return "; ".join(notes)


def _promote_one(pan, rest, paths, code: str, dry: bool) -> str:
    build_play_url, destination, find_existing, safe_name, write_library = rest
    src_path = f"{paths['staging_115'].rstrip('/')}/{code}"
    dst_path = f"{paths['official_115'].rstrip('/')}/{code}"
    src_id = pan.dir_id(src_path)
    if not _id_ok(src_id):
        return f"FAIL {code}: 115 上没有 {src_path}，未动"
    if _id_ok(pan.dir_id(dst_path)):
        return f"FAIL {code}: 正式库已有 {dst_path}，整夹留在 av-auto，未搬"
    videos = _videos_in(pan, src_id)
    official_id = pan.dir_id(paths["official_115"])
    if not _id_ok(official_id):
        return f"FAIL {code}: 正式库 {paths['official_115']} 不存在，未动"
    staging_parent = pan.dir_id(paths["staging_115"])
    existing = find_existing(paths["official_strm"], code)
    dest = existing or destination(paths["official_strm"], code, "")
    staging_dir = paths["staging_strm"] / safe_name(code)
    if dry:
        return (
            f"DRY {code}: 将把 {src_path} → {dst_path}，"
            f"写正式 strm {dest}（{'已有夹' if existing else '新建'}），"
            f"然后刷新 Emby、删除 {staging_dir}；成片 {len(videos)} 个"
        )
    if not videos:
        return f"FAIL {code}: {src_path} 里没有成片，未搬"

    # 1) 115 整夹搬家
    try:
        pan.move(src_id, official_id)
    except Exception as exc:
        return f"FAIL {code}: 115 搬家失败（{exc}），仍在 av-auto"
    time.sleep(0.8)
    if not _id_ok(pan.dir_id(dst_path)) or _id_ok(pan.dir_id(src_path)):
        # 尝试视为失败并保持原状（若已部分过去则再试搬回）
        if _id_ok(pan.dir_id(dst_path)) and _id_ok(staging_parent):
            try:
                pan.move(pan.dir_id(dst_path), staging_parent)
            except Exception:
                pass
        return f"FAIL {code}: 搬家后核验失败，已尝试留在/搬回 av-auto"

    # 2) 写正式 strm（失败则搬回 115）
    def _rollback(reason: str) -> str:
        try:
            now = pan.dir_id(dst_path)
            if _id_ok(now) and _id_ok(staging_parent):
                pan.move(now, staging_parent)
            time.sleep(0.5)
            back = _id_ok(pan.dir_id(src_path))
            return f"FAIL {code}: {reason}；115 已回滚到 av-auto" if back else f"FAIL {code}: {reason}；回滚失败，片子可能已在 {dst_path}，staging strm 保留"
        except Exception as exc:
            return f"FAIL {code}: {reason}；回滚异常（{exc}），片子可能已在 {dst_path}，staging strm 保留"

    try:
        moved_videos = _videos_in(pan, pan.dir_id(dst_path))
        if not moved_videos:
            return _rollback("搬家后正式夹里看不到成片")
        media_mkdir(dest)
        written = _write_strm_dir(dest, paths["play_base"], build_play_url, safe_name, moved_videos, code)
        if not written:
            return _rollback("未能写正式 strm（缺 pickcode）")
    except Exception as exc:
        return _rollback(f"写正式 strm 失败（{exc}）")

    # 3) Emby 刷新（失败不回滚片子：正式 strm 已就绪，刷新可后补）
    emby_note = ""
    try:
        emby_note = _emby_refresh(paths, dest)
    except Exception as exc:
        emby_note = f"Emby 刷新失败（{exc}），正式 strm 已写好，可稍后手动刷新"

    # 4) 清 staging strm（仅在 115 已不在 av-auto 且正式 strm 写好之后）
    if staging_dir.is_dir() or _is_media_path(staging_dir):
        media_rmtree(staging_dir)
    return f"OK {code}: {src_path} → {dst_path}；strm {dest}；{emby_note}"


def cmd_promote(args) -> int:
    codes = [_norm_code(c) for c in (args.codes or [])]
    if not codes:
        _die("用法: av_auto promote [--dry-run] 番号 [番号...]")
    pan, rest = _pan()
    paths = _paths()
    rc = 0
    for code in codes:
        line = _promote_one(pan, rest, paths, code, bool(args.dry_run))
        print(line)
        if line.startswith("FAIL"):
            rc = 1
    return rc



def _task_status_bucket(task: dict) -> str:
    # Prefer 115 display fields. Note: list API filter `stat` (1-9 fail, 10-11 done, 12 running)
    # is NOT the per-task `status` (2 = 下载成功).
    display = str(task.get("display_status") or "").strip().lower()
    text = str(task.get("status_text") or "")
    if "失败" in text or display in {"failed", "fail", "error"}:
        return "fail"
    if display in {"finished", "complete", "completed", "success", "done"} or "成功" in text or "完成" in text:
        return "done"
    raw = task.get("status", task.get("stat", task.get("state")))
    try:
        st = int(raw)
    except (TypeError, ValueError):
        if any(w in display or w in text.lower() for w in ("run", "down", "进行", "等待")):
            return "run"
        return "unknown"
    if st == 2:
        return "done"
    if st in (0, 1):
        return "run"
    # leftover numeric statuses: treat 100% + file_id as done
    try:
        pct = float(task.get("percentDone") or task.get("display_percent") or 0)
    except (TypeError, ValueError):
        pct = 0
    if pct >= 100 and task.get("file_id"):
        return "done"
    if st in (3, 4, 5, 6, 7, 8, 9):
        return "fail"
    return "run"


def _task_matches(task: dict, code: str, inbox_id: str) -> bool:
    code_u = (code or "").upper()
    blob = " ".join(
        str(task.get(k) or "")
        for k in ("name", "url", "dir_name", "path", "savepath", "move_path", "wp_path", "file_name")
    ).upper()
    if code_u and code_u in blob:
        return True
    pid = str(task.get("wp_path_id") or task.get("cid") or task.get("dir_id") or task.get("pid") or "")
    return _id_ok(inbox_id) and pid == str(inbox_id)


def _list_tasks(client, code: str = "", inbox_id: str = "") -> list[dict]:
    found: list[dict] = []
    page = 1
    while page <= 10:
        raw = client.clouddownload_task_list({"page": page, "page_size": 30, "stat": 0})
        if not isinstance(raw, dict):
            break
        tasks = raw.get("tasks") or raw.get("data") or raw.get("info") or []
        if isinstance(tasks, dict):
            tasks = tasks.get("tasks") or tasks.get("list") or []
        if not isinstance(tasks, list):
            break
        if not tasks:
            break
        for task in tasks:
            if isinstance(task, dict) and (not code or _task_matches(task, code, inbox_id)):
                found.append(task)
        if len(tasks) < 30:
            break
        page += 1
    return found


def _summarize_task(task: dict) -> str:
    name = str(task.get("name") or task.get("file_name") or "")
    bucket = _task_status_bucket(task)
    pct = task.get("percentDone") or task.get("percent") or task.get("process") or task.get("rate") or ""
    ih = str(task.get("info_hash") or task.get("infohash") or "")[:12]
    return f"{bucket} name={name or '(unnamed)'} pct={pct} hash={ih}..."


def _redact_result(obj) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    except Exception:
        text = str(obj)
    return _redact_media(text)[:500]


def cmd_submit(args) -> int:
    code = _norm_code(args.code)
    url = (args.url or "").strip()
    if not code or not url:
        _die("用法: av_auto submit --code 番号 --url 磁链或ed2k")
    if not (url.startswith("magnet:") or url.startswith("ed2k://") or url.startswith("http")):
        _die("只接受 magnet / ed2k / http(s) 链接")
    paths = _paths()
    pan, _ = _pan()
    pan.ensure_dir(paths["staging_115"])
    inbox_root = pan.ensure_dir(paths["staging_inbox"])
    if not _id_ok(inbox_root):
        _die(f"无法创建 {paths['staging_inbox']}")
    dest = f"{paths['staging_inbox'].rstrip('/')}/{code}"
    dest_id = pan.ensure_dir(dest)
    if not _id_ok(dest_id):
        _die(f"无法创建 {dest}")
    # already have keepers in /av-auto/<code>? still allow submit for verify, but warn
    lib = f"{paths['staging_115'].rstrip('/')}/{code}"
    if _id_ok(pan.dir_id(lib)) and _videos_in(pan, pan.dir_id(lib)):
        print(f"NOTE {code}: {lib} 里已有正片，仍提交到 inbox")
    result = pan.add_magnet(url, dest_id)
    state = result.get("state") if isinstance(result, dict) else None
    errno = result.get("errno") or result.get("error_code") or result.get("errcode")
    print(f"SUBMIT {code} → {dest} state={state} errno={errno}")
    print(_redact_result(result))
    if isinstance(result, dict) and (state is False or (errno not in (None, 0, "0") and state not in (True, 1, "1"))):
        return 1
    return 0


def cmd_wait(args) -> int:
    code = _norm_code(args.code)
    timeout = int(args.timeout or 0)
    paths = _paths()
    pan, _ = _pan()
    client = _p115()
    dest = f"{paths['staging_inbox'].rstrip('/')}/{code}"
    dest_id = pan.dir_id(dest)
    started = time.time()
    last = ""
    while True:
        tasks = _list_tasks(client, code, dest_id)
        buckets = [_task_status_bucket(t) for t in tasks]
        line = "; ".join(_summarize_task(t) for t in tasks) or "no matching task yet"
        if line != last:
            print(f"WAIT {code}: {line}")
            last = line
        if tasks and all(b == "done" for b in buckets):
            print(f"OK {code}: 离线完成")
            return 0
        if tasks and any(b == "fail" for b in buckets) and not any(b == "run" for b in buckets):
            print(f"FAIL {code}: 离线失败")
            return 1
        if timeout and time.time() - started >= timeout:
            print(f"TIMEOUT {code}: {int(time.time()-started)}s 仍未完成")
            return 2
        time.sleep(max(5, WAIT_POLL_SEC))


def cmd_clean(args) -> int:
    code = _norm_code(args.code)
    paths = _paths()
    min_bytes = _min_bytes(paths)
    pan, _ = _pan()
    client = _p115()
    inbox = f"{paths['staging_inbox'].rstrip('/')}/{code}"
    dest = f"{paths['staging_115'].rstrip('/')}/{code}"
    inbox_id = pan.dir_id(inbox)
    if not _id_ok(inbox_id):
        _die(f"115 上没有 inbox {inbox}，无法清杂质")
    dest_id = pan.ensure_dir(dest)
    if not _id_ok(dest_id):
        _die(f"无法创建 {dest}")
    items = _walk(pan, inbox_id)
    keepers = [r for r in items if not r["is_dir"] and _is_keeper(r["name"], r["size"], min_bytes)]
    junk_dirs = [r for r in items if r["is_dir"]]
    if not keepers:
        already = _videos_in(pan, dest_id, min_bytes)
        if not already:
            print(f"FAIL {code}: inbox 里没有达标正片（>{paths['min_size_mb']}MB 且非预告/广告），不搬、不删 inbox，便于排查")
            print(f"  files={len([r for r in items if not r['is_dir']])} dirs={len(junk_dirs)}")
            return 1
        # Feature already lives in /av-auto/<code>/. A rerun must not fail just
        # because the inbox folder is empty or only still holds junk.
        print(f"SKIP clean {code}: inbox 已无达标正片，正片已在 {dest}")
        moved = 0
    else:
        moved = 0
        for rec in keepers:
            if rec["id"] and rec["id"] != dest_id:
                pan.move(rec["id"], dest_id)
                moved += 1
                print(f"KEEP {code}: {rec['name']} size={rec['size']} → {dest}")
    # re-walk inbox after moves
    left = _walk(pan, inbox_id)
    leftover_files = [r for r in left if not r["is_dir"]]
    leftover_dirs = [r for r in left if r["is_dir"]]
    ids = [r["id"] for r in leftover_files if _id_ok(r["id"])]
    # delete nested dirs deepest-first (longer path / later in walk)
    ids.extend(reversed([r["id"] for r in leftover_dirs if _id_ok(r["id"])]))
    deleted = 0
    if ids:
        try:
            client.fs_delete({"fid": ",".join(ids), "ignore_warn": 1})
            deleted = len(ids)
        except Exception as exc:
            print(f"WARN {code}: 清空残留失败: {_redact_media(str(exc))}")
            return 1
    try:
        client.fs_delete({"fid": str(inbox_id), "ignore_warn": 1})
        print(f"CLEAN {code}: 已删空 inbox 夹 {inbox}")
    except Exception:
        print(f"CLEAN {code}: 残留已清，inbox 夹仍在")
    print(f"OK {code}: 正片 {moved} 已进 {dest}，删除残留 {deleted}")
    return 0


def cmd_intake(args) -> int:
    """submit → wait → clean → strm. Never promote. Frames optional."""
    rc = cmd_submit(args)
    if rc != 0:
        return rc
    wait_args = argparse.Namespace(code=args.code, timeout=getattr(args, "timeout", 0) or 0)
    rc = cmd_wait(wait_args)
    if rc != 0:
        return rc
    rc = cmd_clean(argparse.Namespace(code=args.code))
    if rc != 0:
        return rc
    rc = cmd_strm(argparse.Namespace(codes=[args.code]))
    if rc != 0:
        return rc
    if getattr(args, "frames", False):
        out = getattr(args, "out", "") or ""
        cleanup = bool(getattr(args, "cleanup", False))
        rc = cmd_frames(argparse.Namespace(
            code=args.code, strm="", out=out, cleanup=cleanup,
            no_precheck=bool(getattr(args, "no_precheck", False)),
            no_reuse=False,
        ))
        if rc == 0 and getattr(args, "review", False):
            rc = cmd_review(argparse.Namespace(code=args.code, out=out, cleanup=cleanup))
    if rc == 0:
        print(f"INTAKE { _norm_code(args.code) }: 完成（未 promote，未刮削）")
    return rc


def _frames_root_from_env(name: str, default: Path) -> Path:
    raw = (os.environ.get(name) or "").strip()
    return Path(raw) if raw else default


def _official_frames_root() -> Path:
    return _frames_root_from_env("AV_AUTO_FRAMES_ROOT", OFFICIAL_FRAMES_ROOT)


def _tmp_frames_root() -> Path:
    return _frames_root_from_env("AV_AUTO_FRAMES_TMP", TMP_FRAMES_ROOT)


def _official_frames_dir(code: str) -> Path:
    return _official_frames_root() / code


def _tmp_frames_dir(code: str) -> Path:
    return _tmp_frames_root() / code


def _candidate_frames_dirs(code: str) -> list[Path]:
    """Durable directory first, then container tmp.

    Later runs must reuse official artifacts. A /tmp-only tree is not enough.
    """
    return [_official_frames_dir(code), _tmp_frames_dir(code)]


def _durable_frames_dir(code: str) -> Path:
    dirs = _candidate_frames_dirs(code)
    if not dirs:
        return _official_frames_dir(code)
    return dirs[0]


def _same_frames_path(a: Path, b: Path) -> bool:
    try:
        if a.exists() or b.exists():
            return a.resolve() == b.resolve()
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))


def _is_frames_artifact(name: str) -> bool:
    if not name or name.endswith(".tmp") or name.endswith(".persist-tmp"):
        return False
    if name in _PERSIST_JSON:
        return True
    return Path(name).suffix.lower() in _FRAME_EXT


def _copy_frames_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.persist-tmp"
    try:
        tmp.write_bytes(src.read_bytes())
        os.chmod(tmp, 0o644)
        tmp.replace(dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _copy_frames_tree(src: Path, dst: Path) -> None:
    """Copy frame artifacts onto dst. Never rename grok output onto review.json."""
    if _same_frames_path(src, dst):
        return
    if not src.is_dir():
        raise OSError(errno.ENOENT, "frames directory missing", str(src))
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if not child.is_file() or not _is_frames_artifact(child.name):
            continue
        if child.name != "review.json" and (dst / child.name).name == "review.json":
            raise OSError(errno.EPERM, "refusing to overwrite review.json")
        _copy_frames_file(child, dst / child.name)


def _persist_error(code: str, official: Path, exc: OSError) -> int:
    err = getattr(exc, "strerror", None) or str(exc)
    print(
        f"FAIL {code}: 未能把帧写入持久目录 {official}（{err}）。"
        "只写在容器 /tmp 不算完成。",
        file=sys.stderr,
    )
    return 1


def _persist_frames_dir(code: str, source: Path, *, require_review: bool = False) -> int:
    """Land completed artifacts in the official frames directory.

    Returns 0 only when that directory holds them. A container-tmp write alone
    is a failure, not success.
    """
    official = _durable_frames_dir(code)
    try:
        if not source.is_dir():
            raise OSError(errno.ENOENT, "frames directory missing", str(source))
        _copy_frames_tree(source, official)
        if _frames_ready(source) and not _frames_ready(official):
            raise OSError(errno.EIO, "official frames are not ready")
        if _frames_ready(official):
            manifest = json.loads((official / "frames_manifest.json").read_text(encoding="utf-8"))
            for shot in manifest.get("shots") or []:
                if not (isinstance(shot, dict) and shot.get("ok") and shot.get("valid") is True and shot.get("file")):
                    continue
                name = Path(str(shot.get("file"))).name
                src_file = source / name
                dst_file = official / name
                if not src_file.is_file():
                    continue
                if not dst_file.is_file() or dst_file.stat().st_size != src_file.stat().st_size:
                    raise OSError(errno.EIO, f"frame {name} was not persisted")
        src_review = source / "review.json"
        dst_review = official / "review.json"
        if src_review.is_file():
            if not dst_review.is_file():
                raise OSError(errno.EIO, "official review.json is missing")
            if not _same_frames_path(source, official) and dst_review.read_bytes() != src_review.read_bytes():
                raise OSError(errno.EIO, "official review.json does not match")
        if require_review and _complete_review(official) is None:
            raise OSError(errno.EIO, "official review.json is not a dual-model review")
        src_grok = source / "grok_review.json"
        if src_grok.is_file():
            dst_grok = official / "grok_review.json"
            if not dst_grok.is_file() or dst_grok.read_bytes() != src_grok.read_bytes():
                raise OSError(errno.EIO, "official grok_review.json is missing")
            if dst_review.is_file() and dst_review.read_bytes() == dst_grok.read_bytes():
                raise OSError(errno.EPERM, "review.json matches grok_review.json")
    except OSError as exc:
        return _persist_error(code, official, exc)
    print(f"persist {code}: {official}")
    return 0


def _scratch_has_artifacts(folder: Path) -> bool:
    if _frames_ready(folder) or _complete_review(folder) is not None:
        return True
    return (folder / "grok_review.json").is_file() and (folder / "review.json").is_file()


def _select_frames_dir(code: str) -> tuple[Path, int]:
    """Official frames directory after any container-tmp artifacts are copied in.

    A non-zero return means the durable directory could not be updated.
    """
    official = _durable_frames_dir(code)
    try:
        official.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return official, _persist_error(code, official, exc)
    if _frames_ready(official) or _complete_review(official) is not None:
        return official, 0
    for folder in _candidate_frames_dirs(code)[1:]:
        if _same_frames_path(folder, official) or not _scratch_has_artifacts(folder):
            continue
        rc = _persist_frames_dir(code, folder)
        return official, rc
    return official, 0


def _resolve_marker_dir(code: str, marker: str) -> tuple[Path, int]:
    """Use the official dir, copying a scratch dir that already has marker."""
    official = _durable_frames_dir(code)
    if (official / marker).is_file():
        return official, 0
    for folder in _candidate_frames_dirs(code)[1:]:
        if (folder / marker).is_file():
            rc = _persist_frames_dir(code, folder)
            return official, rc
    return official, 0


def _frames_ready(folder: Path) -> bool:
    """True when prechecked valid frames are already on disk and can be reviewed."""
    path = folder / "frames_manifest.json"
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(manifest, dict) or not manifest.get("precheck"):
        return False
    shots = [
        shot for shot in (manifest.get("shots") or [])
        if isinstance(shot, dict) and shot.get("ok") and shot.get("valid") is True and shot.get("file")
    ]
    if not shots:
        return False
    for shot in shots:
        frame = folder / str(shot.get("file"))
        try:
            if not frame.is_file() or frame.stat().st_size <= 500:
                return False
        except OSError:
            return False
    return True


def _complete_review(folder: Path) -> dict | None:
    """Dual-model review.json, or None when final review still has to run."""
    path = folder / "review.json"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    prior = prior_model_verdicts(doc)
    if not set(REVIEW_MODELS).issubset(prior):
        return None
    return doc


def _grok_already_done(folder: Path, trigger: str, code: str) -> bool:
    path = folder / "grok_review.json"
    if not path.is_file():
        return False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(doc, dict):
        return False
    recorded = _norm_code(str(doc.get("code") or ""))
    if recorded and recorded != code:
        return False
    if str(doc.get("trigger") or "") != trigger:
        return False
    return doc.get("verdict") in _VERDICTS


def _offline_layout(code: str) -> dict:
    """Read inbox/staging presence. Does not submit, delete, move, or promote."""
    paths = _paths()
    pan, _rest = _pan()
    min_bytes = _min_bytes(paths)
    inbox = f"{paths['staging_inbox'].rstrip('/')}/{code}"
    dest = f"{paths['staging_115'].rstrip('/')}/{code}"
    inbox_id = pan.dir_id(inbox)
    dest_id = pan.dir_id(dest)
    keepers = len(_videos_in(pan, dest_id, min_bytes)) if _id_ok(dest_id) else 0
    return {
        "inbox": _id_ok(inbox_id),
        "dest": _id_ok(dest_id),
        "dest_keepers": keepers,
        "inbox_path": inbox,
        "dest_path": dest,
    }


def _staging_strm_ready(code: str) -> bool:
    paths = _paths()
    name = code
    try:
        sys.path.insert(0, str(PLUGIN_DIR))
        from av115_library import safe_name

        name = safe_name(code) or code
    except Exception:
        name = code
    folder = paths["staging_strm"] / name
    if not folder.is_dir():
        return False
    for path in folder.glob("*.strm"):
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _process_clean(code: str) -> int:
    layout = _offline_layout(code)
    if layout.get("inbox"):
        print(f"clean {code}: inbox {layout.get('inbox_path')}")
        return int(cmd_clean(argparse.Namespace(code=code)) or 0)
    if int(layout.get("dest_keepers") or 0) > 0:
        print(f"SKIP clean {code}: inbox 已空，正片已在 {layout.get('dest_path')}")
        return 0
    print(
        f"FAIL {code}: /av-auto/_staging/{code} 与 /av-auto/{code} 都没有正片。"
        "不交磁、不刮削、不 promote。",
        file=sys.stderr,
    )
    return 1


def _process_strm(code: str) -> int:
    apply_pending_queue()
    if _staging_strm_ready(code):
        print(f"SKIP strm {code}: staging strm 已存在")
        return 0
    if _pending_strm_payload(code):
        print(
            f"NOTE: {code} 的 staging strm 已在 pending，不再重复排队。"
            "抽帧使用排队内容；盘上的文件由 av_auto_apply_fs 落盘。",
            flush=True,
        )
        return 0
    return int(cmd_strm(argparse.Namespace(codes=[code])) or 0)


def _process_frames(code: str, folder: Path) -> int:
    if _frames_ready(folder):
        print(f"SKIP frames {code}: 已有预检有效帧 → {folder}")
        return 0
    # Keep the frames. grok-review reads this directory and must not see a cleanup.
    rc = int(cmd_frames(argparse.Namespace(
        code=code,
        strm="",
        out=str(folder),
        cleanup=False,
        no_precheck=False,
        no_reuse=False,
    )) or 0)
    if rc != 0:
        return rc
    if not _frames_ready(folder):
        print(f"FAIL {code}: 抽帧未留下可送终审的预检有效帧", file=sys.stderr)
        return 1
    return 0


def _process_review(code: str, folder: Path) -> tuple[dict, int]:
    existing = _complete_review(folder)
    if existing is not None:
        recorded = _norm_code(str(existing.get("code") or ""))
        if not recorded or recorded == code:
            print(
                f"SKIP review {code}: 已有双模型 review.json"
                f"（{existing.get('agreement')}/{existing.get('consensus')}）"
            )
            return existing, 0
    rc = int(cmd_review(argparse.Namespace(code=code, out=str(folder), cleanup=False)) or 0)
    if rc != 0:
        return {}, rc
    doc = _complete_review(folder)
    if doc is None:
        print(f"FAIL {code}: review 未写出双模型 review.json", file=sys.stderr)
        return {}, 1
    return doc, 0


def _process_grok(code: str, folder: Path, review: dict) -> int:
    trigger = tiebreak_trigger(review)
    if not trigger:
        print(
            f"SKIP grok-review {code}: {review.get('agreement')}/{review.get('consensus')}"
            " 两家已有明确结论，不调用 Grok"
        )
        return 0
    if _grok_already_done(folder, trigger, code):
        print(f"SKIP grok-review {code}: 已有 grok_review.json（trigger={trigger}），不重复调用")
        return 0
    return int(cmd_grok_review(argparse.Namespace(code=code, frames_dir=str(folder), out="")) or 0)


def _process_offline_title(code: str) -> int:
    """clean → strm → frames → review → grok only when still unresolved.

    Never submits offline, scrapes, or promotes.
    """
    print(
        f"PROCESS {code}: clean → strm → frames → review → grok-review（仅 uncertain / 分歧）"
        " → 持久帧目录；不交磁、不刮削、不 promote"
    )
    rc = _process_clean(code)
    if rc:
        return rc
    rc = _process_strm(code)
    if rc:
        return rc
    folder, rc = _select_frames_dir(code)
    if rc:
        return rc
    rc = _process_frames(code, folder)
    if rc:
        return rc
    review, rc = _process_review(code, folder)
    if rc:
        return rc
    rc = _process_grok(code, folder, review)
    if rc:
        return rc
    return _persist_frames_dir(code, folder, require_review=True)


def cmd_process(args) -> int:
    """Official post-offline path. Command first, then one or more catalog codes."""
    raw_codes = list(getattr(args, "codes", None) or [])
    codes: list[str] = []
    for raw in raw_codes:
        if not looks_like_catalog_code(str(raw)):
            _die(
                f"process 只接受番号，收到 {raw!r}。"
                f"用法: av_auto process 番号 [番号...]  例如: {PROCESS_EXAMPLE}",
                2,
            )
        codes.append(_norm_code(str(raw)))
    if not codes:
        _die(f"用法: av_auto process 番号 [番号...]  例如: {PROCESS_EXAMPLE}", 2)
    # Land an earlier run's pending writes if /media accepts them now.
    # A stale shareFloor still leaves the jobs for the host apply_fs.
    apply_pending_queue()
    failed: list[str] = []
    for code in codes:
        try:
            rc = int(_process_offline_title(code) or 0)
        except SystemExit as exc:
            status = exc.code
            rc = status if isinstance(status, int) and status else 1
        if rc:
            failed.append(code)
            print(
                f"FAIL {code}: process 停在非 0（{rc}）。不交磁、不刮削、不 promote。",
                file=sys.stderr,
            )
        else:
            print(f"PROCESS {code}: 完成（未交磁、未刮削、未 promote）")
    if failed:
        print("PROCESS 未完成: " + " ".join(failed), file=sys.stderr)
        print("正确用法: av_auto process " + " ".join(codes), file=sys.stderr)
        return 1
    print("PROCESS 全部完成: " + " ".join(codes) + "（未交磁、未刮削、未 promote）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="av_auto",
        description=(
            "115 av-auto 通路：p115client 交到暂存、清杂质、写正片 strm、加密抽帧、显式 promote。默认不自动搬家。\n"
            "115 离线已经完成后，只用 process（命令在前，番号在后；不交磁、不刮削、不 promote）：\n"
            f"  {PROCESS_EXAMPLE}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "番号不是子命令。av_auto ABP-073 clean 会直接报错并提示正确写法。\n"
            f"  {PROCESS_EXAMPLE}"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="在 115 根下确保 /av-auto 与 /av-auto/_staging，并创建本地独立 strm 根")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("status", help="查看两侧路径和夹数")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("submit", help="用 p115client 把磁链/ed2k 交到 /av-auto/_staging/<番号>/")
    s.add_argument("--code", required=True)
    s.add_argument("--url", required=True)
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("wait", help="等待该番号 inbox 离线任务完成")
    s.add_argument("--code", required=True)
    s.add_argument("--timeout", type=int, default=0, help="秒，0 表示一直等")
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("clean", help="inbox 只把正片搬进 /av-auto/<番号>/，再清空残留；不碰正式 /AV")
    s.add_argument("--code", required=True)
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("strm", help="按 115 /av-auto/<番号> 里的正片写 staging strm（广告/杂质不写）")
    s.add_argument("codes", nargs="+")
    s.set_defaults(func=cmd_strm)

    s = sub.add_parser("frames", help="对正片 strm 加密抽帧：2分钟起每约8分钟一帧，再补约85%%，最多8帧有效帧；默认预检无效则重抽")
    s.add_argument("code", nargs="?", default="")
    s.add_argument("--strm", default="", help="直接指定 strm 路径")
    s.add_argument("--out", default="", help="帧输出目录，默认持久目录 /config/scripts/av-auto-fs/frames/<番号>")
    s.add_argument("--cleanup", action="store_true", help="抽完删除临时帧（保留时间点清单）")
    s.add_argument("--no-precheck", action="store_true", help="跳过有效性预检（官方库只读对时点时用；验证条不要加这个）")
    s.add_argument("--no-reuse", action="store_true", help="不复用已有 jpg，全部重抽")
    s.set_defaults(func=cmd_frames)

    s = sub.add_parser("review", help="仅对有效帧做 DeepSeek-flash + gpt-6-luna 终审；无效帧不送")
    s.add_argument("code")
    s.add_argument("--out", default="", help="帧目录，默认持久目录 /config/scripts/av-auto-fs/frames/<番号>；容器 /tmp 里已有的帧会先复制过去")
    s.add_argument("--cleanup", action="store_true", help="审完删除临时帧（保留清单和 review.json）")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser(
        "grok-review",
        help="双模型 uncertain 或分歧时，用 cursor/grok-4.7 做 Magpie 复审；写 grok_review.json，不改 review.json",
    )
    s.add_argument("code", nargs="?", default="")
    s.add_argument("--frames-dir", default="", help="已有 review.json 与有效帧的目录")
    s.add_argument("--out", default="", help="同 --frames-dir")
    s.set_defaults(func=cmd_grok_review)

    s = sub.add_parser("intake", help="submit→wait→clean→strm；可选抽帧。不刮削、不 promote")
    s.add_argument("--code", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--timeout", type=int, default=0)
    s.add_argument("--frames", action="store_true")
    s.add_argument("--review", action="store_true", help="抽帧预检后再做双模型终审；仍不 promote")
    s.add_argument("--cleanup", action="store_true")
    s.add_argument("--no-precheck", action="store_true")
    s.add_argument("--out", default="")
    s.set_defaults(func=cmd_intake)

    s = sub.add_parser("promote", help="整夹搬到正式 /AV/<番号>，写正式 strm，刷新 Emby，清 staging strm。默认不自动调用")
    s.add_argument("codes", nargs="+")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_promote)

    s = sub.add_parser(
        "process",
        help="115 已离线后：clean→strm→frames→双模型终审→仅未决时 grok-review。不交磁、不刮削、不 promote",
        description=(
            "处理一个或多个已经完成 115 离线的暂存番号。\n"
            "顺序固定：clean → strm → frames → review → grok-review（仅 uncertain 或两家分歧）。\n"
            "完成前把帧、review.json（以及若有的 grok_review.json）写入\n"
            "/config/scripts/av-auto-fs/frames/<番号>/。只留在容器 /tmp 不算完成。\n"
            "已完成的步骤跳过。不重新交磁，不刮削，不搬进 /AV。\n"
            "\n"
            "用法（命令在前，番号在后）：\n"
            "  av_auto process ABP-073\n"
            f"  {PROCESS_EXAMPLE}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s.add_argument("codes", nargs="+", metavar="番号", help="一个或多个番号，必须写在 process 后面")
    s.set_defaults(func=cmd_process)
    return p


def main(argv: list[str] | None = None) -> int:
    incoming = list(sys.argv[1:] if argv is None else argv)
    reject_code_as_command(incoming)
    args = build_parser().parse_args(incoming)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
