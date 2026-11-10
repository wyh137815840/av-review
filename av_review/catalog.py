from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

from av_review.constants import (
    AV_ITEMS_REMOTE,
    CATALOG_JUDGE_SYSTEM,
    FINDAV_DB_CONTAINER,
    HARD_SKIP,
    MAGPIE,
    MEDIA_AV_REMOTE,
    PROFILE_REMOTE,
    TZ,
    WORK,
    XFER,
)
from av_review.cost import compute_cost
from av_review.secrets import load_magpie_key, refresh_secrets
from av_review.util import (
    actresses_of_item,
    extract_code_from_text,
    extract_item_code,
    now_iso,
    now_stamp,
    norm_code,
    parse_json_obj,
    publish_pair,
    scp_from,
    scp_to,
    ssh,
)


def _ssh_mariadb_tsv() -> str:
    """Run a read-only SELECT on findav MariaDB via Unraid docker. Password never printed."""
    remote_out = "/tmp/av_review_findav_titles.tsv"
    script = f"""
set -euo pipefail
set -a
source /mnt/user/appdata/findav/connection.env
set +a
docker exec -i {FINDAV_DB_CONTAINER} mariadb -u"$FINDDAV_USER" -p"$FINDDAV_PASSWORD" -D "$FINDDAV_DB" -N -B --default-character-set=utf8mb4 <<'SQL' 2>/dev/null | grep -v "Using a password" > {remote_out}
SELECT code, IFNULL(code_raw,""), IFNULL(title_jp,""), IFNULL(title_zh,""), IFNULL(actress,""), IFNULL(beauty_score,""), IFNULL(erotic_level,""), IFNULL(erotic_why,""), IFNULL(body_type,""), user_verdict, IFNULL(skip_reason,""), IFNULL(emby_item_id,""), scrape_status, first_seen_at, updated_at
FROM av_title;
SQL
wc -l {remote_out}
"""
    r = ssh(script, timeout=60, check=True)
    return remote_out


def load_findav_titles() -> list[dict]:
    remote = _ssh_mariadb_tsv()
    local = WORK / "findav_titles.tsv"
    WORK.mkdir(parents=True, exist_ok=True)
    scp_from(remote, str(local))
    ssh(f"rm -f {remote}", timeout=15)
    rows = []
    for line in local.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 10:
            continue
        while len(parts) < 15:
            parts.append("")
        actress_raw = parts[4]
        try:
            actress = json.loads(actress_raw) if actress_raw else []
            if not isinstance(actress, list):
                actress = [str(actress)]
        except Exception:
            actress = [actress_raw] if actress_raw else []
        bs = parts[5].strip()
        rows.append({
            "code": norm_code(parts[0]),
            "code_raw": parts[1],
            "title_jp": parts[2],
            "title_zh": parts[3],
            "actress": actress,
            "beauty_score": float(bs) if bs else None,
            "erotic_level": parts[6] or None,
            "erotic_why": parts[7],
            "body_type": parts[8] or None,
            "user_verdict": parts[9] or "pending",
            "skip_reason": parts[10],
            "emby_item_id": parts[11] or None,
            "scrape_status": parts[12],
            "first_seen_at": parts[13],
            "updated_at": parts[14],
        })
    try:
        local.unlink()
    except Exception:
        pass
    return rows


def load_emby_from_raw() -> tuple[dict[str, list[dict]], dict]:
    local = WORK / "av_items_raw.json"
    WORK.mkdir(parents=True, exist_ok=True)
    scp_from(AV_ITEMS_REMOTE, str(local))
    raw = json.loads(local.read_text(encoding="utf-8"))
    items = raw.get("Items") or []
    by: dict[str, list[dict]] = {}
    no_code = 0
    for it in items:
        c = extract_item_code(it)
        if not c:
            no_code += 1
            continue
        slim = {
            "emby_id": str(it.get("Id") or ""),
            "code": c,
            "title": (it.get("Name") or "")[:120],
            "actresses": actresses_of_item(it),
            "path": it.get("Path"),
            "year": it.get("ProductionYear"),
        }
        by.setdefault(c, []).append(slim)
    meta = {
        "source": "av_items_raw.json",
        "raw_items": len(items),
        "unique_codes": len(by),
        "no_code": no_code,
        "total_record_count": raw.get("TotalRecordCount"),
    }
    return by, meta


