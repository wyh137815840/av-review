"""Parameterized Emby AV vision review. Adapted from the proven rest/mid/low scanners.

Does not touch /tmp/rest-vision-build or av_mismatch_vision_{mid,low,pilot,rest,grok*}.
Default models: deepseek-flash + gpt-6-luna. Grok only if cfg.grok_review.
"""
from __future__ import annotations

import base64
import json
import shutil
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from av_review.constants import (
    CHUNK_SIZE,
    EMBY,
    EXPECTED_ROUTE_MD5,
    FF_BATCH_LOCAL,
    FF_BATCH_REMOTE,
    FRAME_TIMEOUT,
    GROK_MODEL,
    HARD_SKIP,
    MAGPIE,
    MAX_RETRIES,
    MAX_WORKERS,
    PROBE_TIMEOUT,
    PROFILE_REMOTE,
    PROVEN_SYSTEM,
    ROUTE_REMOTE,
    TZ,
    UNRAID_WORK,
    USAGE_REMOTE,
    VISION_MODELS,
    WORK,
    XFER,
)
from av_review.cost import compute_cost
from av_review.inventory import plan_review
from av_review.secrets import load_emby_key, load_magpie_key, refresh_secrets
from av_review.util import (
    actresses_of_item,
    assert_safe_basename,
    now_iso,
    now_stamp,
    parse_json_obj,
    publish_pair,
    scp_from,
    scp_to,
    ssh,
)


@dataclass
class ReviewCfg:
    codes: list[str] | None = None
    from_json: Path | None = None
    skip_scanned: bool = True
    limit: int | None = None
    dry_run: bool = False
    grok_review: bool = False
    chunk_size: int = CHUNK_SIZE
    max_workers: int = MAX_WORKERS
    models: list[str] = field(default_factory=lambda: list(VISION_MODELS))


EMBY_KEY = None
MAGPIE_KEY = None
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
IMG = WORK / "images"
FR = WORK / "frames"
OUT = WORK / "out"
MODELS = list(VISION_MODELS)


def emby_url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return EMBY + path + sep + "api_key=" + urllib.parse.quote(EMBY_KEY)


def emby_get_json(path: str, timeout: float = 30):
    with OPENER.open(emby_url(path), timeout=timeout) as r:
        return json.loads(r.read().decode()), r.status


def emby_get_bytes(path: str, dest: Path, timeout: float = 60) -> dict:
    try:
        with OPENER.open(emby_url(path), timeout=timeout) as r:
            data = r.read()
            dest.write_bytes(data)
            return {"ok": True, "http": r.status, "ctype": r.headers.get("Content-Type"), "bytes": len(data), "file": dest.name}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "ctype": None, "bytes": 0, "file": dest.name, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": None, "ctype": None, "bytes": 0, "file": dest.name, "error": f"{type(e).__name__}: {e}"}


def actresses_of(item: dict) -> str:
    acts = item.get("actresses") or []
    return "、".join(acts) if acts else "（未知）"


def seek_points(duration: float | None) -> list[tuple[str, int]]:
    if not duration or duration <= 10:
        return []
    pts: list[tuple[str, int]] = []
    def add(label: str, t: float):
        t = int(t)
        if 1 <= t < duration - 1 and all(abs(t - x[1]) >= 30 for x in pts):
            pts.append((label, t))
    if duration > 210:
        add("frame_180s", 180)
    else:
        add("frame_early", min(max(5, duration * 0.15), duration - 3))
    if duration > 930:
        add("frame_900s", 900)
    mid = duration * 0.5
    if duration > 930 and abs(mid - 900) < 90:
        mid = duration * 0.7
    add("frame_mid", mid)
    if not any(l == "frame_mid" for l, _ in pts):
        add("frame_mid", duration * 0.8)
    return pts


def data_uri(path: Path) -> tuple[str, int]:
    raw = path.read_bytes()
    suf = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suf, "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(raw).decode(), len(raw)


def redirect_safe(url: str) -> dict:
    u = urllib.parse.urlparse(url or "")
    return {"host": u.netloc, "path": u.path, "has_query": bool(u.query)}


