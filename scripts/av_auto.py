#!/usr/bin/env python3
"""115 av-auto staging ↔ official /AV promote, plus strm/frame helpers.

Run inside the moviepilot container (p115client + plugin + /media mounts).
Never print cookies/tokens. Do not change media_llm_route.

Flow for intake: p115client submit into /av-auto/_staging/<code>/ → wait offline
→ junk-clean (keep feature videos only, move into /av-auto/<code>/, delete leftovers)
→ write strm only for keepers → dense-frame from those strm.
Promote is explicit and never automatic.
"""
from __future__ import annotations

import argparse
import base64
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
MAGPIE_DEFAULT = "http://192.168.1.9:3430"
PRECHECK_MODEL = "deepseek/deepseek-flash"
REVIEW_MODELS = ("deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna")
FORBIDDEN_MODEL_BITS = ("minimax", "grok")
PRECHECK_TO = int(os.environ.get("AV_AUTO_PRECHECK_TIMEOUT", "90"))
REVIEW_TO = int(os.environ.get("AV_AUTO_REVIEW_TIMEOUT", "180"))
TZ = ZoneInfo("Asia/Shanghai")

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
    "你是影视库筛选助手。用户偏好：身材要好、不要干扁；不要显老，要年轻、皮肤白、水嫩；"
    "预览不要狰狞表情（「色不是难受」）。色情题材、裸露、性行为本身不判负。"
    "只根据所附抽帧里女优的可见外观判断，不要因为片名、类型标签（如人妻、熟女）本身判负。"
    "判定维度仅限：dry_thin（身材干扁）、looks_old（显老）、poor_skin（皮肤差/暗沉、明显不白不水嫩）、"
    "grimace（狰狞或难受表情，含看起来痛苦的夸张白目/失神）。"
    "スレンダー不要单独判成干扁；人妻/熟女/老片/蜜色不单独判负。"
    "只输出一个 JSON 对象，不要 Markdown，不要其它文字。字段："
    '{"verdict":"pass|fail|uncertain","hits":[],"reason":"一句短理由","confidence":"high|mid|low"}。'
    "verdict=fail：至少一项维度在图中较明确；pass：未见明显不符；uncertain：看不清、角度限制或证据冲突。"
    "只使用有效抽帧；不要根据无效空景或纯字幕想象内容。"
)


DROP_ROOT = Path("/config/scripts/av-auto-fs/pending")


def _is_media_path(path: Path) -> bool:
    s = str(path)
    return s.startswith("/media/") or s.startswith("/mnt/user/media/")


def _queue_fs(kind: str, path: Path, text: str | None = None) -> None:
    DROP_ROOT.mkdir(parents=True, exist_ok=True)
    job = DROP_ROOT / f"{time.time_ns()}"
    job.mkdir()
    (job / "kind").write_text(kind, encoding="utf-8")
    (job / "path").write_text(str(path), encoding="utf-8")
    if text is not None:
        (job / "data").write_text(text, encoding="utf-8")
    print(f"NOTE: /mnt/user/media 暂不可写（ENOSPC，shfs 未吃到新 shareFloor），已排队经 cache 落盘: {path}")


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


def _magpie_chat(cfg: dict, model: str, images: list[Path], system: str, user_text: str, timeout: int, max_tokens: int) -> dict:
    model = _assert_allowed_model(model)
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


def _extract_one(media: str, dest: Path, ss: int) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    row = {"ss": ss, "ok": False, "degraded": False, "bytes": 0, "error": ""}
    try:
        fr = _run_ff(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(ss),
                "-i",
                media,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-vf",
                "scale=w='min(800,iw)':h=-2",
                str(dest),
            ],
            FRAME_TO,
        )
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
        strm = _find_strm_for_code(code, paths)
        if strm is None:
            _die(f"找不到 {code} 的 strm（staging {paths['staging_strm']}/{code} 或正式库）。空目录请不要为测试拷片。")
    else:
        _die("用法: av_auto frames 番号  或  av_auto frames --strm /path/file.strm")
    if not strm.is_file():
        _die(f"strm 不存在: {strm}")
    code = code or strm.stem
    out = Path(args.out) if args.out else Path(f"/tmp/av-auto-frames/{code}")
    out.mkdir(parents=True, exist_ok=True)
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
    # 手动打开 CDN 链会 403（缺跳转 cookie）；把 Av115 redirect 交给 ffmpeg 自己跟 1 跳。
    media = src
    probe = out / "probe.json"
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
                ext = _extract_one(media, dest, t)
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
        "started_at": started.isoformat(),
        "finished_at": datetime.now(TZ).isoformat(),
    }
    (out / "frames_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    print(
        f"frames 抽出 {ok}/{len(pts)} 有效 {valid_n}/{len(pts)} → {out}"
        + (f"  丢弃 {len(discarded)}" if discarded else "")
        + (f"  预检 DeepSeek ¥{cost['deepseek_cny']:.4f}" if precheck else "")
    )
    keep = Path(f"/tmp/av-auto-frames/{code}_manifest.json")
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    if args.cleanup:
        for child in out.iterdir():
            if child.name != "frames_manifest.json":
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
        print("已清理临时帧（保留 frames_manifest.json）")
    return 0 if (valid_n if precheck else ok) else 1


