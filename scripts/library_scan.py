"""Full-library dense-frame scan for findav.

Official entry: ``av_auto library-scan``. One process can be left running.
It resumes from progress.json, submits ed2k/magnet into /av-auto/_staging,
and reviews with the verified dense-frame path.

It does not scrape, does not promote, does not refresh Emby, and does not
move anything into /AV. Links are never written to progress or stdout.
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def print(*args, **kwargs):  # noqa: A001
    """Line-flush every operator line. nohup logs stay block-buffered otherwise."""
    kwargs.setdefault("flush", True)
    builtins.print(*args, **kwargs)

TZ = ZoneInfo("Asia/Shanghai")
HARD_SKIP = frozenset({"HPET-022", "MFYD-175"})
SUBMIT_GAP_SEC = 48
BACKOFF_406_SEC = 180
MIN_OFFLINE_SURPLUS = 5
FULL_DISK_BYTES = 1024 ** 3
MIN_115_REMAIN_BYTES = 20 * FULL_DISK_BYTES
FAIL_RETRY_SEC = 1800
STUCK_AFTER_SEC = 2 * 3600
POLL_SEC = 20
QUOTA_POLL_SEC = 600
STUCK_POLL_SEC = 1800
STATE_DIR_DEFAULT = Path("/config/scripts/av-auto-fs/library-scan")

# Live findav has no URI column. av_ed2k is file_hash/file_name/size_bytes.
# av_magnet is infohash. source_url is a page reference, not a download.
ED2K_FIELDS = ("code", "file_name", "size_bytes", "file_hash")
MAGNET_FIELDS = ("code", "infohash")

_LINK_RE = re.compile(r"(?i)(?:ed2k://\S+|magnet:\S+)")
_DROP_KEYS = {
    "uri",
    "url",
    "ed2k",
    "magnet",
    "cookie",
    "password",
    "secret",
    "api_key",
    "finddav_password",
    "mysql_pwd",
}
_BLOCK_MARKERS = ("验证码", "风控", "账号异常")
_SIGNAL_SKIP = _DROP_KEYS | {"urls", "link", "links"}

LIBRARY_SCAN_EXAMPLES = """\
用法（命令在前，番号在后；不写番号就是整个 findav 库）：
  av_auto library-scan
  av_auto library-scan --dry-run
  av_auto library-scan --status
  av_auto library-scan ABP-073

不刮削，不 promote，不刷新 Emby，不把片子搬进 /AV。
只走成片抽帧：需要清杂质或写 strm 时用 process；已有本片正片 strm 时用
frames --strm <容器路径> <番号>，再 review，仅 uncertain 或两家分歧才 grok-review。
不送封面、剧照、Thumb、Backdrop，也不调用 av_review review。
"""


def redact(text: str) -> str:
    """Replace ed2k and magnet values. The replacement is not itself a URI."""
    return _LINK_RE.sub("[link]", text or "")


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f}MiB"
    return f"{n}B"


def _norm(code: str) -> str:
    return str(code or "").strip().upper()


def _now_default() -> datetime:
    return datetime.now(TZ)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed


def _as_int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?I?B)?\s*", value, re.I)
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or "").upper()
        scale = {
            "": 1,
            "B": 1,
            "KB": 1000,
            "MB": 1000 ** 2,
            "GB": 1000 ** 3,
            "TB": 1000 ** 4,
            "KIB": 1024,
            "MIB": 1024 ** 2,
            "GIB": 1024 ** 3,
            "TIB": 1024 ** 4,
        }.get(unit)
        if scale is None:
            return None
        return int(number * scale)
    if isinstance(value, dict):
        for key in ("size", "bytes", "remain", "value"):
            if key in value:
                got = _as_int(value[key])
                if got is not None:
                    return got
    return None


def ed2k_uri(name: str, size: int, file_hash: str) -> str:
    """Build one ed2k link. Hash is lowercased. aich and source_url are not used."""
    safe = (name or f"{file_hash}.mp4").replace("|", "_").replace("\n", "").replace("\r", "")
    return f"ed2k://|file|{safe}|{int(size)}|{file_hash.lower()}|/"


def magnet_uri(infohash: str) -> str:
    """Build one magnet from the infohash only."""
    digest = (infohash or "").strip().lower()
    return f"magnet:?xt=urn:btih:{digest}"


def _positive_size(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if not value.is_integer() or value <= 0:
            return None
        return int(value)
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def ed2k_uris_from_rows(rows: list[dict]) -> list[tuple[str, str]]:
    """(code, uri) largest size_bytes first.

    A row needs code, file_hash, and size_bytes > 0. source_url is ignored.
    """
    usable: list[tuple[int, str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        file_hash = str(row.get("file_hash") or "").strip()
        size = _positive_size(row.get("size_bytes"))
        if not code or not file_hash or size is None:
            continue
        raw_name = row.get("file_name")
        name = "" if raw_name is None else str(raw_name)
        usable.append((size, code, ed2k_uri(name, size, file_hash)))
    usable.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [(code, uri) for _size, code, uri in usable]


def magnet_uris_from_rows(rows: list[dict]) -> list[tuple[str, str]]:
    """(code, uri) in code then infohash order. Empty infohash is skipped."""
    found: list[tuple[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        infohash = str(row.get("infohash") or "").strip()
        if not code or not infohash:
            continue
        found.append((code, magnet_uri(infohash)))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def pick_link(ed2ks: list[str] | None, magnets: list[str] | None) -> dict | None:
    """Prefer an ed2k URI, otherwise a magnet. Do not log the return value."""
    for raw in ed2ks or []:
        uri = str(raw or "").strip()
        if uri.lower().startswith("ed2k://"):
            return {"kind": "ed2k", "uri": uri}
    for raw in magnets or []:
        uri = str(raw or "").strip()
        if uri.lower().startswith("magnet:"):
            return {"kind": "magnet", "uri": uri}
    return None


def parse_corpus_tsv(text: str) -> dict:
    """Parse the export TSV. Callers must not print ``text``."""
    titles: list[dict] = []
    links: dict[str, dict[str, list[str]]] = {}
    seen: set[str] = set()
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        kind = parts[0].strip().upper()
        if kind == "T" and len(parts) >= 2:
            code = _norm(parts[1])
            if not code or code in seen:
                continue
            seen.add(code)
            verdict = (parts[2] if len(parts) > 2 else "") or "pending"
            erotic = parts[3] if len(parts) > 3 else ""
            titles.append({
                "code": code,
                "user_verdict": verdict.strip() or "pending",
                "erotic_level": erotic.strip() or None,
            })
            continue
        if kind in ("E", "M") and len(parts) >= 3:
            code = _norm(parts[1])
            uri = parts[2].strip()
            if not code or not uri:
                continue
            bucket = links.setdefault(code, {"ed2k": [], "magnet": []})
            key = "ed2k" if kind == "E" else "magnet"
            bucket[key].append(uri)
    return {"titles": titles, "links": links}


def load_corpus_file(path: Path, *, unlink: bool = True) -> dict:
    """Read a mode-600 corpus and drop the file so URIs are not left on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    finally:
        if unlink:
            try:
                path.unlink()
            except OSError:
                pass
    return parse_corpus_tsv(text)