def supplement_strm_codes(emby_by: dict[str, list[dict]]) -> dict:
    """List local .strm basenames on Unraid (no ffprobe, no Emby image traffic)."""
    r = ssh(
        f"find {MEDIA_AV_REMOTE} -type f -name '*.strm' 2>/dev/null | sed 's|.*/||; s/\\.[Ss][Tt][Rr][Mm]$//'",
        timeout=60,
    )
    extra = []
    for name in (r.stdout or "").splitlines():
        name = name.strip()
        if not name:
            continue
        # strip domain@ prefixes and trailing -1/-2
        c = extract_code_from_text(name)
        if c and c not in emby_by:
            extra.append(c)
            emby_by[c] = [{"emby_id": "", "code": c, "title": name, "actresses": [], "path": None, "year": None, "from": "strm"}]
    return {"strm_only_codes": sorted(set(extra)), "n_strm_only": len(set(extra))}


def load_user_profile() -> dict:
    local = WORK / "user_profile.json"
    WORK.mkdir(parents=True, exist_ok=True)
    scp_from("/mnt/user/appdata/findav/profile/user_profile.json", str(local))
    return json.loads(local.read_text(encoding="utf-8"))


def rule_prefilter(row: dict, profile: dict) -> dict:
    av = (profile or {}).get("av") or {}
    brands = [str(b).upper() for b in (av.get("brand_pref") or [])]
    pref_acts = set(av.get("actress_pref") or [])
    skip_acts = set(av.get("actress_skip_suggest") or [])
    acts = set(row.get("actress") or [])
    flags = []
    score = 0
    code = row["code"]
    if row.get("user_verdict") in ("skip", "dislike"):
        flags.append("user_verdict_skip")
        return {"bucket": "exclude", "score": -100, "flags": flags, "note": row.get("skip_reason") or "user_verdict skip/dislike"}
    if skip_acts & acts:
        flags.append("actress_skip_suggest")
        return {"bucket": "exclude", "score": -50, "flags": flags, "note": "画像建议跳过该女优"}
    brand_hit = next((b for b in brands if code.startswith(b + "-") or code.startswith(b)), None)
    if brand_hit:
        flags.append(f"brand:{brand_hit}")
        score += 3
    act_hits = sorted(pref_acts & acts)
    if act_hits:
        flags.append("actress_pref:" + ",".join(act_hits))
        score += 5
    if row.get("beauty_score") is not None:
        bs = float(row["beauty_score"])
        flags.append(f"beauty:{bs:.3f}")
        if bs >= 80:
            score += 3
        elif bs >= 70:
            score += 1
        elif bs < 50:
            score -= 1
    el = row.get("erotic_level")
    if el == "high":
        flags.append("erotic:high")
        score += 1
    elif el == "low":
        flags.append("erotic:low")
        score -= 1
    if score >= 5:
        bucket = "priority"
    elif score >= 2:
        bucket = "review"
    else:
        bucket = "other"
    return {"bucket": bucket, "score": score, "flags": flags, "note": "规则预筛，尚未经 Magpie 文本判断"}


