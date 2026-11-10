from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from av_review.constants import TZ


def is_peak(ts: datetime) -> bool:
    local = ts.astimezone(TZ)
    if local.weekday() >= 5:
        return False
    hm = local.hour * 60 + local.minute + local.second / 60
    return (9 * 60 <= hm < 12 * 60) or (14 * 60 <= hm < 18 * 60)


def compute_cost(usage_path: Path, window_start: datetime, window_end: datetime, models: list[str] | None = None) -> dict:
    """Same peak/off-peak accounting as the mid/low/rest scans.

    DeepSeek official: peak uncached ¥2 / cache ¥0.04 / out ¥8 per 1M; off-peak half.
    luna catalog estimate: $0.10 / $0.50 per 1M. reasoning already included in out.
    """
    rows = []
    if not usage_path.exists():
        return {"ok": False, "error": "usage.jsonl missing", "markdown": "usage.jsonl 缺失，未能核算。"}
    start_utc = window_start.astimezone(timezone.utc) - timedelta(seconds=5)
    end_utc = window_end.astimezone(timezone.utc) + timedelta(seconds=30)
    for line in usage_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        req = o.get("req") or ""
        model = o.get("model") or ""
        provider = o.get("provider") or ""
        is_ds = req == "deepseek/deepseek-flash" or (model == "deepseek-flash" and provider == "deepseek")
        is_lu = req == "chatgpt-api/gpt-6-luna" or model == "gpt-6-luna"
        is_gk = req == "cursor/grok-4.7" or "grok-4.7" in str(model)
        if not (is_ds or is_lu or is_gk):
            continue
        t = o.get("t") or ""
        try:
            td = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            continue
        if td < start_utc or td > end_utc:
            continue
        kind = "deepseek" if is_ds else ("luna" if is_lu else "grok")
        rows.append({
            "t": t,
            "t_shanghai": td.astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S") + " (Asia/Shanghai)",
            "req": req, "model": model, "provider": provider,
            "status": o.get("status"),
            "in": int(o.get("in") or 0),
            "out": int(o.get("out") or 0),
            "cache_read": int(o.get("cache_read") or 0),
            "cache_write": int(o.get("cache_write") or 0),
            "reasoning": int(o.get("reasoning") or 0),
            "peak": is_peak(td),
            "kind": kind,
            "ms": o.get("ms"),
        })

    def agg(kind, peak=None):
        xs = [r for r in rows if r["kind"] == kind and (peak is None or r["peak"] == peak)]
        return {
            "n": len(xs),
            "in_uncached": sum(r["in"] for r in xs),
            "cache_read": sum(r["cache_read"] for r in xs),
            "cache_write": sum(r["cache_write"] for r in xs),
            "out": sum(r["out"] for r in xs),
            "reasoning": sum(r["reasoning"] for r in xs),
            "http_200": sum(1 for r in xs if r.get("status") == 200),
            "non_200": sum(1 for r in xs if r.get("status") != 200),
        }

    def ds_cny(a, peak: bool) -> dict:
        mul = 1.0 if peak else 0.5
        unc = a["in_uncached"] * 2 * mul / 1e6
        cache = a["cache_read"] * 0.04 * mul / 1e6
        out = a["out"] * 8 * mul / 1e6
        return {"cny_uncached_in": unc, "cny_cache": cache, "cny_out": out, "cny_total": unc + cache + out}

    ds_all, ds_peak, ds_off = agg("deepseek"), agg("deepseek", True), agg("deepseek", False)
    lu, gk = agg("luna"), agg("grok")
    p_peak, p_off = ds_cny(ds_peak, True), ds_cny(ds_off, False)
    ds_total = p_peak["cny_total"] + p_off["cny_total"]
    lu_in = lu["in_uncached"] * 0.10 / 1e6
    lu_out = lu["out"] * 0.50 / 1e6
    lu_total = lu_in + lu_out

    def short(s):
        return s.replace(" (Asia/Shanghai)", "")
    tmin = min((r["t_shanghai"] for r in rows), default="")
    tmax = max((r["t_shanghai"] for r in rows), default="")
    window_txt = f"{short(tmin)}–{short(tmax)} (Asia/Shanghai)" if tmin else "（无调用记录）"

    md = []
    md.append(
        f"来源：Magpie `usage.jsonl`（窗口 {window_txt}；过滤时起点向前 5 秒、终点向后 30 秒容差）。"
        "网关记录无费用字段，下表为 token × 目录/官方单价。DeepSeek 按**每条调用时间戳**拆高峰/谷时后合计。不为算账重跑。"
    )
    md.append("")
    md.append("**deepseek-flash**（官方价高峰：缓存未命中输入 ¥2 / 百万、缓存命中输入 ¥0.04 / 百万、输出 ¥8 / 百万；谷时为高峰一半；人民币）：")
    md.append("")
    md.append(f"- 调用 {ds_all['n']} 次（HTTP 200：{ds_all['http_200']}；非 200：{ds_all['non_200']}）。其中高峰 {ds_peak['n']} 次，谷时 {ds_off['n']} 次。")
    md.append(f"- 合计：输入未命中 {ds_all['in_uncached']}、缓存命中 {ds_all['cache_read']}、输出 {ds_all['out']}（思考 token {ds_all['reasoning']} 已包含在输出中，未另加）。")
    if ds_off["n"]:
        md.append(
            f"- 谷时 {ds_off['n']} 次：未命中 {ds_off['in_uncached']}、缓存 {ds_off['cache_read']}、输出 {ds_off['out']} → "
            f"¥{p_off['cny_uncached_in']:.6f} + ¥{p_off['cny_cache']:.6f} + ¥{p_off['cny_out']:.6f} = **¥{p_off['cny_total']:.4f}**。"
        )
    if ds_peak["n"]:
        md.append(
            f"- 高峰 {ds_peak['n']} 次：未命中 {ds_peak['in_uncached']}、缓存 {ds_peak['cache_read']}、输出 {ds_peak['out']} → "
            f"¥{p_peak['cny_uncached_in']:.6f} + ¥{p_peak['cny_cache']:.6f} + ¥{p_peak['cny_out']:.6f} = **¥{p_peak['cny_total']:.4f}**。"
        )
    md.append(f"- DeepSeek 合计：**¥{ds_total:.4f}**。")
    md.append("")
    md.append("**chatgpt-api/gpt-6-luna**（Magpie 目录价 input $0.10 / output $0.50 per 1M，走 ChatGPT 登录，不是官方 Platform 账单，标「估算」）：")
    md.append("")
    md.append(f"- 调用 {lu['n']} 次（HTTP 200：{lu['http_200']}；非 200：{lu['non_200']}）。")
    md.append(f"- 输入 {lu['in_uncached']}、输出 {lu['out']}、缓存 {lu['cache_read']}。")
    md.append(f"- 估算费用：**${lu_total:.4f}**（输入 ${lu_in:.4f} + 输出 ${lu_out:.4f}）。")
    md.append("")
    if gk["n"]:
        md.append(f"**cursor/grok-4.7**（Cursor 登录配额，无人民币价，只记 token）：调用 {gk['n']} 次，输入 {gk['in_uncached']}、缓存 {gk['cache_read']}、输出 {gk['out']}。")
        md.append("")
    md.append(f"两家合计：DeepSeek **¥{ds_total:.4f}** + luna 约 ${lu_total:.4f}。")
    return {
        "ok": True,
        "window_local": window_txt,
        "deepseek_all": ds_all,
        "deepseek_peak": {**ds_peak, **{k: round(v, 6) if isinstance(v, float) else v for k, v in p_peak.items()}},
        "deepseek_offpeak": {**ds_off, **{k: round(v, 6) if isinstance(v, float) else v for k, v in p_off.items()}},
        "deepseek_cny_total": round(ds_total, 4),
        "luna": {**lu, "usd_in": round(lu_in, 6), "usd_out": round(lu_out, 6), "usd_total_est": round(lu_total, 4)},
        "grok": gk,
        "markdown": "\n".join(md),
        "n_rows": len(rows),
    }