def empty_progress() -> dict:
    return {
        "version": 1,
        "updated_at": "",
        "current": "",
        "queue": [],
        "done": {},
        "failed": {},
        "skipped_no_link": [],
        "skipped_user": [],
        "hard_skip": [],
        "already_reviewed": [],
        "stuck": {},
        "in_progress": {},
        "submitted": {},
        "costs": {"deepseek_cny": 0.0, "luna_usd_est": 0.0},
        "blocked": None,
        "submit_hold": None,
        "counts": {},
        "next_submit_at": "",
        "phase": "",
        "inventory_index": 0,
        "inventory_total": 0,
    }


def scrub(obj):
    """Drop secret keys and URI strings before anything is written or printed."""
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            if str(key).lower() in _DROP_KEYS:
                continue
            cleaned[str(key)] = scrub(value)
        return cleaned
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def save_progress(path: Path, doc: dict) -> None:
    clean = scrub(doc)
    text = json.dumps(clean, ensure_ascii=False, indent=2) + "\n"
    if _LINK_RE.search(text):
        raise RuntimeError("progress 含有链接，已拒绝写入")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def load_progress(path: Path) -> dict:
    if not path.is_file():
        return empty_progress()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_progress()
    if not isinstance(raw, dict):
        return empty_progress()
    base = empty_progress()
    for key in base:
        if key in raw:
            base[key] = raw[key]
    if not isinstance(base["done"], dict):
        base["done"] = {}
    if not isinstance(base["failed"], dict):
        base["failed"] = {}
    if not isinstance(base["stuck"], dict):
        base["stuck"] = {}
    if not isinstance(base["in_progress"], dict):
        base["in_progress"] = {}
    if not isinstance(base["submitted"], dict):
        base["submitted"] = {}
    if not isinstance(base["costs"], dict):
        base["costs"] = {"deepseek_cny": 0.0, "luna_usd_est": 0.0}
    if not isinstance(base["counts"], dict):
        base["counts"] = {}
    return base


def dense_bucket(
    *,
    frames_ready: bool,
    review_complete: bool,
    needs_tiebreak: bool,
    tiebreak_done: bool,
    has_manifest: bool,
    has_review_file: bool,
) -> str:
    """complete / incomplete / missing. A manifest with zero frames is not done."""
    if frames_ready and review_complete and (not needs_tiebreak or tiebreak_done):
        return "complete"
    if has_manifest or has_review_file or frames_ready:
        return "incomplete"
    return "missing"


def disposition(record: dict) -> str:
    """hard_skip and user_verdict=skip win over a link. erotic_level is ignored."""
    if record["code"] in HARD_SKIP:
        return "hard_skip"
    if str(record.get("user_verdict") or "").strip().lower() == "skip":
        return "user_skip"
    if record.get("review") == "complete":
        return "already_reviewed"
    if not record.get("has_link"):
        return "no_link"
    return "work"


def counts_of(records: list[dict]) -> dict:
    remaining = [row for row in records if disposition(row) == "work"]
    return {
        "total": len(records),
        "with_link": sum(1 for row in records if row.get("has_link")),
        "no_link": sum(1 for row in records if not row.get("has_link")),
        "already_reviewed": sum(1 for row in records if row.get("review") == "complete"),
        "user_verdict_skip": sum(1 for row in records if str(row.get("user_verdict") or "").lower() == "skip"),
        "skip": sum(1 for row in records if str(row.get("user_verdict") or "").lower() == "skip"),
        "hard_skip": sum(1 for row in records if row["code"] in HARD_SKIP),
        "incomplete": sum(1 for row in remaining if row.get("review") == "incomplete"),
        "remaining": len(remaining),
    }


def format_status(progress: dict) -> str:
    """One line a running scan and ``--status`` both use. No links or secrets."""
    counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
    remaining = counts.get("remaining")
    if remaining is None:
        remaining = len(progress.get("queue") or [])
    try:
        index = int(progress.get("inventory_index") or 0)
    except (TypeError, ValueError):
        index = 0
    try:
        total = int(progress.get("inventory_total") or 0)
    except (TypeError, ValueError):
        total = 0
    return (
        "status:"
        f" phase={progress.get('phase') or '-'}"
        f" current={progress.get('current') or '-'}"
        f" done={len(progress.get('done') or {})}"
        f" remaining={int(remaining or 0)}"
        f" updated_at={progress.get('updated_at') or '-'}"
        f" inventory={index}/{total}"
        f" failed={len(progress.get('failed') or {})}"
        f" stuck={len(progress.get('stuck') or {})}"
        f" in_progress={len(progress.get('in_progress') or {})}"
        f" queue={len(progress.get('queue') or [])}"
    )


def format_counts(counts: dict) -> str:
    lines = ["library-scan 语料（不交磁、不刮削、不 promote）"]
    for key in (
        "total",
        "with_link",
        "already_reviewed",
        "skip",
        "hard_skip",
        "no_link",
        "incomplete",
        "remaining",
    ):
        lines.append(f"{key}: {int(counts.get(key) or 0)}")
    return "\n".join(lines)