def collect_images(code: str, available: list[dict]) -> list[dict]:
    out = []
    for rec in available:
        path = Path(rec["path"])
        if path.is_file() and path.stat().st_size > 500:
            uri, nbytes = data_uri(path)
            out.append({"type": rec["type"], "file": path.name, "bytes": nbytes, "note": rec["note"], "data_uri": uri})
    return out


def route_stat() -> dict:
    r = ssh(f'stat -c "%y %s" {ROUTE_REMOTE} && md5sum {ROUTE_REMOTE}', timeout=20, check=True)
    lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    md5 = lines[1].split()[0] if len(lines) > 1 else ""
    return {"raw": r.stdout.strip(), "mtime_size": lines[0] if lines else "", "md5": md5}


def install_ff_batch():
    if not FF_BATCH_LOCAL.is_file():
        raise RuntimeError(f"missing {FF_BATCH_LOCAL}")
    scp_to(str(FF_BATCH_LOCAL), FF_BATCH_REMOTE)
    ssh(f"chmod +x {FF_BATCH_REMOTE}", check=True)


def chat_once(model: str, images: list[dict], item: dict, timeout: float, system: str) -> dict:
    captions = "、".join(f"{im['type']}（{im['note']}，{im['bytes']}字节）" for im in images)
    text = (
        f"条目：{item['code']} 演员：{actresses_of(item)}。\n"
        f"本请求附图（均为本地缩图的 data URI，不是网址）：{captions}。\n"
        "请综合所有附图给出 JSON 判断。"
    )
    content = [{"type": "text", "text": text}]
    for im in images:
        content.append({"type": "image_url", "image_url": {"url": im["data_uri"]}})
    body = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 800,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        MAGPIE + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {MAGPIE_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    started = now_iso()
    try:
        with OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            elapsed = round(time.time() - t0, 2)
            payload = json.loads(raw.decode())
            msg = ((payload.get("choices") or [{}])[0].get("message") or {})
            content_out = msg.get("content") or ""
            parsed = parse_json_obj(content_out)
            rec = {
                "ok": True, "http": r.status, "elapsed_s": elapsed, "started_at": started,
                "model_requested": model, "model_echo": payload.get("model"),
                "finish_reason": ((payload.get("choices") or [{}])[0].get("finish_reason")),
                "usage": payload.get("usage"), "raw_content": content_out[:4000], "parsed": parsed, "attempts": 1,
            }
            if parsed and parsed.get("verdict") in ("pass", "fail", "uncertain"):
                rec["verdict"] = parsed.get("verdict")
                rec["hits"] = parsed.get("hits") or []
                rec["reason"] = parsed.get("reason") or ""
                rec["confidence"] = parsed.get("confidence") or ""
            else:
                rec.update({"verdict": "uncertain", "hits": [], "reason": "响应无法解析为约定 JSON", "confidence": "low", "ok": False, "parse_error": True})
            return rec
    except urllib.error.HTTPError as e:
        elapsed = round(time.time() - t0, 2)
        err = e.read().decode("utf-8", "replace")[:2000]
        return {"ok": False, "http": e.code, "elapsed_s": elapsed, "started_at": started, "model_requested": model,
                "error": err, "verdict": "uncertain", "hits": [], "reason": f"HTTP {e.code}", "confidence": "",
                "retryable": e.code in (422, 429, 500, 502, 503, 504)}
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return {"ok": False, "http": None, "elapsed_s": elapsed, "started_at": started, "model_requested": model,
                "error": f"{type(e).__name__}: {e}", "verdict": "uncertain", "hits": [], "reason": type(e).__name__,
                "confidence": "", "retryable": "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()}


def chat_with_retry(model: str, images: list[dict], item: dict, system: str) -> dict:
    last = None
    attempts = []
    for i in range(1, MAX_RETRIES + 1):
        rec = chat_once(model, images, item, timeout=180, system=system)
        rec["attempts"] = i
        attempts.append({"i": i, "http": rec.get("http"), "ok": rec.get("ok"), "elapsed_s": rec.get("elapsed_s"), "reason": rec.get("reason")})
        last = rec
        if rec.get("ok") and rec.get("verdict") in ("pass", "fail", "uncertain") and not rec.get("parse_error"):
            rec["attempt_log"] = attempts
            return rec
        retryable = rec.get("retryable") or rec.get("parse_error") or rec.get("http") in (422, 429, 500, 502, 503, 504)
        if not retryable or i == MAX_RETRIES:
            rec["attempt_log"] = attempts
            if rec.get("verdict") not in ("pass", "fail", "uncertain"):
                rec["verdict"] = "uncertain"
            return rec
        time.sleep(2 * i)
    last["attempt_log"] = attempts
    return last


def agreement(models: dict, model_ids: list[str]) -> str:
    vs = {m: (models[m].get("verdict") if models[m].get("ok") or models[m].get("verdict") in ("pass", "fail", "uncertain") else "error") for m in model_ids}
    vals = [vs[m] for m in model_ids]
    if len(set(vals)) == 1 and vals[0] in ("pass", "fail", "uncertain"):
        return f"两家一致 {vals[0]}" if len(model_ids) == 2 else f"一致 {vals[0]}"
    if "error" in vals or not all(vals):
        return "不完全可比（" + " / ".join(vals) + "）"
    return "分歧（" + " / ".join(vals) + "）"


def consensus(models: dict, model_ids: list[str]) -> str:
    vs = [models[m].get("verdict") for m in model_ids]
    if len(set(vs)) == 1 and vs[0] in ("pass", "fail", "uncertain"):
        return vs[0]
    return "disagree"


def fetch_meta_and_images(remain: list[dict]) -> dict:
    by_code = {}
    ids = [str(x["emby_id"]) for x in remain if x.get("emby_id")]
    fields = "Path,MediaSources,People,Genres,ImageTags,BackdropImageTags,ProductionYear"
    found = {}
    if ids:
        payload, _ = emby_get_json(f"/Items?Ids={','.join(ids)}&Fields={fields}")
        found = {str(it.get("Id")): it for it in (payload.get("Items") or [])}
        print(f"emby returned {len(found)}/{len(ids)} items", flush=True)
    IMG.mkdir(parents=True, exist_ok=True)
    for it in remain:
        eid = str(it.get("emby_id") or "")
        raw = found.get(eid) or {}
        src = (raw.get("MediaSources") or [{}])[0]
        rec = {
            **it,
            "emby_name": raw.get("Name"),
            "path": raw.get("Path"),
            "container": src.get("Container"),
            "redirect": src.get("Path") if str(src.get("Path") or "").startswith("http") else None,
            "redirect_safe": redirect_safe(src.get("Path") or ""),
            "image_tags": list((raw.get("ImageTags") or {}).keys()),
            "n_backdrop": len(raw.get("BackdropImageTags") or []),
            "downloads": [],
            "image_files": [],
        }
        for typ, path, note in [
            ("Primary", f"/Items/{eid}/Images/Primary?maxWidth=800&quality=80", "Emby Primary maxWidth=800 quality=80"),
            ("Thumb", f"/Items/{eid}/Images/Thumb?maxWidth=800&quality=80", "Emby Thumb maxWidth=800 quality=80（非封面）"),
            ("Backdrop", f"/Items/{eid}/Images/Backdrop/0?maxWidth=800&quality=80", "Emby Backdrop[0] maxWidth=800 quality=80（非封面）"),
        ]:
            dest = IMG / f"{it['code']}_{typ}_800q80.jpg"
            info = emby_get_bytes(path, dest) if eid else {"ok": False, "bytes": 0, "error": "no emby_id"}
            info.update({"type": typ, "note": note, "skipped": False})
            if typ == "Thumb" and not info.get("ok"):
                info["skipped"] = True
                info["skip_reason"] = "Emby 无 Thumb 或取图失败，已跳过"
            rec["downloads"].append(info)
            if info.get("ok") and info.get("bytes", 0) > 500:
                rec["image_files"].append({"type": typ, "path": str(dest), "note": note})
        by_code[it["code"]] = rec
        print(f"  stills {it['code']} P/T/B={[(d['type'], d.get('bytes') or 0) for d in rec['downloads']]}", flush=True)
    return by_code


def run_ffmpeg(by_code: dict) -> None:
    ssh(f"mkdir -p {UNRAID_WORK}/probes {UNRAID_WORK}/frames {UNRAID_WORK}/logs", check=True)
    install_ff_batch()
    jobs = []
    for code, rec in by_code.items():
        url = rec.get("redirect")
        if not url:
            rec["ffmpeg"] = {"ok_probe": False, "error": "no redirect URL", "frames": []}
            continue
        jobs.append({"action": "probe", "code": code, "url": url, "label": "probe", "ss": 0})
    jobp = WORK / "jobs.jsonl"
    jobp.write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs))
    if jobs:
        scp_to(str(jobp), f"{UNRAID_WORK}/jobs_probe.jsonl")
        print("remote probe start", flush=True)
        t0 = time.time()
        r = ssh(f"bash {FF_BATCH_REMOTE} {UNRAID_WORK}/jobs_probe.jsonl", timeout=900)
        print(r.stdout)
        if r.stderr:
            print("probe stderr", r.stderr[-500:])
        print(f"probe batch elapsed {round(time.time()-t0,1)}s exit={r.returncode}", flush=True)
        ssh(f"tar -C {UNRAID_WORK} -czf {UNRAID_WORK}/probes.tgz probes", check=True)
        scp_from(f"{UNRAID_WORK}/probes.tgz", str(WORK / "probes.tgz"))
        import subprocess
        subprocess.check_call(["tar", "-C", str(WORK), "-xzf", str(WORK / "probes.tgz")])

    frame_jobs = []
    for code, rec in by_code.items():
        pf = WORK / "probes" / f"{code}.json"
        rec.setdefault("ffmpeg", {"ok_probe": False, "frames": [], "errors": []})
        rec["ffmpeg"].setdefault("frames", [])
        rec["ffmpeg"].setdefault("errors", [])
        if not rec.get("redirect"):
            continue
        if not pf.exists():
            rec["ffmpeg"]["error"] = rec["ffmpeg"].get("error") or "probe file missing"
            rec["ffmpeg"]["seek"] = []
            continue
        try:
            info = json.loads(pf.read_text())
        except Exception as e:
            rec["ffmpeg"]["error"] = f"probe json: {e}"
            continue
        rec["ffmpeg"]["ok_probe"] = True
        rec["ffmpeg"]["probe"] = {
            "format": (info.get("format") or {}).get("format_name"),
            "duration": float((info.get("format") or {}).get("duration") or 0) or None,
            "video": next((s for s in (info.get("streams") or []) if s.get("codec_type")=="video"), None),
        }
        dur = rec["ffmpeg"]["probe"]["duration"]
        rec["ffmpeg"]["duration"] = dur
        pts = seek_points(dur)
        rec["ffmpeg"]["seek"] = pts
        url = rec.get("redirect")
        for label, ss in pts:
            frame_jobs.append({"action": "frame", "code": code, "url": url, "label": label, "ss": ss})
            rec["ffmpeg"]["frames"].append({"label": label, "ss": ss, "ok": False})
        print(f"  probe {code} dur={dur} seek={pts}", flush=True)

    if frame_jobs:
        fjp = WORK / "frame_jobs.jsonl"
        fjp.write_text("".join(json.dumps(j, ensure_ascii=False) + "\n" for j in frame_jobs))
        scp_to(str(fjp), f"{UNRAID_WORK}/jobs_frames.jsonl")
        print(f"remote frames start n={len(frame_jobs)}", flush=True)
        t0 = time.time()
        r = ssh(f"bash {FF_BATCH_REMOTE} {UNRAID_WORK}/jobs_frames.jsonl", timeout=1800)
        print(r.stdout)
        if r.stderr:
            print("frame stderr", r.stderr[-800:])
        print(f"frame batch elapsed {round(time.time()-t0,1)}s exit={r.returncode}", flush=True)
        ssh(f"tar -C {UNRAID_WORK} -czf {UNRAID_WORK}/frames.tgz frames", check=True)
        scp_from(f"{UNRAID_WORK}/frames.tgz", str(WORK / "frames.tgz"))
        import subprocess
        subprocess.check_call(["tar", "-C", str(WORK), "-xzf", str(WORK / "frames.tgz")])

    note_map = {
        "frame_180s": "ffmpeg 抽帧 ~180s，scale min(800,iw)，来自片源 redirect，非封面",
        "frame_900s": "ffmpeg 抽帧 ~900s，scale min(800,iw)，来自片源 redirect，非封面",
        "frame_mid": "ffmpeg 抽帧 片长约50%或中后段，scale min(800,iw)，来自片源 redirect，非封面",
        "frame_early": "ffmpeg 抽帧 片长短，前段，scale min(800,iw)，来自片源 redirect，非封面",
    }
    for code, rec in by_code.items():
        for fr in rec["ffmpeg"].get("frames") or []:
            label, ss = fr["label"], fr["ss"]
            path = FR / f"{code}_{label}.jpg"
            if path.is_file() and path.stat().st_size > 500:
                fr["ok"] = True
                fr["bytes"] = path.stat().st_size
                rec["image_files"].append({"type": label, "path": str(path), "note": note_map.get(label, f"ffmpeg {label} ~{ss}s")})
            else:
                fr["ok"] = False
                rec["ffmpeg"].setdefault("errors", []).append(f"{label}@{ss}s missing")