def _load_manifest(out: Path, code: str) -> dict:
    path = out / "frames_manifest.json"
    if not path.is_file():
        _die(f"找不到 {path}，请先跑 av_auto frames")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_review(args) -> int:
    """Dual-model final review on valid frames only. Never sends invalid shots."""
    paths = _paths()
    code = _norm_code(args.code)
    out = Path(args.out) if args.out else Path(f"/tmp/av-auto-frames/{code}")
    if not args.out:
        # prefer the same dir used by frames in this environment
        alt = Path(f"/config/scripts/av-auto-fs/frames/{code}")
        if not (out / "frames_manifest.json").is_file() and (alt / "frames_manifest.json").is_file():
            out = alt
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
    print(f"review {code}: {agreement}/{consensus}  DeepSeek ¥{cost['deepseek_cny']:.4f}  luna 约 ${cost['luna_usd_est']:.4f} → {dest}")
    if args.cleanup:
        for child in out.iterdir():
            if child.name in {"frames_manifest.json", "review.json"}:
                continue
            if child.is_file() and child.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
                child.unlink()
        print("已清理临时帧（保留 frames_manifest.json 与 review.json）")
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
    junk_files = [r for r in items if not r["is_dir"] and r not in keepers]
    junk_dirs = [r for r in items if r["is_dir"]]
    if not keepers:
        print(f"FAIL {code}: inbox 里没有达标正片（>{paths['min_size_mb']}MB 且非预告/广告），不搬、不删 inbox，便于排查")
        print(f"  files={len([r for r in items if not r['is_dir']])} dirs={len(junk_dirs)}")
        return 1
    moved = 0
    for rec in keepers:
        if rec["id"] and rec["id"] != dest_id:
            pan.move(rec["id"], dest_id)
            moved += 1
            print(f"KEEP {code}: {rec['name']} size={rec['size']} → {dest}")
    leftover = []
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
    # leave empty inbox/<code> folder (or try delete the code dir itself)
    if deleted:
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
    print(f"INTAKE { _norm_code(args.code) }: 完成（未 promote，未刮削）")
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="av_auto",
        description="115 av-auto 通路：p115client 交到暂存、清杂质、写正片 strm、加密抽帧、显式 promote。默认不自动搬家。",
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

    s = sub.add_parser("frames", help="对正片 strm 加密抽帧：2分钟起每约8分钟一帧，再补约85%，最多8帧有效帧；默认预检无效则重抽")
    s.add_argument("code", nargs="?", default="")
    s.add_argument("--strm", default="", help="直接指定 strm 路径")
    s.add_argument("--out", default="", help="帧输出目录，默认 /tmp/av-auto-frames/<番号>")
    s.add_argument("--cleanup", action="store_true", help="抽完删除临时帧（保留时间点清单）")
    s.add_argument("--no-precheck", action="store_true", help="跳过有效性预检（官方库只读对时点时用；验证条不要加这个）")
    s.add_argument("--no-reuse", action="store_true", help="不复用已有 jpg，全部重抽")
    s.set_defaults(func=cmd_frames)

    s = sub.add_parser("review", help="仅对有效帧做 DeepSeek-flash + gpt-6-luna 终审；无效帧不送")
    s.add_argument("code")
    s.add_argument("--out", default="", help="帧目录，默认已有的 av-auto-fs/frames/<番号> 或 /tmp/av-auto-frames/<番号>")
    s.add_argument("--cleanup", action="store_true", help="审完删除临时帧（保留清单和 review.json）")
    s.set_defaults(func=cmd_review)

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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