def build_records(corpus: dict, status_of, only_codes: list[str] | None = None) -> list[dict]:
    wanted = {_norm(code) for code in (only_codes or []) if _norm(code)}
    titles = list(corpus.get("titles") or [])
    known = {_norm(row.get("code")) for row in titles}
    if wanted:
        for code in sorted(wanted - known):
            titles.append({"code": code, "user_verdict": "pending", "erotic_level": None, "missing": True})
    records = []
    seen: set[str] = set()
    links = corpus.get("links") or {}
    for title in titles:
        code = _norm(title.get("code"))
        if not code or code in seen:
            continue
        if wanted and code not in wanted:
            continue
        seen.add(code)
        bucket = links.get(code) or {}
        ed2ks = [str(item) for item in (bucket.get("ed2k") or [])]
        magnets = [str(item) for item in (bucket.get("magnet") or [])]
        has_link = pick_link(ed2ks, magnets) is not None
        records.append({
            "code": code,
            "user_verdict": str(title.get("user_verdict") or "pending").strip() or "pending",
            "erotic_level": title.get("erotic_level"),
            "has_link": has_link,
            "review": status_of(code),
            "missing": bool(title.get("missing")),
            "ed2ks": ed2ks,
            "magnets": magnets,
        })
    records.sort(key=lambda row: row["code"])
    return records


def classify_submit_text(text: str) -> str:
    """backoff on HTTP 406, block on captcha / 911 / 风控 / 账号异常, else ''."""
    cleaned = redact(text or "")
    if any(marker in cleaned for marker in _BLOCK_MARKERS):
        return "block"
    if re.search(r"(?<![\w\]])911(?![\w])", cleaned):
        return "block"
    if re.search(r"(?<![\w\]])406(?![\w])", cleaned):
        return "backoff"
    return ""


def signal_from_result(result, depth: int = 0) -> str:
    """Readable error fields only. URI-bearing keys are skipped before matching."""
    if depth > 5:
        return ""
    if isinstance(result, dict):
        parts = []
        for key, value in result.items():
            if str(key).lower() in _SIGNAL_SKIP:
                continue
            parts.append(signal_from_result(value, depth + 1))
        return " ".join(part for part in parts if part)
    if isinstance(result, list):
        return " ".join(signal_from_result(item, depth + 1) for item in result[:20])
    if isinstance(result, (str, int, float)) and not isinstance(result, bool):
        return str(result)
    return ""


def classify_result(result) -> str:
    kind = classify_submit_text(signal_from_result(result))
    if kind:
        return kind
    if not isinstance(result, dict):
        return "fail"
    state = result.get("state", result.get("ok"))
    errno = result.get("errno", result.get("error_code", result.get("errcode")))
    if state is False or (errno not in (None, 0, "0", "") and state not in (True, 1, "1", None)):
        return "fail"
    if state is False:
        return "fail"
    return "ok"


def parse_quota(payload) -> dict:
    """Offline-task surplus. Unknown stays None so a missing field is not 'quota full'."""
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return {"surplus": None, "total": None, "used": None}
    surplus = _as_int(data.get("surplus", data.get("quota_surplus")))
    total = _as_int(data.get("total", data.get("quota_total")))
    used = _as_int(data.get("used", data.get("used_quota")))
    if surplus is None and total is not None and used is not None:
        surplus = total - used
    return {"surplus": surplus, "total": total, "used": used}