def write_catalog_md(doc: dict, path: Path):
    s = doc["summary"]
    lines = [
        "# findav 库内尚未入 Emby 的候选短名单（待判断）",
        "",
        f"生成时间：{doc['generated_at']}",
        "",
        "数据库：Unraid 上的 **findav** MariaDB（容器 `findav-db`，mariadb:11.4，端口 3316，库名 findav）。",
        "表：`av_title` / `av_source` / `av_magnet` / `av_actress_skip` / `ingest_batch`。本轮只读 `av_title`。",
        "对照：`av_items_raw.json` 中的 Emby AV 条目（按番号去重；修正 hhd800.com 前缀与「4512681 3天」误粘连）。可选补充本地 `.strm` 文件名。",
        "说明：`av_title.emby_item_id` 目前全部为空，不能靠该字段判断是否已入库，只能按番号对照。",
        "",
        "**未调用 Magpie、未识图、未抽帧、未交磁、未入库。** `--judge` 默认关闭。下列规则预筛只是排序，不是最终优劣判断。",
        "",
        "## 摘要",
        "",
        f"- findav `av_title`：{s['findav_titles']} 条。",
        f"- Emby AV 唯一番号：{s['emby_unique']}（原始条目 {s['emby_raw_items']}）。",
        f"- 两边都有：{s['in_both']}。",
        f"- **库里有、Emby 还没有：{s['missing']}**（待 Magpie 文本判断）。",
        f"- Emby 有、findav 没有：{s['emby_only']}（多见于 FC2 / 日付番号等，仅备查）。",
        f"- 规则预筛：priority {s['bucket_priority']}，review {s['bucket_review']}，other {s['bucket_other']}，exclude {s['bucket_exclude']}。",
        "",
        "## 用法（判断请等识图空档）",
        "",
        "```bash",
        "/home/yuanhao/av-review/bin/av_review catalog          # 默认 --no-judge，只刷新短名单",
        "/home/yuanhao/av-review/bin/av_review catalog --judge  # 文本判断（DeepSeek-flash；勿与识图抢配额）",
        "# 不要加交磁/入库开关：本工具不提供自动交磁。",
        "```",
        "",
        "## 优先预筛（priority，仍待判断）",
        "",
    ]
    prio = [x for x in doc["missing"] if x.get("prefilter", {}).get("bucket") == "priority"]
    if not prio:
        lines.append("（无）")
        lines.append("")
    else:
        lines += ["| 番号 | 演员 | beauty | erotic | 预筛分 | 标记 |", "|---|---|---|---|---|---|"]
        for x in prio:
            acts = "、".join(x.get("actress") or []) or "（未知）"
            bs = x.get("beauty_score")
            bs_s = f"{bs:.3f}" if isinstance(bs, (int, float)) else ""
            pf = x.get("prefilter") or {}
            flags = ",".join(pf.get("flags") or [])
            lines.append(f"| {x['code']} | {acts} | {bs_s} | {x.get('erotic_level') or ''} | {pf.get('score')} | {flags} |")
        lines.append("")
    lines += ["## 全量待判断清单（库有 Emby 无）", "", f"共 {s['missing']} 条，完整 JSON 见同名 `.json`。下面只列前 80 条（按预筛分、beauty 降序）。", ""]
    lines += ["| 番号 | 演员 | beauty | erotic | bucket | 分 |", "|---|---|---|---|---|---|"]
    for x in doc["missing"][:80]:
        acts = "、".join(x.get("actress") or []) or "（未知）"
        bs = x.get("beauty_score")
        bs_s = f"{bs:.3f}" if isinstance(bs, (int, float)) else ""
        pf = x.get("prefilter") or {}
        lines.append(f"| {x['code']} | {acts} | {bs_s} | {x.get('erotic_level') or ''} | {pf.get('bucket')} | {pf.get('score')} |")
    if s["missing"] > 80:
        lines.append("")
        lines.append(f"（其余 {s['missing']-80} 条见 JSON。）")
    lines += ["", "## 说明", ""]
    for n in doc.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    if doc.get("judge"):
        lines += ["## Magpie 文本判断", "", (doc["judge"].get("markdown") or ""), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _sort_key(row: dict):
    pf = row.get("prefilter") or {}
    order = {"priority": 0, "review": 1, "other": 2, "exclude": 3}
    bs = row.get("beauty_score")
    return (order.get(pf.get("bucket"), 9), -(pf.get("score") or 0), -(bs or 0), row.get("code") or "")


def run_catalog(judge: bool = False, dry_run: bool = False, limit: int | None = None, include_strm: bool = True) -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "out").mkdir(parents=True, exist_ok=True)
    titles = load_findav_titles()
    emby_by, emby_meta = load_emby_from_raw()
    strm_meta = supplement_strm_codes(emby_by) if include_strm else {"n_strm_only": 0, "strm_only_codes": []}
    profile = load_user_profile()
    fset = {r["code"] for r in titles if r.get("code")}
    eset = set(emby_by)
    missing_codes = fset - eset
    both = sorted(fset & eset)
    emby_only = sorted(eset - fset)

    missing = []
    for r in titles:
        if r["code"] not in missing_codes:
            continue
        rec = dict(r)
        rec["in_emby"] = False
        rec["prefilter"] = rule_prefilter(r, profile)
        rec["judge"] = None
        rec["status"] = "pending_judge"
        missing.append(rec)
    missing.sort(key=_sort_key)

    if limit is not None and limit >= 0:
        # limit only affects judge batch, not the saved full list, except dry preview
        pass

    buckets = Counter((r["prefilter"]["bucket"] for r in missing))
    summary = {
        "findav_titles": len(titles),
        "emby_raw_items": emby_meta.get("raw_items"),
        "emby_unique": len(eset),
        "in_both": len(both),
        "missing": len(missing),
        "emby_only": len(emby_only),
        "bucket_priority": buckets.get("priority", 0),
        "bucket_review": buckets.get("review", 0),
        "bucket_other": buckets.get("other", 0),
        "bucket_exclude": buckets.get("exclude", 0),
        "strm_supplement": strm_meta,
        "emby_source": emby_meta,
    }
    doc = {
        "title": "findav 库内尚未入 Emby 的候选短名单（待判断）",
        "generated_at": now_iso(),
        "database": {
            "engine": "MariaDB 11.4",
            "container": "findav-db",
            "host": "192.168.1.9",
            "port": 3316,
            "name": "findav",
            "tables": ["av_title", "av_source", "av_magnet", "av_actress_skip", "ingest_batch"],
            "note": "av_title.emby_item_id 目前全部为空，对照只能按番号。本轮只读。",
        },
        "summary": summary,
        "in_both_codes": both,
        "emby_only_codes": emby_only,
        "missing": missing,
        "judge": None,
        "notes": [
            "未调用 Magpie 识图，未批量抽帧，未改 media_llm_route.json，未删库、未移库、未交磁、未入库。",
            "规则预筛依据 user_profile 的厂牌/女优偏好以及库内 beauty_score、erotic_level、user_verdict；不是最终判断。",
            "Magpie 文本判断请在识图空档使用：/home/yuanhao/av-review/bin/av_review catalog --judge",
            "HPET-022 / MFYD-175 若出现在名单中也不会被本工具处置。",
        ],
    }

    if judge and not dry_run:
        raise RuntimeError("internal: judge path should be called via run_catalog_judge")

    stamp = now_stamp()
    out_json = WORK / "out" / f"av_catalog_missing_{stamp}.json"
    out_md = WORK / "out" / f"av_catalog_missing_{stamp}.md"
    if dry_run:
        doc["dry_run"] = True
        doc["would_write"] = [str(out_json), str(out_md), "profile/av_catalog_missing_latest.{json,md}"]
        return doc

    out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    write_catalog_md(doc, out_md)
    publish_pair(out_json, out_md, also_latest_prefix="av_catalog_missing_latest")
    doc["written"] = {
        "json": str(out_json),
        "md": str(out_md),
        "profile": f"{PROFILE_REMOTE}/av_catalog_missing_{stamp}.json",
        "latest": f"{PROFILE_REMOTE}/av_catalog_missing_latest.json",
        "xfer": str(XFER / out_json.name),
    }
    return doc