def judge_all(rows_in: list[dict], models: list[str], system: str, max_workers: int) -> list[dict]:
    prepared = []
    for rec in rows_in:
        imgs = collect_images(rec["code"], rec.get("image_files") or [])
        meta_imgs = [{k: im[k] for k in im if k != "data_uri"} for im in imgs]
        prepared.append((rec, imgs, meta_imgs, None if imgs else "no images"))

    def job(rec, imgs, model):
        print(f"  -> {rec['code']} {model} ...", flush=True)
        ret = chat_with_retry(model, imgs, rec, system)
        print(f"     {rec['code']} {model} verdict={ret.get('verdict')} http={ret.get('http')} {ret.get('elapsed_s')}s", flush=True)
        return rec["code"], model, ret

    model_rows: dict[str, dict] = {rec["code"]: {} for rec, *_ in prepared}
    futs = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for rec, imgs, meta_imgs, err in prepared:
            if err or not imgs:
                for m in models:
                    model_rows[rec["code"]][m] = {"ok": False, "verdict": "uncertain", "hits": [], "reason": "无可用图片", "confidence": "low", "http": None, "error": err or "no images"}
                continue
            for m in models:
                futs.append(ex.submit(job, rec, imgs, m))
        for fut in as_completed(futs):
            code, model, ret = fut.result()
            model_rows[code][model] = ret

    results = []
    for rec, imgs, meta_imgs, err in prepared:
        rows = model_rows[rec["code"]]
        used_types = [im["type"] for im in meta_imgs]
        ev = {
            "has_primary": "Primary" in used_types,
            "has_thumb": "Thumb" in used_types,
            "has_backdrop": "Backdrop" in used_types,
            "frames": [im["type"] for im in meta_imgs if str(im["type"]).startswith("frame_")],
            "missing_notes": [],
        }
        if not ev["has_thumb"]:
            ev["missing_notes"].append("缺 Thumb，已跳过")
        if "frame_900s" not in ev["frames"]:
            ev["missing_notes"].append("未抽到 900s 帧")
        if "frame_mid" not in ev["frames"] and "frame_early" not in ev["frames"]:
            ev["missing_notes"].append("未抽到任何片源帧" if not ev["frames"] else "未抽到中后段帧")
        primary_models = [m for m in models if m != GROK_MODEL]
        item_out = {
            "code": rec["code"], "emby_id": rec.get("emby_id"), "actress": actresses_of(rec),
            "actresses": rec.get("actresses") or [], "title": rec.get("title") or rec.get("emby_name"),
            "source": "new_scan", "images_used": meta_imgs,
            "ffmpeg": {k: rec.get("ffmpeg", {}).get(k) for k in ("ok_probe", "duration", "seek", "frames", "errors", "probe")},
            "evidence": ev, "models": rows,
            "agreement": agreement(rows, primary_models) if rows else "无结果",
            "consensus": consensus(rows, primary_models) if rows else "uncertain",
            "verdicts": {m: rows.get(m, {}).get("verdict") for m in models},
        }
        results.append(item_out)
    return results