def parse_cloud_remain_bytes(payload) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("all_remain", "remain_size", "size_remain", "rt_remain"):
        if key in payload:
            got = _as_int(payload[key])
            if got is not None:
                return got
    for key in ("data", "rt_space_info", "space", "space_info"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            got = parse_cloud_remain_bytes(inner)
            if got is not None:
                return got
    return None


def assess_space(sample: dict | None) -> dict:
    """Separate 115 quota, 115 cloud remain, frame disk, and the media/array mount.

    ``磁盘满`` is used only when a measured filesystem is under 1GiB.
    A large cache, a large array, or a low offline-task surplus is not a full disk.
    Unknown readings do not block and are not called full.
    """
    sample = sample or {}
    cloud = sample.get("cloud_remain")
    surplus = sample.get("offline_surplus")
    frames = sample.get("frames_free")
    media = sample.get("media_free")
    cache = sample.get("cache_free")
    disk_full = False
    where = ""
    process_ok = True
    submit_blocked = ""
    notes: list[str] = []

    def short(value) -> bool:
        return value is not None and value < FULL_DISK_BYTES

    if frames is not None and short(frames):
        disk_full = True
        where = "frames"
        process_ok = False
        submit_blocked = "frames-disk"
        notes.append(f"帧目录可用 {_fmt_bytes(frames)}，低于 1GiB，这是磁盘满。抽帧和新交磁都暂停。")
    elif frames is None and short(cache):
        disk_full = True
        where = "cache"
        process_ok = False
        submit_blocked = "frames-disk"
        notes.append(f"cache 可用 {_fmt_bytes(cache)}，低于 1GiB，这是磁盘满。抽帧和新交磁都暂停。")
    else:
        if frames is not None:
            notes.append(f"帧目录可用 {_fmt_bytes(frames)}")
        if cache is not None:
            notes.append(f"cache 可用 {_fmt_bytes(cache)}")

    if short(media):
        disk_full = True
        where = where or "media"
        notes.append(
            f"/media 可用 {_fmt_bytes(media)}，低于 1GiB，这是磁盘满。"
            "115 下载不落在这块盘上，交磁不因此停止。"
        )
    elif media is not None:
        notes.append(f"/media 可用 {_fmt_bytes(media)}")

    if surplus is not None and surplus < MIN_OFFLINE_SURPLUS:
        submit_blocked = submit_blocked or "quota"
        notes.append(
            f"115 离线剩余配额 {surplus}，低于 {MIN_OFFLINE_SURPLUS}。"
            "停止新交磁。已有文件的番号继续处理。这不是磁盘满。"
        )
    elif surplus is not None:
        notes.append(f"115 离线剩余配额 {surplus}")

    if cloud is not None and cloud < MIN_115_REMAIN_BYTES:
        if cloud < FULL_DISK_BYTES:
            disk_full = True
            where = where or "115"
            submit_blocked = submit_blocked or "115-space"
            notes.append(f"115 云盘剩余 {_fmt_bytes(cloud)}，低于 1GiB，这是 115 云盘空间不足。停止新交磁。")
        else:
            submit_blocked = submit_blocked or "115-space"
            notes.append(
                f"115 云盘剩余 {_fmt_bytes(cloud)}，低于 {_fmt_bytes(MIN_115_REMAIN_BYTES)}。"
                "停止新交磁。这不是本地磁盘满。"
            )
    elif cloud is not None:
        notes.append(f"115 云盘剩余 {_fmt_bytes(cloud)}")

    if not notes:
        notes.append("没有读到空间数字，不把未知说成磁盘满。")
    return {
        "disk_full": disk_full,
        "disk_full_where": where,
        "submit_blocked": submit_blocked,
        "process_ok": process_ok,
        "message": "；".join(notes),
    }


def task_percent(task: dict) -> float | None:
    for key in ("percentDone", "percent", "progress", "process"):
        if key not in task or task.get(key) in (None, ""):
            continue
        try:
            return float(task.get(key))
        except (TypeError, ValueError):
            continue
    return None


def task_rate(task: dict) -> float | None:
    for key in ("rate", "speed"):
        if key not in task or task.get(key) in (None, ""):
            continue
        raw = task.get(key)
        if isinstance(raw, str):
            match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
            if not match:
                continue
            try:
                return float(match.group(0))
            except ValueError:
                continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def observe_task(previous: dict | None, task: dict, now: datetime, stuck_after_sec: int) -> dict:
    """A running task with no movement is stuck only after stuck_after_sec.

    The first look is in-progress. Either way the caller moves on to later titles.
    """
    percent = task_percent(task)
    rate = task_rate(task)
    name = redact(str(task.get("name") or task.get("file_name") or ""))[:180]
    bucket = str(task.get("bucket") or "")
    unchanged_since = now
    if previous and percent is not None and previous.get("percent") is not None:
        try:
            same = abs(float(percent) - float(previous["percent"])) < 0.05
        except (TypeError, ValueError):
            same = False
        stalled = rate == 0 or (rate is None and float(percent) < 1.0)
        if same and stalled:
            unchanged_since = _parse_time(str(previous.get("unchanged_since") or "")) or now
    stalled_now = bucket == "run" and (
        rate == 0 or (rate is None and percent is not None and percent < 1.0)
    )
    elapsed = (now - unchanged_since).total_seconds()
    stuck = bool(stalled_now and elapsed >= stuck_after_sec)
    return {
        "bucket": bucket,
        "percent": percent,
        "rate": rate,
        "name": name,
        "seen_at": now.isoformat(),
        "unchanged_since": unchanged_since.isoformat(),
        "stuck": stuck,
    }


def invoke_uri_file(code: str, uri: str, invoke, directory: Path | None = None):
    """Hand a link through a mode-600 file, then delete it before ``invoke``.

    This is the in-process form of ``URI=$(cat file)`` and ``--url "$URI"``.
    The file is gone before the submit call. ``uri`` must not be logged here.
    """
    root = directory or Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="av-auto-url.", dir=str(root))
    os.close(fd)
    path = Path(name)
    os.chmod(path, 0o600)
    try:
        path.write_bytes(uri.encode("utf-8"))
        os.chmod(path, 0o600)
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise OSError("链接临时文件不是 mode 600")
        loaded = path.read_text(encoding="utf-8")
        path.unlink()
        path = None
        return mode, invoke(code, loaded)
    finally:
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def read_blocked(state_dir: Path) -> str | None:
    path = state_dir / "BLOCKED"
    try:
        if not path.is_file():
            return None
        text = redact(path.read_text(encoding="utf-8", errors="replace")).strip()
    except OSError:
        return None
    return text or "blocked"


def write_blocked(state_dir: Path, reason: str, now: datetime) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    reason = redact(reason).strip() or "风控"
    body = (
        f"reason: {reason[:80]}\n"
        f"at: {now.isoformat()}\n"
        "action: 已停止新的交磁。已有正片或 strm 的番号继续抽帧审片。"
        "不要继续重试交磁，直到这个 BLOCKED 文件被拿掉。\n"
    )
    path = state_dir / "BLOCKED"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o644)


def _remember_lists(progress: dict, records: list[dict]) -> None:
    progress["skipped_no_link"] = [row["code"] for row in records if disposition(row) == "no_link"]
    progress["skipped_user"] = [row["code"] for row in records if disposition(row) == "user_skip"]
    progress["hard_skip"] = [row["code"] for row in records if disposition(row) == "hard_skip"]
    progress["already_reviewed"] = [row["code"] for row in records if disposition(row) == "already_reviewed"]
    progress["queue"] = [row["code"] for row in records if disposition(row) == "work"]
    progress["counts"] = counts_of(records)


def _add_cost(progress: dict, cost: dict | None) -> None:
    if not isinstance(cost, dict):
        return
    costs = progress.setdefault("costs", {"deepseek_cny": 0.0, "luna_usd_est": 0.0})
    for key in ("deepseek_cny", "luna_usd_est"):
        try:
            costs[key] = float(costs.get(key) or 0) + float(cost.get(key) or 0)
        except (TypeError, ValueError):
            continue


def _mark_done(progress: dict, code: str, how: str, now: datetime) -> None:
    progress["done"][code] = {"at": now.isoformat(), "via": how}
    progress["failed"].pop(code, None)
    progress["stuck"].pop(code, None)
    progress["in_progress"].pop(code, None)


def _mark_failed(progress: dict, code: str, reason: str, now: datetime, retry_sec: int) -> None:
    progress["failed"][code] = {
        "at": now.isoformat(),
        "until": (now + timedelta(seconds=retry_sec)).isoformat(),
        "reason": redact(reason)[:300],
    }
    progress["in_progress"].pop(code, None)


def _retry_later(progress: dict, code: str, now: datetime) -> bool:
    row = progress.get("failed", {}).get(code)
    if not isinstance(row, dict):
        return False
    until = _parse_time(str(row.get("until") or ""))
    return until is not None and until > now