def load_missing_source() -> dict:
    """Read the existing shortlist. Never rewrite av_catalog_missing_* source files."""
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "out").mkdir(parents=True, exist_ok=True)
    local = WORK / "av_catalog_missing_source.json"
    scp_from(f"{PROFILE_REMOTE}/av_catalog_missing_latest.json", str(local))
    doc = json.loads(local.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("missing"), list):
        raise RuntimeError("av_catalog_missing_latest.json missing or has no missing[]")
    return doc


def _judge_sort_key(row: dict):
    pf = row.get("prefilter") or {}
    order = {"priority": 0, "review": 1, "other": 2, "exclude": 3}
    bs = row.get("beauty_score")
    return (order.get(pf.get("bucket"), 9), -(pf.get("score") or 0), -(bs or 0), row.get("code") or "")


def _chat_text(opener, key: str, model: str, rec: dict, timeout: float = 90) -> dict:
    import time
    import urllib.error
    import urllib.request
    text = (
        f"番号 {rec['code']} 演员：{'、'.join(rec.get('actress') or []) or '未知'}。"
        f"beauty_score={rec.get('beauty_score')} erotic_level={rec.get('erotic_level')} "
        f"erotic_why={rec.get('erotic_why') or ''} body_type={rec.get('body_type') or ''} "
        f"user_verdict={rec.get('user_verdict')}。"
        f"规则预筛：{json.dumps(rec.get('prefilter') or {}, ensure_ascii=False)}。"
        "请只根据这些元数据给出 JSON 判断，不要假装看过封面或影片。"
    )
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": CATALOG_JUDGE_SYSTEM},
            {"role": "user", "content": text},
        ],
    }
    last = None
    for attempt in range(1, 4):
        t0 = time.time()
        req = urllib.request.Request(
            MAGPIE + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
                content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                parsed = parse_json_obj(content) or {}
                verdict = parsed.get("verdict") if parsed.get("verdict") in ("pass", "fail", "uncertain") else "uncertain"
                rec_out = {
                    "ok": bool(parsed) and verdict in ("pass", "fail", "uncertain") and not (not parsed.get("verdict")),
                    "verdict": verdict,
                    "hits": parsed.get("hits") or [],
                    "reason": parsed.get("reason") or ("响应无法解析为约定 JSON" if not parsed else ""),
                    "confidence": parsed.get("confidence") or "",
                    "usage": payload.get("usage"),
                    "http": r.status,
                    "elapsed_s": round(time.time() - t0, 2),
                    "attempts": attempt,
                    "model": model,
                    "parse_error": not parsed or parsed.get("verdict") not in ("pass", "fail", "uncertain"),
                }
                if rec_out.get("parse_error") and attempt < 3:
                    last = rec_out
                    time.sleep(attempt)
                    continue
                rec_out["ok"] = not rec_out.get("parse_error")
                return rec_out
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", "replace")[:500]
            last = {
                "ok": False, "verdict": "uncertain", "hits": [], "reason": f"HTTP {e.code}",
                "error": err, "http": e.code, "elapsed_s": round(time.time() - t0, 2),
                "attempts": attempt, "model": model,
                "retryable": e.code in (422, 429, 500, 502, 503, 504),
            }
            if last["retryable"] and attempt < 3:
                time.sleep(2 * attempt)
                continue
            return last
        except Exception as e:
            last = {
                "ok": False, "verdict": "uncertain", "hits": [], "reason": type(e).__name__,
                "error": type(e).__name__, "http": None, "elapsed_s": round(time.time() - t0, 2),
                "attempts": attempt, "model": model,
                "retryable": "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower(),
            }
            if last["retryable"] and attempt < 3:
                time.sleep(2 * attempt)
                continue
            return last
    return last or {"ok": False, "verdict": "uncertain", "reason": "no attempt", "model": model}


def _agreement(ds: str, lu: str) -> str:
    if ds == lu and ds in ("pass", "fail", "uncertain"):
        return f"两家一致 {ds}"
    if not ds or not lu or "error" in (ds, lu):
        return f"不完全可比（{ds} / {lu}）"
    return f"分歧（{ds} / {lu}）"


def _consensus(ds: str, lu: str) -> str:
    if ds == lu and ds in ("pass", "fail", "uncertain"):
        return ds
    return "disagree"


def _tier(consensus: str) -> str:
    if consensus == "pass":
        return "建议入库"
    if consensus == "fail":
        return "不建议"
    return "再看"


def write_judged_md(doc: dict, path: Path):
    s = doc.get("summary") or {}
    j = doc.get("judge") or {}
    lines = [
        "# findav 库外候选 — Magpie 文本判断短名单",
        "",
        f"生成时间：{doc.get('generated_at')}",
        f"判断窗口：{j.get('started_at') or ''} – {j.get('ended_at') or ''}",
        "",
        "只根据番号/演员/beauty_score/erotic_level 等元数据判断，**未看封面、未抽帧、未识图复查、未调 Grok**。",
        "模型：`deepseek/deepseek-flash` + `chatgpt-api/gpt-6-luna`。未改 `media_llm_route.json`，未交磁、未入库。",
        "源清单 `av_catalog_missing_latest.json` 未覆盖。本文件为判断结果。",
        "",
        "## 摘要",
        "",
        f"- 源清单库外 {s.get('missing')} 条；本轮待判 {j.get('n_todo')}，已判 {j.get('n_done')}，失败/解析问题 {j.get('n_failed')}，跳过 {j.get('n_skipped')}，未跑到 {j.get('n_pending')}。",
        f"- 分层：建议入库 {s.get('tier_keep', 0)}，再看 {s.get('tier_review', 0)}，不建议 {s.get('tier_drop', 0)}。",
        f"- 两家一致 pass {s.get('agree_pass', 0)}，一致 fail {s.get('agree_fail', 0)}，一致 uncertain {s.get('agree_uncertain', 0)}，分歧 {s.get('disagree', 0)}。",
        f"- 预筛优先：priority 已判 {j.get('done_priority', 0)}，review 已判 {j.get('done_review', 0)}，other 已判 {j.get('done_other', 0)}。",
        "",
        "## 建议入库（两家一致 pass）",
        "",
    ]
    keep = [x for x in doc.get("missing") or [] if x.get("tier") == "建议入库"]
    if not keep:
        lines += ["（无）", ""]
    else:
        lines += ["| 番号 | 演员 | beauty | erotic | 预筛 | 理由（摘要） |", "|---|---|---|---|---|---|"]
        for x in keep:
            acts = "、".join(x.get("actress") or []) or "（未知）"
            bs = x.get("beauty_score")
            bs_s = f"{bs:.3f}" if isinstance(bs, (int, float)) else ""
            bucket = (x.get("prefilter") or {}).get("bucket") or ""
            reason = ((x.get("models") or {}).get("deepseek/deepseek-flash") or {}).get("reason") or x.get("reason") or ""
            reason = str(reason).replace("\n", " ").replace("|", "\\|")[:120]
            lines.append(f"| {x['code']} | {acts} | {bs_s} | {x.get('erotic_level') or ''} | {bucket} | {reason} |")
        lines.append("")
    lines += ["## 不建议（两家一致 fail）", ""]
    drop = [x for x in doc.get("missing") or [] if x.get("tier") == "不建议"]
    if not drop:
        lines += ["（无）", ""]
    else:
        lines += ["| 番号 | 演员 | 预筛 | DS | luna | 理由 |", "|---|---|---|---|---|---|"]
        for x in drop:
            acts = "、".join(x.get("actress") or []) or "（未知）"
            ds = (x.get("models") or {}).get("deepseek/deepseek-flash") or {}
            lu = (x.get("models") or {}).get("chatgpt-api/gpt-6-luna") or {}
            reason = (ds.get("reason") or lu.get("reason") or "").replace("\n", " ").replace("|", "\\|")[:140]
            lines.append(f"| {x['code']} | {acts} | {(x.get('prefilter') or {}).get('bucket') or ''} | {ds.get('verdict')} | {lu.get('verdict')} | {reason} |")
        lines.append("")
    lines += ["## 再看（分歧或 uncertain / 失败）", ""]
    review = [x for x in doc.get("missing") or [] if x.get("tier") == "再看" and x.get("status") not in (None, "pending_judge")]
    lines.append(f"共 {len(review)} 条。完整对照见 JSON。下面列出分歧。")
    lines += ["", "| 番号 | 演员 | 预筛 | deepseek-flash | gpt-6-luna | 一致/分歧 |", "|---|---|---|---|---|---|"]
    disag = [x for x in review if x.get("consensus") == "disagree"]
    if not disag:
        lines.append("| （无分歧） |  |  |  |  |  |")
    for x in disag:
        acts = "、".join(x.get("actress") or []) or "（未知）"
        ds = (x.get("models") or {}).get("deepseek/deepseek-flash") or {}
        lu = (x.get("models") or {}).get("chatgpt-api/gpt-6-luna") or {}
        def cell(m):
            v = m.get("verdict") or ""
            r = (m.get("reason") or m.get("error") or "").replace("\n", " ").replace("|", "\\|")[:100]
            return f"{v} / {r}"
        lines.append(f"| {x['code']} | {acts} | {(x.get('prefilter') or {}).get('bucket') or ''} | {cell(ds)} | {cell(lu)} | {x.get('agreement') or ''} |")
    lines += ["", "## Token / 费用", "", (doc.get("cost") or {}).get("markdown") or "（扫完后按 Magpie usage.jsonl 核算。）", ""]
    lines += ["## 说明", ""]
    for n in doc.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _summarize_judged(missing: list[dict], todo_n: int, failed: int, skipped: int) -> dict:
    done = [x for x in missing if x.get("consensus") in ("pass", "fail", "uncertain", "disagree")]
    ap = sum(1 for x in done if x.get("consensus") == "pass")
    af = sum(1 for x in done if x.get("consensus") == "fail")
    au = sum(1 for x in done if x.get("consensus") == "uncertain")
    dg = sum(1 for x in done if x.get("consensus") == "disagree")
    keep = sum(1 for x in missing if x.get("tier") == "建议入库")
    drop = sum(1 for x in missing if x.get("tier") == "不建议")
    rev = sum(1 for x in missing if x.get("tier") == "再看" and x.get("consensus"))
    pending = sum(1 for x in missing if not x.get("consensus"))
    by_bucket_done = {"priority": 0, "review": 0, "other": 0, "exclude": 0}
    for x in done:
        b = (x.get("prefilter") or {}).get("bucket") or "other"
        by_bucket_done[b] = by_bucket_done.get(b, 0) + 1
    return {
        "missing": len(missing),
        "agree_pass": ap, "agree_fail": af, "agree_uncertain": au, "disagree": dg,
        "tier_keep": keep, "tier_review": rev, "tier_drop": drop,
        "n_done": len(done), "n_failed": failed, "n_skipped": skipped, "n_pending": pending, "n_todo": todo_n,
        "done_priority": by_bucket_done.get("priority", 0),
        "done_review": by_bucket_done.get("review", 0),
        "done_other": by_bucket_done.get("other", 0),
    }


def run_catalog_judge(limit: int | None = None, resume: bool = True, workers: int = 8) -> dict:
    """Text-only Magpie judge with two models, incremental writes, no overwrite of source list."""
    import time
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime

    from av_review.constants import USAGE_REMOTE, VISION_MODELS
    from av_review.cost import compute_cost
    from av_review.util import assert_safe_basename, now_iso, now_stamp, publish_pair, scp_from

    MODELS = list(VISION_MODELS)  # deepseek-flash + gpt-6-luna
    for m in MODELS:
        if "minimax" in m.lower():
            raise RuntimeError("MiniMax is forbidden for catalog judge")

    refresh_secrets(need_emby=False, need_magpie=True, need_db=False)
    key = load_magpie_key()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    src = load_missing_source()
    missing = list(src.get("missing") or [])
    missing.sort(key=_judge_sort_key)

    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "out").mkdir(parents=True, exist_ok=True)
    partial_path = WORK / "out" / "av_catalog_judged_partial.json"

    # Resume from prior judged outputs (partial, latest, timestamped snapshots).
    # Later files do not wipe earlier verdicts; first consensus for a code wins unless a newer file also has one (newer overwrites).
    prev_by = {}
    if resume:
        cands = []
        for base in (WORK / "out", Path("/home/yuanhao/agent-xfer"), Path("/tmp/av-review-work/out")):
            if not base.exists():
                continue
            cands.extend(sorted(base.glob("av_catalog_judged_*.json"), key=lambda x: x.stat().st_mtime))
        # de-dupe by resolved path, keep mtime order (older first, newer overwrites)
        seen = set()
        ordered = []
        for cand in cands:
            try:
                resolved = cand.resolve()
            except Exception:
                resolved = cand
            if resolved in seen:
                continue
            seen.add(resolved)
            ordered.append(cand)
        for cand in ordered:
            try:
                prev = json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
            items = prev.get("missing") if isinstance(prev, dict) else None
            if not isinstance(items, list):
                continue
            n = 0
            for it in items:
                if not it.get("code") or it.get("consensus") not in ("pass", "fail", "uncertain", "disagree"):
                    continue
                # Do not keep transport failures (401/429/5xx) as final verdicts
                models = it.get("models") or {}
                transport_fail = False
                if models:
                    transport_fail = all(
                        (not (m or {}).get("ok")) and str((m or {}).get("http") or "") in {"401", "403", "429", "500", "502", "503", "504"}
                        or (not (m or {}).get("ok") and str((m or {}).get("reason") or "").startswith("HTTP 40"))
                        for m in models.values()
                    ) if models else False
                    # simpler: if every model is a transport error, skip
                    errs = []
                    for m in models.values():
                        http = (m or {}).get("http")
                        reason = str((m or {}).get("reason") or "")
                        errs.append((not (m or {}).get("ok")) and (http in (401, 403, 429, 500, 502, 503, 504) or reason.startswith("HTTP 40") or reason.startswith("HTTP 429") or reason.startswith("HTTP 5")))
                    transport_fail = bool(errs) and all(errs)
                if transport_fail:
                    continue
                prev_by[it["code"]] = it
                n += 1
            if n:
                print(f"resume from {cand.name}: {n} judged (pool now {len(prev_by)})", flush=True)
        if prev_by:
            print(f"resume: {len(prev_by)} unique already judged", flush=True)

    skipped, already = [], 0
    todo = []
    for rec in missing:
        bucket = (rec.get("prefilter") or {}).get("bucket")
        if bucket == "exclude":
            rec["status"] = "skipped_exclude"
            rec["tier"] = "不建议"
            rec["consensus"] = "fail"
            rec["agreement"] = "规则排除，未送模型"
            skipped.append(rec["code"])
            continue
        if rec["code"] in prev_by:
            rec.update(prev_by[rec["code"]])
            already += 1
            continue
        todo.append(rec)
    if limit is not None and limit >= 0:
        todo = todo[:limit]

    started = now_iso()
    started_ts = datetime.now(TZ)
    failed_n = 0
    done_codes = []

    stamp_run = now_stamp()
    # Incremental / final names must not clobber source or vision history
    def publish_progress(final: bool, cost=None):
        nonlocal failed_n
        ended = now_iso() if final else None
        sumj = _summarize_judged(missing, len(todo) + already, failed_n, len(skipped))
        doc = {
            "title": "findav 库外候选 — Magpie 文本判断短名单",
            "generated_at": now_iso(),
            "database": src.get("database"),
            "source_list": "av_catalog_missing_latest.json (not overwritten)",
            "summary": {**(src.get("summary") or {}), **{k: sumj[k] for k in sumj if k != "missing"}, "missing": len(missing)},
            "in_both_codes": src.get("in_both_codes"),
            "emby_only_codes": src.get("emby_only_codes"),
            "missing": missing,
            "judge": {
                "started_at": started,
                "ended_at": ended,
                "models": MODELS,
                "n_todo": len(todo) + already,
                "n_resumed": already,
                **sumj,
            },
            "cost": cost or {"markdown": "判断进行中，结束后按 usage.jsonl 回填。"},
            "notes": [
                "仅 Magpie 文本/元数据判断，未抽帧、未识图复查、未调 Grok、未改 media_llm_route.json。",
                "未删库、未移库、未交磁、未入库。",
                "未覆盖 av_catalog_missing_latest 及 mid/low/rest/grok 历史结果。",
                "分层：建议入库=两家一致 pass；不建议=两家一致 fail；再看=分歧或 uncertain/失败。",
            ],
        }
        if not final:
            doc["notes"].append(f"增量落盘：已判 {sumj['n_done']}/{len(todo)+already}。")
        # write partial locally always
        partial_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        if final:
            out_json = WORK / "out" / f"av_catalog_judged_{stamp_run}.json"
            out_md = WORK / "out" / f"av_catalog_judged_{stamp_run}.md"
            assert_safe_basename(out_json.name)
            assert_safe_basename(out_md.name)
            out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            write_judged_md(doc, out_md)
            publish_pair(out_json, out_md, also_latest_prefix="av_catalog_judged_latest")
            # also keep a copy of the last partial under a judged name
            try:
                from av_review.util import scp_to, XFER
                scp_to(str(partial_path), f"{PROFILE_REMOTE}/av_catalog_judged_partial.json")
                (XFER / "av_catalog_judged_partial.json").write_bytes(partial_path.read_bytes())
            except Exception as e:
                print("partial publish warn", e, flush=True)
            doc["written"] = {
                "json": str(out_json),
                "md": str(out_md),
                "profile": f"{PROFILE_REMOTE}/av_catalog_judged_{stamp_run}.json",
                "latest": f"{PROFILE_REMOTE}/av_catalog_judged_latest.json",
                "partial": f"{PROFILE_REMOTE}/av_catalog_judged_partial.json",
            }
        else:
            # periodic remote backup of partial only (new name)
            try:
                from av_review.util import scp_to, XFER
                scp_to(str(partial_path), f"{PROFILE_REMOTE}/av_catalog_judged_partial.json")
                XFER.mkdir(parents=True, exist_ok=True)
                (XFER / "av_catalog_judged_partial.json").write_bytes(partial_path.read_bytes())
            except Exception as e:
                print("partial scp warn", e, flush=True)
            doc["written"] = {"partial": str(partial_path)}
        return doc

    def judge_one(rec: dict) -> dict:
        models = {}
        for m in MODELS:
            models[m] = _chat_text(opener, key, m, rec)
        ds = (models.get(MODELS[0]) or {}).get("verdict") or "uncertain"
        lu = (models.get(MODELS[1]) or {}).get("verdict") or "uncertain"
        ok = all((models[m] or {}).get("ok") for m in MODELS)
        rec = dict(rec)
        rec["models"] = models
        rec["verdicts"] = {m: (models[m] or {}).get("verdict") for m in MODELS}
        rec["agreement"] = _agreement(ds, lu)
        rec["consensus"] = _consensus(ds, lu)
        rec["tier"] = _tier(rec["consensus"])
        rec["status"] = rec["consensus"]
        rec["judge"] = {
            "ok": ok,
            "verdict": rec["consensus"] if rec["consensus"] != "disagree" else "disagree",
            "models": rec["verdicts"],
        }
        if not ok:
            rec["judge_failed"] = True
        return rec

    print(f"catalog judge start todo={len(todo)} resumed={already} skipped={len(skipped)} workers={workers} models={MODELS} at {started}", flush=True)
    publish_progress(final=False)

    # Run in modest batches so we can flush incrementally
    batch = 20
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        # Submit all but collect in completion order; flush every batch
        futs = {ex.submit(judge_one, rec): rec["code"] for rec in todo}
        by_code = {r["code"]: r for r in missing}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = dict(by_code[code])
                rec["models"] = {}
                rec["consensus"] = "uncertain"
                rec["agreement"] = f"异常：{type(e).__name__}"
                rec["tier"] = "再看"
                rec["status"] = "uncertain"
                rec["judge_failed"] = True
                rec["judge"] = {"ok": False, "error": type(e).__name__}
            # write back into missing list in place
            for i, old in enumerate(missing):
                if old.get("code") == rec["code"]:
                    missing[i] = rec
                    break
            if rec.get("judge_failed") or not ((rec.get("judge") or {}).get("ok", True)):
                failed_n += 1
            completed += 1
            done_codes.append(rec["code"])
            if completed % 10 == 0 or completed == len(todo):
                print(
                    f"  progress {completed}/{len(todo)} last={rec['code']} "
                    f"tier={rec.get('tier')} consensus={rec.get('consensus')}",
                    flush=True,
                )
            if completed % batch == 0:
                publish_progress(final=False)

    # cost from usage.jsonl
    usage_local = WORK / "out" / "usage_judge.jsonl"
    try:
        scp_from(USAGE_REMOTE, str(usage_local), timeout=60)
        cost = compute_cost(usage_local, started_ts, datetime.now(TZ))
    except Exception as e:
        cost = {"ok": False, "error": f"{type(e).__name__}: {e}", "markdown": f"usage 核算失败：{type(e).__name__}: {e}"}
    if usage_local.exists():
        try:
            usage_local.unlink()
        except Exception:
            pass

    doc = publish_progress(final=True, cost=cost)
    print("DONE judge", doc.get("judge"), "written", doc.get("written"), flush=True)
    return doc


def datetime_now():
    from datetime import datetime
    return datetime.now(TZ)
