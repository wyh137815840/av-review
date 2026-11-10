from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from av_review.constants import (
    PROTECTED_BASENAMES,
    TZ,
    UNRAID,
    VISION_RESULT_PREFIXES,
    XFER,
)

CODE_RE = re.compile(
    r"(?i)(?:FC2[-_]?PPV[-_]?\d{5,}|[A-Z]{2,10}-\d{2,5}(?:[-_]\d+)?|[0-9]{6}[-_][0-9]{2,3})"
)
DOMAIN_RE = re.compile(r"(?i)https?://|\b[\w-]+\.(com|net|org|jp|tv|cc|co)\b")
DOMAIN_AT_RE = re.compile(r"(?i)[\w.-]+\.(com|net|org|jp|tv|cc|co)@")


def now_iso() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S") + " (Asia/Shanghai)"


def now_stamp() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")


def load_env(p: Path) -> dict:
    out = {}
    for line in Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ssh(cmd: str, timeout: float = 30, check: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", UNRAID, cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"ssh failed: {r.returncode} {r.stderr[-400:]}")
    return r


def scp_to(local: str, remote: str, timeout: float = 60):
    subprocess.check_call(
        ["scp", "-o", "BatchMode=yes", local, f"{UNRAID}:{remote}"], timeout=timeout
    )


def scp_from(remote: str, local: str, timeout: float = 120):
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        ["scp", "-o", "BatchMode=yes", f"{UNRAID}:{remote}", local], timeout=timeout
    )


def norm_code(c: str) -> str:
    c = (c or "").strip().upper().replace("_", "-")
    m = re.fullmatch(r"FC2(?:-PPV)?-(\d{5,})(?:-\d+)?", c)
    if m:
        return f"FC2-PPV-{m.group(1)}"
    # Collapse part/quality suffixes (MDVR-421-2-8K -> MDVR-421)
    m = re.fullmatch(r"([A-Z]{2,10}-\d{2,5})(?:-[0-9A-Z]+)+", c)
    if m:
        return m.group(1)
    return c


def extract_code_from_text(s: str) -> str:
    if not s:
        return ""
    s = DOMAIN_AT_RE.sub("", s)
    # Do not strip all spaces (avoids gluing "4512681 3天" into 45126813)
    m = CODE_RE.search(s)
    return norm_code(m.group(0)) if m else ""


def extract_item_code(it: dict) -> str:
    path = (it.get("Path") or "").replace("\\", "/")
    parts = [p for p in path.split("/") if p]
    for p in reversed(parts):
        p2 = DOMAIN_AT_RE.sub("", p)
        c = extract_code_from_text(p2)
        if c:
            return c
    for src in (
        it.get("Name") or "",
        str((it.get("ProviderIds") or {}).get("MetaTube") or ""),
    ):
        c = extract_code_from_text(src)
        if c:
            return c
    return ""


def actresses_of_item(it: dict) -> list[str]:
    out = []
    for p in it.get("People") or []:
        if (p.get("Type") or "").lower() in ("actor", "actress", ""):
            n = p.get("Name")
            if n:
                out.append(n)
    return out[:8]


def parse_json_obj(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None


def assert_safe_basename(name: str) -> str:
    base = Path(name).name
    if base in PROTECTED_BASENAMES:
        raise SystemExit(f"拒绝写入受保护的历史结果文件: {base}")
    return base


def items_from_result_doc(doc) -> list[dict]:
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    if isinstance(doc, dict):
        if isinstance(doc.get("items"), list):
            return [x for x in doc["items"] if isinstance(x, dict)]
    return []


def codes_from_result_file(path: Path) -> set[str]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for it in items_from_result_doc(doc):
        c = norm_code(it.get("code") or "")
        if c:
            out.add(c)
    return out


def is_vision_result_name(name: str) -> bool:
    if not name.endswith(".json"):
        return False
    stem = name[:-5]
    for p in VISION_RESULT_PREFIXES:
        if stem == p or stem.startswith(p):
            return True
    return False


def collect_scanned_codes(local_dirs: list[Path] | None = None) -> dict:
    """Return {code: [source_file, ...]} from local result copies. No Magpie."""
    dirs = local_dirs or [XFER, Path("/tmp/av-review-formalize"), Path("/tmp/av-review-work/out")]
    found: dict[str, list[str]] = {}
    seen_files = set()
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        for p in sorted(Path(d).glob("*.json")):
            if not is_vision_result_name(p.name) and p.name not in {
                "av_mismatch_vision_pilot5.json",
                "av_mismatch_vision_mid.json",
                "av_mismatch_vision_low.json",
                "av_mismatch_vision_grok4.json",
                "av_mismatch_vision_grok_low5.json",
                "av_mismatch_vision_rest.json",
            }:
                continue
            key = (p.name, p.stat().st_size)
            if key in seen_files:
                continue
            seen_files.add(key)
            for c in codes_from_result_file(p):
                found.setdefault(c, []).append(p.name)
    return found


def pull_profile_results(dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    r = ssh("ls -1 /mnt/user/appdata/findav/profile/av_mismatch_vision_*.json /mnt/user/appdata/findav/profile/av_review_vision_*.json 2>/dev/null || true")
    names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().endswith(".json")]
    out = []
    for remote in names:
        local = dest / Path(remote).name
        try:
            scp_from(remote, str(local))
            out.append(local)
        except Exception:
            continue
    return out


def publish_pair(json_path: Path, md_path: Path, also_latest_prefix: str | None = None):
    assert_safe_basename(json_path.name)
    assert_safe_basename(md_path.name)
    for p in (json_path, md_path):
        scp_to(str(p), f"/mnt/user/appdata/findav/profile/{p.name}")
        XFER.mkdir(parents=True, exist_ok=True)
        (XFER / p.name).write_bytes(p.read_bytes())
    if also_latest_prefix:
        for src, ext in ((json_path, ".json"), (md_path, ".md")):
            latest = also_latest_prefix + ext
            assert_safe_basename(latest)
            scp_to(str(src), f"/mnt/user/appdata/findav/profile/{latest}")
            (XFER / latest).write_bytes(src.read_bytes())


def redact(s: str) -> str:
    if not s:
        return s
    s = re.sub(r"(?i)(api[_-]?key|authorization|bearer|password|secret|token)([\"'=\s:]+)([^\s\"']+)", r"\1\2***", s)
    return s