def _submit_allowed(progress: dict, now: datetime, space: dict, blocked: str | None) -> tuple[bool, str]:
    if blocked:
        return False, "blocked"
    if space.get("submit_blocked"):
        progress["submit_hold"] = {
            "reason": space["submit_blocked"],
            "at": now.isoformat(),
            "disk_full": bool(space.get("disk_full")),
            "where": space.get("disk_full_where") or "",
        }
        return False, str(space["submit_blocked"])
    progress["submit_hold"] = None
    nxt = _parse_time(str(progress.get("next_submit_at") or ""))
    if nxt is not None and nxt > now:
        return False, "gap"
    return True, ""


def _run_ready(deps, progress: dict, code: str, files: dict, now: datetime) -> str:
    progress["current"] = code
    action = files.get("action")
    try:
        if action == "process":
            progress["phase"] = "process"
            _save(deps, progress, now)
            print(f"library-scan {code}: process（clean→strm→frames→review→必要时 grok-review；不交磁、不刮削、不 promote）")
            rc = int(deps.process_title(code) or 0)
            how = "process"
        elif action == "frames":
            strm = str(files.get("strm") or "")
            progress["phase"] = "frames"
            _save(deps, progress, now)
            print(f"library-scan {code}: frames --strm 已有正片，再 review；不交磁、不刮削、不 promote")
            rc = int(deps.frames_title(code, strm) or 0)
            how = "frames"
        else:
            _mark_failed(progress, code, "没有可执行的抽帧路径", now, int(deps.fail_retry_sec))
            return "failed"
    except SystemExit as exc:
        status = exc.code
        rc = status if isinstance(status, int) else 1
        how = action or "work"
        _mark_failed(progress, code, f"退出 {rc}", now, int(deps.fail_retry_sec))
        print(f"FAIL {code}: library-scan 继续后面的番号（不刮削、不 promote）")
        return "failed"
    except Exception as exc:
        _mark_failed(progress, code, redact(str(exc))[:300] or "error", now, int(deps.fail_retry_sec))
        print(f"FAIL {code}: {redact(str(exc))[:200]}。继续后面的番号。")
        return "failed"
    if rc == 0 and deps.review_status(code) == "complete":
        _mark_done(progress, code, how, now)
        _add_cost(progress, deps.cost_of(code))
        print(f"library-scan {code}: 完成（未刮削、未 promote）")
        return "done"
    _mark_failed(progress, code, f"{how} 未留下完整成片抽帧 review（rc={rc}）", now, int(deps.fail_retry_sec))
    print(f"FAIL {code}: 未留下完整 review。继续后面的番号。不刮削、不 promote。")
    return "failed"


def _observe_waiting(progress: dict, code: str, task: dict, now: datetime, stuck_after: int) -> str:
    previous = progress.get("stuck", {}).get(code) or progress.get("in_progress", {}).get(code)
    if not isinstance(previous, dict):
        previous = None
    observed = observe_task(previous, task, now, stuck_after)
    if observed["stuck"]:
        progress["stuck"][code] = observed
        progress["in_progress"].pop(code, None)
        print(
            f"STUCK {code}: percent={observed.get('percent')} rate={observed.get('rate')} "
            f"name={observed.get('name') or '-'}。先处理后面的番号，稍后再看这一条。"
        )
        return "stuck"
    progress["in_progress"][code] = observed
    progress["stuck"].pop(code, None)
    print(
        f"IN-PROGRESS {code}: percent={observed.get('percent')} rate={observed.get('rate')}。"
        "不堵住后面的番号。"
    )
    return "running"


