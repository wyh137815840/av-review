from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from av_review.constants import BUSY_PATTERNS, RUN_DIR


LOCK = RUN_DIR / "vision.lock"


def _pgrep(pattern: str) -> list[str]:
    r = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True)
    lines = []
    mypid = str(os.getpid())
    parent = str(os.getppid())
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # skip our own process / grep
        if "pgrep" in ln:
            continue
        parts = ln.split(None, 1)
        pid = parts[0] if parts else ""
        if pid in {mypid, parent}:
            continue
        if "av_review" in ln and ("--help" in ln or " api" in ln or "-m av_review api" in ln):
            continue
        lines.append(ln)
    return lines


def vision_busy() -> dict:
    hits = []
    for pat in BUSY_PATTERNS:
        hits.extend(_pgrep(pat))
    # unique
    seen = set()
    out = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    lock_held = False
    lock_info = None
    if LOCK.exists():
        try:
            info = LOCK.read_text().strip()
            lock_info = info
            pid = int(info.split()[0])
            os.kill(pid, 0)
            lock_held = True
        except Exception:
            lock_held = False
    return {
        "busy": bool(out) or lock_held,
        "processes": out,
        "lock": lock_info if lock_held else None,
    }


class VisionLock:
    def __init__(self):
        self.held = False

    def acquire(self) -> bool:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        if LOCK.exists():
            try:
                pid = int(LOCK.read_text().split()[0])
                os.kill(pid, 0)
                return False
            except Exception:
                pass
        LOCK.write_text(f"{os.getpid()} {os.getpid()}\n")
        self.held = True
        return True

    def release(self):
        if self.held:
            try:
                if LOCK.exists():
                    LOCK.unlink()
            except Exception:
                pass
            self.held = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("vision lock held by another av_review process")
        return self

    def __exit__(self, *exc):
        self.release()