def hits_text(hits) -> str:
    if not hits:
        return "无"
    if isinstance(hits, list):
        return ",".join(str(x) for x in hits) or "无"
    return str(hits)


def cell(rec: dict, model: str) -> str:
    m = (rec.get("models") or {}).get(model) or {}
    v = m.get("verdict") or "uncertain"
    conf = m.get("confidence") or ""
    reason = (m.get("reason") or m.get("error") or "").replace("\n", " ").replace("|", "\\|")
    tail = f" / {conf}" if conf else ""
    return f"{v}{tail} / 命中:{hits_text(m.get('hits'))} / {reason[:180]}"


def images_cell(rec: dict) -> str:
    parts = [f"{im.get('type')}({im.get('bytes')}B)" for im in rec.get("images_used") or []]
    extra = "；".join((rec.get("evidence") or {}).get("missing_notes") or [])
    s = "、".join(parts) if parts else "无图"
    if extra:
        s += "；" + extra
    return s


def annotate_timeouts(rec: dict) -> None:
    ff = rec.setdefault("ffmpeg", {})
    notes = list(ff.get("timeout_notes") or [])
    for err in ff.get("errors") or []:
        if "900s" in str(err) or "frame_900s" in str(err):
            notes.append("900s 抽帧失败或超时（已降级，保留已有封面/预览/180s）")
        elif "frame_mid" in str(err):
            notes.append("中后段抽帧失败或超时（已降级）")
        elif "180s" in str(err) or "frame_180s" in str(err) or "frame_early" in str(err):
            notes.append("180s/前段抽帧失败或超时")
    if ff.get("ok_probe") is False:
        notes.append("ffprobe 失败或超时，未能按片长抽帧")
    seen, out = set(), []
    for n in notes:
        if n not in seen:
            seen.add(n); out.append(n)
    ff["timeout_notes"] = out
    ev = rec.setdefault("evidence", {})
    ev["missing_notes"] = list(ev.get("missing_notes") or []) + [n for n in out if n not in (ev.get("missing_notes") or [])]