def one_pass(deps, progress: dict, records: list[dict], now: datetime) -> str:
    """Process every ready title before any new submit. Stuck titles do not block."""
    _remember_lists(progress, records)
    blocked_text = read_blocked(deps.state_dir)
    if blocked_text:
        progress["blocked"] = {"reason": blocked_text.splitlines()[0][:180], "at": now.isoformat()}
    else:
        progress["blocked"] = None

    try:
        space = assess_space(deps.space())
    except Exception as exc:
        print(f"NOTE: 空间读取失败（{redact(str(exc))[:160]}）。不把这次失败说成磁盘满。")
        space = assess_space({})
    if space["message"]:
        print(f"space: {space['message']}")

    tasks = None
    tasks_known = True
    try:
        tasks = deps.tasks()
    except Exception as exc:
        tasks_known = False
        print(f"NOTE: 读不到离线任务（{redact(str(exc))[:160]}）。本轮不新交磁。")
    if tasks is None:
        tasks_known = False
        tasks = {}

    ready: list[tuple[dict, dict]] = []
    waiting: list[tuple[str, dict]] = []
    to_submit: list[dict] = []
    to_check = [
        record for record in records
        if disposition(record) == "work" and not record.get("missing") and not _retry_later(progress, record["code"], now)
    ]
    progress["phase"] = "inventory"
    progress["inventory_index"] = 0
    progress["inventory_total"] = len(to_check)
    progress["current"] = ""
    _save(deps, progress, now)
    print(f"library-scan: 开始核对 115 上 {len(to_check)} 个番号（不交磁、不刮削、不 promote）")
    seen = 0
    for record in records:
        if disposition(record) != "work":
            continue
        code = record["code"]
        if record.get("missing"):
            print(f"SKIP {code}: 不在 findav 语料里，不交磁")
            continue
        if _retry_later(progress, code, now):
            continue
        seen += 1
        progress["phase"] = "inventory"
        progress["current"] = code
        progress["inventory_index"] = seen
        progress["inventory_total"] = len(to_check)
        _save(deps, progress, now)
        print(f"library-scan: 核对 115 {seen}/{len(to_check)} {code}")
        try:
            files = deps.files_of(code)
        except Exception as exc:
            _mark_failed(progress, code, redact(str(exc))[:200], now, int(deps.fail_retry_sec))
            continue
        action = str((files or {}).get("action") or "none")
        if action == "error":
            _mark_failed(progress, code, redact(str((files or {}).get("reason") or "files"))[:200], now, int(deps.fail_retry_sec))
            continue
        if action in ("process", "frames"):
            ready.append((record, files))
            continue
        task = tasks.get(code) if isinstance(tasks, dict) else None
        bucket = str((task or {}).get("bucket") or "")
        already = code in progress.get("submitted", {}) or code in progress.get("in_progress", {}) or code in progress.get("stuck", {})
        if not tasks_known:
            waiting.append((code, {"bucket": "unknown", "name": code}))
            continue
        if task and bucket == "done":
            ready.append((record, {"action": "process", "strm": ""}))
            continue
        if task and bucket == "fail":
            progress.get("submitted", {}).pop(code, None)
            _mark_failed(progress, code, "115 离线失败", now, int(deps.fail_retry_sec))
            continue
        if task and bucket in ("run", "unknown"):
            waiting.append((code, task))
            continue
        if already:
            # Submitted, or previously seen running, but this poll has no task yet.
            # Do not send a second link. Re-check later.
            waiting.append((code, {"bucket": "run", "name": code}))
            continue
        to_submit.append(record)

    did = False
    if space.get("process_ok"):
        for record, files in ready:
            _run_ready(deps, progress, record["code"], files, now)
            did = True
            _save(deps, progress, now)
    elif ready:
        print("library-scan: 帧盘空间不够，本轮不抽帧。不把还够用的盘说成满。")

    for code, task in waiting:
        progress["phase"] = "wait"
        progress["current"] = code
        _save(deps, progress, now)
        _observe_waiting(progress, code, task, now, int(deps.stuck_after_sec))
        _save(deps, progress, now)

    allowed, why = _submit_allowed(progress, now, space, blocked_text)
    if to_submit and not allowed:
        if why == "blocked":
            print("BLOCKED: 已停止新的交磁。已有文件的番号继续。不继续撞风控。")
        elif why == "gap":
            print("library-scan: 交磁间隔未到，先不提交下一条。")
        elif why == "quota":
            print("library-scan: 115 离线配额不足，停止新交磁。已有文件的番号继续。")
        elif why == "115-space":
            print("library-scan: 115 云盘剩余空间不足，停止新交磁。这不是本地磁盘满。")
        elif why == "frames-disk":
            print("library-scan: 帧目录所在盘低于 1GiB，暂停新交磁。")
        else:
            print(f"library-scan: 本轮不新交磁（{why}）。")
    elif to_submit and allowed:
        record = to_submit[0]
        code = record["code"]
        link = pick_link(record.get("ed2ks"), record.get("magnets"))
        if link is None:
            _mark_failed(progress, code, "没有可用链接", now, int(deps.fail_retry_sec))
        else:
            progress["current"] = code
            progress["phase"] = "submit"
            kind_name = link["kind"]
            _save(deps, progress, now)
            print(f"library-scan {code}: 提交 {kind_name} 到 /av-auto/_staging（不打印链接，不刮削、不 promote）")
            try:
                outcome = deps.submit(code, link["uri"])
            except Exception as exc:
                outcome = {"class": "fail", "detail": redact(str(exc))[:200]}
                print(f"FAIL {code}: 提交失败。不打印链接。")
            outcome = outcome if isinstance(outcome, dict) else {"class": "fail", "detail": ""}
            klass = str(outcome.get("class") or "fail")
            detail = redact(str(outcome.get("detail") or ""))[:200]
            if klass == "ok":
                progress["submitted"][code] = {"at": now.isoformat(), "kind": kind_name}
                progress["in_progress"][code] = {
                    "bucket": "run",
                    "percent": None,
                    "rate": None,
                    "name": "",
                    "seen_at": now.isoformat(),
                    "unchanged_since": now.isoformat(),
                    "stuck": False,
                }
                progress["next_submit_at"] = (now + timedelta(seconds=int(deps.submit_gap_sec))).isoformat()
                print(f"SUBMIT {code}: 已交 {kind_name}。下一条至少等 {int(deps.submit_gap_sec)}s。")
                did = True
            elif klass == "backoff":
                progress["next_submit_at"] = (now + timedelta(seconds=int(deps.backoff_sec))).isoformat()
                progress["submit_hold"] = {"reason": "406", "at": now.isoformat(), "until": progress["next_submit_at"]}
                print(f"BACKOFF {code}: 115 HTTP 406，{int(deps.backoff_sec)}s 内不再交磁。{detail}")
                did = True
            elif klass == "block":
                write_blocked(deps.state_dir, detail or "风控", now)
                progress["blocked"] = {"reason": detail or "风控", "at": now.isoformat()}
                print(f"BLOCKED {code}: 验证码/911/风控/账号异常。停止新交磁。{detail}")
                did = True
            else:
                _mark_failed(progress, code, detail or "提交失败", now, int(deps.fail_retry_sec))
                progress["next_submit_at"] = (now + timedelta(seconds=int(deps.submit_gap_sec))).isoformat()
                print(f"FAIL {code}: 提交未成功。{detail}")
                did = True
            _save(deps, progress, now)

    progress["current"] = ""
    work_left = [row["code"] for row in records if disposition(row) == "work" and not row.get("missing")]
    unfinished = [code for code in work_left if code not in progress["done"]]
    if not unfinished:
        progress["phase"] = "finished"
        print("library-scan: 队列里没有还要处理的番号。")
        return "stop"
    progress["phase"] = "idle"
    if did:
        return "again"
    return "idle"


def _save(deps, progress: dict, now: datetime) -> None:
    progress["updated_at"] = now.isoformat()
    save_progress(deps.state_dir / "progress.json", progress)


def _idle_seconds(progress: dict, now: datetime) -> int:
    """Poll running downloads often. Don't spin, and don't sleep past a 406/gap hold."""
    if progress.get("in_progress"):
        return POLL_SEC
    nxt = _parse_time(str(progress.get("next_submit_at") or ""))
    if nxt is not None and nxt > now:
        return max(1, int((nxt - now).total_seconds()))
    hold = progress.get("submit_hold") if isinstance(progress.get("submit_hold"), dict) else None
    if hold and hold.get("reason") in ("quota", "115-space", "frames-disk"):
        return QUOTA_POLL_SEC
    if progress.get("stuck"):
        return STUCK_POLL_SEC
    return POLL_SEC


