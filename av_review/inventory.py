from __future__ import annotations

import json
from pathlib import Path

from av_review.constants import HARD_SKIP, WORK, XFER
from av_review.util import (
    actresses_of_item,
    collect_scanned_codes,
    extract_item_code,
    pull_profile_results,
    scp_from,
)


def load_emby_items_local() -> list[dict]:
    WORK.mkdir(parents=True, exist_ok=True)
    local = WORK / "av_items_raw.json"
    scp_from("/mnt/user/appdata/findav/profile/av_items_raw.json", str(local))
    raw = json.loads(local.read_text(encoding="utf-8"))
    out = []
    for it in raw.get("Items") or []:
        code = extract_item_code(it)
        out.append({
            "code": code,
            "emby_id": str(it.get("Id") or ""),
            "title": it.get("Name"),
            "actresses": actresses_of_item(it),
            "path": it.get("Path"),
            "production_year": it.get("ProductionYear"),
        })
    return out


def scanned_codes(refresh_profile: bool = True) -> dict[str, list[str]]:
    dirs = [XFER, WORK / "profile_cache"]
    if refresh_profile:
        try:
            pull_profile_results(WORK / "profile_cache")
        except Exception as e:
            print("pull profile results warn:", e, flush=True)
    return collect_scanned_codes(dirs)


def plan_review(codes: list[str] | None = None, from_json: Path | None = None,
                skip_scanned: bool = True, skip_hard: bool = True, limit: int | None = None) -> dict:
    """Build the incremental review plan. No Magpie, no ffmpeg, no Emby image fetch."""
    scanned = scanned_codes(refresh_profile=True)
    wanted: list[dict] = []
    source = "emby_av_items_raw"

    if from_json:
        source = f"from-json:{from_json}"
        doc = json.loads(Path(from_json).read_text(encoding="utf-8"))
        items = doc.get("items") if isinstance(doc, dict) else doc
        for it in items or []:
            if isinstance(it, str):
                wanted.append({"code": it, "emby_id": "", "title": "", "actresses": []})
            elif isinstance(it, dict):
                wanted.append({
                    "code": it.get("code") or "",
                    "emby_id": str(it.get("emby_id") or it.get("emby_id") or it.get("Id") or ""),
                    "title": it.get("title") or it.get("emby_name") or it.get("Name") or "",
                    "actresses": it.get("actresses") or [],
                })
    else:
        items = load_emby_items_local()
        # unique by code, first emby id
        by = {}
        for it in items:
            if not it.get("code"):
                continue
            by.setdefault(it["code"], it)
        wanted = list(by.values())

    if codes:
        allow = {c.strip().upper().replace("_", "-") for c in codes if c.strip()}
        # also accept FC2 forms
        wanted = [w for w in wanted if w["code"] in allow or w["code"].replace("FC2-PPV-", "FC2-") in allow]

    skipped_hard, skipped_scanned, no_id = [], [], []
    todo = []
    seen = set()
    for w in wanted:
        c = w.get("code") or ""
        if not c or c in seen:
            continue
        seen.add(c)
        if skip_hard and c in HARD_SKIP:
            skipped_hard.append(c)
            continue
        if skip_scanned and c in scanned:
            skipped_scanned.append(c)
            continue
        if not w.get("emby_id"):
            no_id.append(c)
        todo.append(w)

    truncated = False
    if limit is not None and limit >= 0:
        if limit == 0:
            truncated = True
            planned = list(todo)
            todo = []
        else:
            truncated = len(todo) > limit
            todo = todo[:limit]
    else:
        planned = todo

    return {
        "source": source,
        "emby_unique_considered": len(seen),
        "already_scanned": len(scanned),
        "skipped_scanned": skipped_scanned,
        "skipped_hard": skipped_hard,
        "missing_emby_id": no_id,
        "todo": todo if limit != 0 else [],
        "todo_if_unlimited": planned if limit == 0 else todo,
        "n_todo": 0 if limit == 0 else len(todo),
        "n_would_scan": len(planned) if limit == 0 else len(todo),
        "limit": limit,
        "skip_scanned": skip_scanned,
        "hard_skip": sorted(HARD_SKIP),
        "note": "incremental: skip mid/low/pilot/rest/grok/av_review_vision results; HPET-022/MFYD-175 hard-skip",
    }