def summarize(items: list[dict]) -> dict:
    ap = af = au = dg = pt = 0
    fail_codes, review, disagree_detail = [], [], []
    for x in items:
        c = x.get("consensus")
        if c == "pass":
            ap += 1
        elif c == "fail":
            af += 1
            fail_codes.append(x["code"])
        elif c == "uncertain":
            au += 1
            review.append(x["code"])
        elif c == "disagree":
            dg += 1
            review.append(x["code"])
            disagree_detail.append({
                "code": x["code"], "actress": x.get("actress"), "verdicts": x.get("verdicts"),
                "deepseek_reason": ((x.get("models") or {}).get("deepseek/deepseek-flash") or {}).get("reason"),
                "luna_reason": ((x.get("models") or {}).get("chatgpt-api/gpt-6-luna") or {}).get("reason"),
            })
        else:
            pt += 1
            review.append(x["code"])
    return {
        "n_new": len(items), "new_agree_pass": ap, "new_agree_fail": af,
        "new_agree_uncertain": au, "new_disagree": dg, "new_partial": pt,
        "fail_codes": fail_codes, "review_codes": review, "disagree_detail": disagree_detail,
    }


def write_md(doc: dict, path: Path, models: list[str]):
    lines = [f"# Emby AV 识图复查 — {doc.get('title') or 'av_review'}", "", f"生成时间：{doc['generated_at']}",
             f"扫描窗口：{doc.get('scan_started_at') or ''} – {doc.get('scan_ended_at') or ''}。", ""]
    lines.append("未删库、未改媒体原片、**未改** `media_llm_route.json`。默认 DeepSeek-flash + gpt-6-luna；未默认 MiniMax / Grok。")
    lines.append("图只在内网取，交给模型的是 data URI，不是局域网 URL。临时帧用后删除。")
    lines.append("")
    s = doc["summary"]
    lines += ["## 摘要", "",
              f"- 本轮新扫 {s['n_new']} 条：两家一致 pass {s['new_agree_pass']}，一致 fail {s['new_agree_fail']}，一致 uncertain {s['new_agree_uncertain']}，分歧 {s['new_disagree']}，不完全可比 {s['new_partial']}。",
              f"- 明确 fail：{('、'.join(s['fail_codes']) if s['fail_codes'] else '无')}。",
              f"- 分歧或需人看：{('、'.join(s['review_codes']) if s['review_codes'] else '无')}。", ""]
    lines += ["## 分歧表", ""]
    disag = [x for x in doc["items"] if x.get("consensus") == "disagree"]
    if not disag:
        lines += ["（无）", ""]
    else:
        ds, lu = "deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna"
        lines += ["| 番号 | 演员 | deepseek-flash | gpt-6-luna | 一致/分歧 |", "|---|---|---|---|---|"]
        for rec in disag:
            lines.append(f"| {rec['code']} | {(rec.get('actress') or '').replace('|','\\|')} | {cell(rec, ds)} | {cell(rec, lu)} | {rec.get('agreement') or ''} |")
        lines.append("")
    lines += ["## 完整对照表", "", "| 番号 | 演员 | deepseek-flash | gpt-6-luna | 一致/分歧 | 用图 |", "|---|---|---|---|---|---|"]
    ds, lu = "deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna"
    for rec in doc["items"]:
        lines.append(f"| {rec['code']} | {(rec.get('actress') or '').replace('|','\\|')} | {cell(rec, ds)} | {cell(rec, lu)} | {rec.get('agreement') or ''} | {images_cell(rec)} |")
    lines += ["", "## Token / 费用", "", (doc.get("cost") or {}).get("markdown") or "（无）", ""]
    rt = doc.get("route_check") or {}
    lines += ["## 路由文件核对", "", f"- 扫前：`{rt.get('before')}`", f"- 扫后：`{rt.get('after')}`", f"- 未变化：{rt.get('unchanged')}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def cleanup_temps():
    for p in [IMG, FR, WORK / "probes"]:
        if p.exists():
            for f in p.glob("*"):
                try:
                    f.unlink()
                except Exception:
                    pass
    for name in ("jobs_probe.jsonl", "jobs_frames.jsonl", "frame_jobs.jsonl", "jobs.jsonl", "probes.tgz", "frames.tgz"):
        f = WORK / name
        if f.exists():
            try:
                f.unlink()
            except Exception:
                pass
    try:
        ssh(
            "docker exec embyserver rm -rf /tmp/av-review-frames; "
            f"rm -rf {UNRAID_WORK}/frames {UNRAID_WORK}/probes {UNRAID_WORK}/logs "
            f"{UNRAID_WORK}/*.tgz {UNRAID_WORK}/jobs*.jsonl",
            timeout=60,
        )
    except Exception as e:
        print("cleanup remote warn", e)


def maybe_grok(results: list[dict], system: str):
    """Only disagreements; explicit flag. Does not change media_llm_route."""
    for rec in results:
        if rec.get("consensus") != "disagree":
            continue
        # reuse images on disk if still present; otherwise skip
        imgs = collect_images(rec["code"], rec.get("image_files") or [])
        if not imgs:
            rec.setdefault("models", {})[GROK_MODEL] = {"ok": False, "verdict": "uncertain", "reason": "无图可复审"}
            continue
        ret = chat_with_retry(GROK_MODEL, imgs, rec, system)
        rec["models"][GROK_MODEL] = ret
        rec["verdicts"][GROK_MODEL] = ret.get("verdict")
        rec["grok_review"] = {"verdict": ret.get("verdict"), "reason": ret.get("reason"), "hits": ret.get("hits")}


def run_review(cfg: ReviewCfg) -> dict:
    global EMBY_KEY, MAGPIE_KEY, MODELS
    plan = plan_review(codes=cfg.codes, from_json=cfg.from_json, skip_scanned=cfg.skip_scanned, limit=cfg.limit)
    if cfg.dry_run or cfg.limit == 0:
        return {"dry_run": True, "plan": plan, "would_call_magpie": False, "would_ffmpeg": False,
                "generated_at": now_iso(), "note": "dry-run / limit=0：只出计划，不取封面、不抽帧、不调 Magpie。"}
    todo = plan["todo"]
    if not todo:
        return {"dry_run": False, "plan": plan, "items": [], "summary": summarize([]), "generated_at": now_iso(),
                "note": "没有需要新扫的条目（均已扫过或已跳过）。"}

    if str(WORK).startswith("/tmp/rest-vision"):
        raise RuntimeError("refusing to use rest-vision work dir")

    for d in (IMG, FR, OUT, WORK / "probes", WORK / "logs"):
        d.mkdir(parents=True, exist_ok=True)

    refresh_secrets(need_emby=True, need_magpie=True)
    EMBY_KEY = load_emby_key()
    MAGPIE_KEY = load_magpie_key()
    MODELS = list(cfg.models)
    system = PROVEN_SYSTEM

    before = route_stat()
    print("ROUTE BEFORE", before["raw"].replace("\n", " | "), flush=True)
    if before["md5"] != EXPECTED_ROUTE_MD5:
        print("WARNING route md5 changed before start:", before["md5"], flush=True)

    started = now_iso()
    started_ts = datetime.now(TZ)
    stamp = now_stamp()
    out_name = f"av_review_vision_{stamp}"
    assert_safe_basename(out_name + ".json")
    out_json = OUT / f"{out_name}.json"
    out_md = OUT / f"{out_name}.md"
    results = []

    for i in range(0, len(todo), cfg.chunk_size):
        chunk = todo[i:i + cfg.chunk_size]
        print(f"==== chunk {i//cfg.chunk_size+1} codes={[x['code'] for x in chunk]} ====", flush=True)
        by_code = fetch_meta_and_images(chunk)
        run_ffmpeg(by_code)
        ordered = [by_code[x["code"]] for x in chunk if x["code"] in by_code]
        new_results = judge_all(ordered, MODELS, system, cfg.max_workers)
        for rec in new_results:
            annotate_timeouts(rec)
        if cfg.grok_review:
            maybe_grok(new_results, system)
        results.extend(new_results)
        cleanup_temps()

    usage_local = OUT / "usage_after.jsonl"
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

    after = route_stat()
    unchanged = after["md5"] == before["md5"] and after["mtime_size"] == before["mtime_size"]
    doc = {
        "title": "Emby 已入库 AV 识图复查（DeepSeek-flash + gpt-6-luna）",
        "generated_at": now_iso(),
        "scan_started_at": started,
        "scan_ended_at": now_iso(),
        "plan": {k: v for k, v in plan.items() if k != "todo_if_unlimited"},
        "scope": {"n_new": len(results), "codes": [x["code"] for x in results], "models_used": MODELS,
                  "models_forbidden": ["minimax-cn/*", "MiniMax"], "grok_review": cfg.grok_review},
        "route_check": {"before": before["raw"].strip(), "after": after["raw"].strip(),
                        "md5_before": before["md5"], "md5_after": after["md5"], "unchanged": unchanged},
        "summary": summarize(results),
        "cost": cost,
        "items": results,
        "notes": [
            "未改 media_llm_route.json；请求级临时指定模型。",
            "未使用 MiniMax；Grok 仅在 --grok-review 时对分歧调用。",
            "未删库、未改原片；临时封面/抽帧用后已删。未自动交磁。",
            f"结果文件 {out_name}.json，未覆盖 mid/low/pilot/rest/grok 历史文件。",
        ],
    }
    out_json.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    write_md(doc, out_md, MODELS)
    publish_pair(out_json, out_md)
    doc["written"] = {"json": str(out_json), "md": str(out_md), "profile": f"{PROFILE_REMOTE}/{out_json.name}"}
    print("DONE n=", len(results), "summary=", doc["summary"], "route_unchanged=", unchanged, flush=True)
    return doc