def execute(args, deps=None) -> int:
    """Run one report or the resume-safe scan. ``deps`` is injected by tests."""
    deps = deps or default_deps()
    codes = list(getattr(args, "codes", None) or [])
    if codes:
        import av_auto

        for raw in codes:
            if not av_auto.looks_like_catalog_code(str(raw)):
                av_auto._die(
                    f"library-scan 的番号要写在子命令后面，收到 {raw!r}。"
                    "用法: av_auto library-scan [番号...]",
                    2,
                )
    dry = bool(getattr(args, "dry_run", False))
    status = bool(getattr(args, "status", False))
    once = bool(getattr(args, "once", False))
    try:
        corpus = deps.load_corpus()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: 读不到 findav 语料（{redact(str(exc))[:200]}）。不打印连接信息。")
        return 1
    records = build_records(corpus, deps.review_status, codes)
    counts = counts_of(records)
    print(format_counts(counts))
    for record in records:
        kind = disposition(record)
        if kind == "hard_skip":
            print(f"SKIP {record['code']}: hard-skip")
        elif kind == "user_skip":
            print(f"SKIP {record['code']}: user_verdict=skip")
        elif kind == "already_reviewed":
            print(f"SKIP {record['code']}: 已有成片抽帧 review")
        elif kind == "no_link":
            print(f"SKIP {record['code']}: 没有 ed2k/magnet")
    if dry or status:
        if status:
            progress = load_progress(deps.state_dir / "progress.json")
            blocked = read_blocked(deps.state_dir)
            print(format_status(progress))
            costs = progress.get("costs") or {}
            print(
                "costs:"
                f" deepseek_cny={costs.get('deepseek_cny') or 0}"
                f" luna_usd_est={costs.get('luna_usd_est') or 0}"
            )
            if blocked:
                print("BLOCKED:")
                print(redact(blocked))
            elif progress.get("blocked"):
                print(f"BLOCKED: {redact(str(progress.get('blocked')))[:200]}")
        print("library-scan: 只读结束（未交磁、未刮削、未 promote）")
        return 0

    deps.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = deps.state_dir / "lock"
    lock_fh = open(lock_path, "a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        print("ERROR: 已有 library-scan 在跑。这次退出，避免两份进度互相覆盖。")
        return 1
    try:
        progress = load_progress(deps.state_dir / "progress.json")
        passes = 0
        while True:
            now = deps.now()
            outcome = one_pass(deps, progress, records, now)
            records = build_records(corpus, deps.review_status, codes)
            _remember_lists(progress, records)
            _save(deps, progress, deps.now())
            passes += 1
            max_passes = getattr(deps, "max_passes", None)
            if once or (max_passes and passes >= int(max_passes)):
                return 0
            if outcome == "stop":
                return 0
            delay = _idle_seconds(progress, deps.now())
            deps.sleep(delay)
            records = build_records(corpus, deps.review_status, codes)
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()


def _review_status_default(code: str) -> str:
    import av_auto

    folder = av_auto._official_frames_dir(_norm(code))
    has_manifest = (folder / "frames_manifest.json").is_file()
    has_review = (folder / "review.json").is_file()
    frames_ready = av_auto._frames_ready(folder)
    review = av_auto._complete_review(folder)
    needs = bool(review) and av_auto.tiebreak_trigger(review) != ""
    trigger = av_auto.tiebreak_trigger(review) if review else ""
    done = bool(trigger) and av_auto._grok_already_done(folder, trigger, _norm(code))
    return dense_bucket(
        frames_ready=frames_ready,
        review_complete=review is not None,
        needs_tiebreak=needs,
        tiebreak_done=done,
        has_manifest=has_manifest,
        has_review_file=has_review,
    )


def _files_of_default(code: str) -> dict:
    import av_auto

    code = _norm(code)
    strm = ""
    try:
        paths = av_auto._paths()
        found = av_auto._find_strm_for_code(code, paths)
        if found is not None:
            strm = str(found)
    except Exception:
        found = None
    if not strm:
        try:
            if av_auto._pending_strm_payload(code):
                materialized = av_auto._materialize_pending_strm(code)
                if materialized is not None:
                    strm = str(materialized)
        except Exception:
            pass
    try:
        layout = av_auto._offline_layout(code)
    except Exception as exc:
        return {"action": "error", "strm": strm, "reason": redact(str(exc))[:200]}
    inbox = bool(layout.get("inbox"))
    keepers = int(layout.get("dest_keepers") or 0)
    if inbox or (keepers and not strm):
        return {"action": "process", "strm": strm}
    if strm:
        return {"action": "frames", "strm": strm}
    return {"action": "none", "strm": ""}


def _tasks_default() -> dict | None:
    import av_auto

    try:
        client = av_auto._p115()
        rows = av_auto._list_tasks(client, "", "")
    except Exception as exc:
        print(f"NOTE: 读不到 115 离线任务（{redact(str(exc))[:160]}）。本轮不新交磁。")
        return None
    found: dict[str, dict] = {}
    rank = {"run": 3, "unknown": 2, "fail": 1, "done": 0}
    code_re = re.compile(
        r"(?i)(?:FC2[-_]?PPV[-_]?\d{5,}|[A-Z]{2,10}-\d{2,5}|[0-9]{6}[-_]\d{2,3})"
    )
    for task in rows:
        if not isinstance(task, dict):
            continue
        name = str(task.get("name") or task.get("file_name") or "")
        match = code_re.search(name)
        if not match:
            continue
        code = _norm(match.group(0))
        bucket = av_auto._task_status_bucket(task)
        item = {
            "bucket": bucket,
            "name": redact(name)[:180],
            "percentDone": task.get("percentDone", task.get("percent")),
            "rate": task.get("rate", task.get("speed")),
        }
        current = found.get(code)
        if current is None or rank.get(bucket, 0) > rank.get(str(current.get("bucket")), 0):
            found[code] = item
    return found


def _safe_result(result) -> dict:
    if isinstance(result, dict):
        return result
    return {"message": "non-dict"}


def make_submit(perform, directory: Path | None = None):
    """Submit via a mode-600 file. ``perform(code, uri)`` sees the value only in memory."""

    def submit(code: str, uri: str) -> dict:
        def invoke(code_arg: str, loaded: str) -> dict:
            try:
                out = perform(code_arg, loaded)
            except Exception as exc:
                detail = redact(str(exc))[:200]
                print(f"SUBMIT {code_arg} fail {detail}")
                return {"class": "fail", "detail": detail}
            result = _safe_result(out.get("result") if isinstance(out, dict) else out)
            klass = classify_result(result)
            if isinstance(out, dict) and out.get("rc") not in (None, 0, "0") and klass == "ok":
                klass = "fail"
            detail = redact(signal_from_result(result))[:200]
            print(f"SUBMIT {code_arg} {klass} {detail}")
            return {"class": klass, "detail": detail}

        _mode, outcome = invoke_uri_file(code, uri, invoke, directory)
        return outcome

    return submit


def _perform_submit_default(code: str, uri: str) -> dict:
    import av_auto

    return av_auto.perform_submit(code, uri)


def _process_default(code: str) -> int:
    import av_auto

    return int(av_auto._process_offline_title(code) or 0)


def _frames_default(code: str, strm: str) -> int:
    import av_auto

    rc = int(av_auto.cmd_frames(argparse.Namespace(
        code=code,
        strm=strm,
        out="",
        cleanup=False,
        no_precheck=False,
        no_reuse=False,
    )) or 0)
    if rc != 0:
        return rc
    folder, persist_rc = av_auto._select_frames_dir(code)
    if persist_rc != 0:
        return persist_rc
    _review, rc = av_auto._process_review(code, folder)
    if rc != 0:
        return rc
    review = av_auto._complete_review(folder) or {}
    rc = int(av_auto._process_grok(code, folder, review) or 0)
    if rc != 0:
        return rc
    return int(av_auto._persist_frames_dir(code, folder, require_review=True) or 0)


def _call_quiet(obj, names: tuple[str, ...]):
    for name in names:
        fn = getattr(obj, name, None)
        if not callable(fn):
            continue
        try:
            return fn()
        except TypeError:
            continue
        except Exception as exc:
            print(f"NOTE: 115 {name} 失败（{redact(str(exc))[:120]}）。未知不当成磁盘满。")
            return None
    return None


def _quota_and_cloud_default() -> tuple[int | None, int | None]:
    import av_auto

    client = av_auto._p115()
    quota_payload = _call_quiet(client, ("offline_quota", "offline_get_quota"))
    if quota_payload is None:
        offline = getattr(client, "offline", None)
        if offline is not None:
            quota_payload = _call_quiet(offline, ("quota", "get_quota"))
    space_payload = _call_quiet(client, ("user_storage_info", "user_info"))
    quota = parse_quota(quota_payload)
    cloud = parse_cloud_remain_bytes(space_payload)
    if cloud is None:
        cloud = parse_cloud_remain_bytes(quota_payload)
    return quota.get("surplus"), cloud


def _space_default() -> dict:
    import av_auto

    frames = av_auto._stat_free_bytes(av_auto._official_frames_root())
    media_path = Path(os.environ.get("AV_AUTO_MEDIA_PATH", "/media"))
    cache_path = Path(os.environ.get("AV_AUTO_CACHE_PATH", "/mnt/cache"))
    media = av_auto._stat_free_bytes(media_path)
    cache = av_auto._stat_free_bytes(cache_path) if cache_path.exists() else None
    surplus = None
    cloud = None
    try:
        surplus, cloud = _quota_and_cloud_default()
    except Exception as exc:
        print(f"NOTE: 读不到 115 配额或云盘剩余（{redact(str(exc))[:160]}）。未知不当成磁盘满，也不当成配额用尽。")
    return {
        "frames_free": frames,
        "media_free": media,
        "cache_free": cache,
        "cloud_remain": cloud,
        "offline_surplus": surplus,
    }


def _cost_default(code: str) -> dict:
    import av_auto

    folder = av_auto._official_frames_dir(_norm(code))
    total = {"deepseek_cny": 0.0, "luna_usd_est": 0.0}
    for name in ("frames_manifest.json", "review.json", "grok_review.json"):
        path = folder / name
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cost = doc.get("cost") if isinstance(doc, dict) else None
        if not isinstance(cost, dict):
            continue
        for key in total:
            try:
                total[key] += float(cost.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return total


def _load_corpus_default() -> dict:
    raw = (os.environ.get("AV_AUTO_CORPUS_FILE") or "").strip()
    if not raw:
        raise RuntimeError("没有 AV_AUTO_CORPUS_FILE。请用官方入口 av_auto library-scan")
    path = Path(raw)
    if not path.is_file():
        raise RuntimeError("找不到 findav 语料文件")
    return load_corpus_file(path, unlink=True)


class ScanDeps:
    """Production hooks. Tests replace methods or build their own object."""

    def __init__(self) -> None:
        self.state_dir = Path(os.environ.get("AV_AUTO_LIBRARY_STATE", str(STATE_DIR_DEFAULT)))
        self.submit_gap_sec = int(os.environ.get("AV_AUTO_SUBMIT_GAP", str(SUBMIT_GAP_SEC)))
        self.backoff_sec = int(os.environ.get("AV_AUTO_406_BACKOFF", str(BACKOFF_406_SEC)))
        self.fail_retry_sec = int(os.environ.get("AV_AUTO_FAIL_RETRY", str(FAIL_RETRY_SEC)))
        self.stuck_after_sec = int(os.environ.get("AV_AUTO_STUCK_SEC", str(STUCK_AFTER_SEC)))
        self.max_passes = None
        self.submit = make_submit(_perform_submit_default)
        self._now = _now_default

    def now(self) -> datetime:
        return self._now()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(max(0, float(seconds)))

    def load_corpus(self) -> dict:
        return _load_corpus_default()

    def review_status(self, code: str) -> str:
        return _review_status_default(code)

    def files_of(self, code: str) -> dict:
        return _files_of_default(code)

    def tasks(self) -> dict | None:
        return _tasks_default()

    def process_title(self, code: str) -> int:
        return _process_default(code)

    def frames_title(self, code: str, strm: str) -> int:
        return _frames_default(code, strm)

    def quota(self) -> dict:
        surplus, _cloud = _quota_and_cloud_default()
        return {"surplus": surplus}

    def space(self) -> dict:
        return _space_default()

    def cost_of(self, code: str) -> dict:
        return _cost_default(code)


def default_deps() -> ScanDeps:
    return ScanDeps()


def cmd_library_scan(args) -> int:
    # Fail fast before a ~10h inventory when the LLM runtime is missing:
    # ready processing needs it and would otherwise fail every title at the end.
    if not getattr(args, "status", False) and not getattr(args, "dry_run", False):
        import av_auto

        av_auto._load_llm_runtime()
    return int(execute(args) or 0)
