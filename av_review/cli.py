from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from av_review import __version__
from av_review.busy import vision_busy
from av_review.constants import API_HOST_DEFAULT, API_PORT_DEFAULT, HOME
from av_review.util import now_iso


def _cmd_help_epilog() -> str:
    return """
示例（均在 grokbot-vm 上执行）：

  # 查用法
  /home/yuanhao/av-review/bin/av_review --help
  /home/yuanhao/av-review/bin/av_review review --help

  # 复查一批：只出计划，不识图、不抽帧
  /home/yuanhao/av-review/bin/av_review review --dry-run
  /home/yuanhao/av-review/bin/av_review review --limit 0

  # 复查指定番号（真正识图；勿与正在跑的 rest 扫抢配额）
  /home/yuanhao/av-review/bin/av_review review --codes IPX-750,MIDV-786 --skip-scanned

  # 从 JSON 增量扫尚未识图的条目（默认跳过 mid/low/pilot/rest/grok 已有结果）
  /home/yuanhao/av-review/bin/av_review review --from-json /path/to/codes.json --limit 16

  # 库外候选：只出短名单，默认不判断、不交磁
  /home/yuanhao/av-review/bin/av_review catalog
  /home/yuanhao/av-review/bin/av_review catalog --dry-run

  # 文本判断（等识图空档再开；仍不交磁）
  /home/yuanhao/av-review/bin/av_review catalog --judge

  # 每日增量（cron 默认 19:30 Asia/Shanghai）：先复查新入库，再出库外短名单
  /home/yuanhao/av-review/bin/av_review daily --skip-if-busy

  # 内网 API
  /home/yuanhao/av-review/bin/av_review api --host 0.0.0.0 --port 18765
""".strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="av_review",
        description="findav 正式入口：Emby 已入库 AV 识图复查 + 库外候选短名单。不改 media_llm_route，不自动交磁。",
        epilog=_cmd_help_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"av_review {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common_review(sp):
        sp.add_argument("--codes", help="逗号分隔番号，只扫这些")
        sp.add_argument("--from-json", type=Path, help="从 JSON 读取条目（items[].code / emby_id）")
        sp.add_argument("--skip-scanned", action=argparse.BooleanOptionalAction, default=True,
                        help="跳过已有 mid/low/pilot/rest/grok/av_review_vision 结果（默认开）")
        sp.add_argument("--limit", type=int, default=None, help="最多扫 N 条；0 表示只出计划、不识图")
        sp.add_argument("--dry-run", action="store_true", help="只出计划，不取图、不抽帧、不调 Magpie")
        sp.add_argument("--grok-review", action="store_true", default=False,
                        help="仅对两家分歧再调 cursor/grok-4.7；默认关，daily 不会打开")

    pr = sub.add_parser("review", help="Emby 已入库 AV 识图复查（默认 DeepSeek-flash + gpt-6-luna）",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_review(pr)

    pc = sub.add_parser("catalog", help="findav 库 vs Emby：尚未入库的短名单（默认不判断、不交磁）")
    pc.add_argument("--judge", action="store_true", default=False, help="调用 Magpie 文本判断；默认关")
    pc.add_argument("--no-judge", action="store_true", help="显式关闭判断（默认就是关）")
    pc.add_argument("--dry-run", action="store_true", help="只打印摘要，不写 profile")
    pc.add_argument("--limit", type=int, default=None, help="--judge 时最多判断 N 条")
    pc.add_argument("--no-strm", action="store_true", help="不补充本地 .strm 文件名")

    pd = sub.add_parser("daily", help="定时入口：增量复查 + 库外短名单（默认 19:30 由 cron 拉起）")
    add_common_review(pd)
    pd.add_argument("--skip-if-busy", action=argparse.BooleanOptionalAction, default=True,
                    help="若已有识图进程在跑，则跳过识图与 --judge（默认开）")
    pd.add_argument("--judge", action="store_true", default=False, help="空档时顺带做库外文本判断；默认关")

    pa = sub.add_parser("api", help="在本机起局域网 HTTP API（健康检查 + 触发）")
    pa.add_argument("--host", default=API_HOST_DEFAULT)
    pa.add_argument("--port", type=int, default=API_PORT_DEFAULT)

    sub.add_parser("health", help="打印健康状态（进程/路由/忙闲），不调模型")
    sub.add_parser("status", help="同 health")
    return p


def _parse_codes(s: str | None) -> list[str] | None:
    if not s:
        return None
    return [x.strip() for x in s.split(",") if x.strip()]


def _dump(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return 0


def _health() -> dict:
    from av_review.util import ssh
    from av_review.constants import EXPECTED_ROUTE_MD5, EXPECTED_ROUTE_MTIME_PREFIX, ROUTE_REMOTE
    busy = vision_busy()
    try:
        r = ssh(f'stat -c "%y %s" {ROUTE_REMOTE} && md5sum {ROUTE_REMOTE}', timeout=15, check=True)
        route = r.stdout.strip()
        md5 = r.stdout.strip().splitlines()[-1].split()[0]
        mtime_ok = EXPECTED_ROUTE_MTIME_PREFIX in route
        md5_ok = md5 == EXPECTED_ROUTE_MD5
    except Exception as e:
        route, md5, mtime_ok, md5_ok = f"error: {type(e).__name__}", "", False, False
    return {
        "ok": True,
        "generated_at": now_iso(),
        "host_role": "grokbot-vm formal av_review entry",
        "vision_busy": busy,
        "route": {"raw": route, "md5": md5, "mtime_expected_prefix": EXPECTED_ROUTE_MTIME_PREFIX,
                  "mtime_ok": mtime_ok, "md5_ok": md5_ok, "tool_never_writes_route": True},
        "paths": {"home": str(HOME), "bin": str(HOME / "bin" / "av_review")},
    }


def cmd_review(args) -> int:
    from av_review.vision import ReviewCfg, run_review
    cfg = ReviewCfg(
        codes=_parse_codes(getattr(args, "codes", None)),
        from_json=getattr(args, "from_json", None),
        skip_scanned=getattr(args, "skip_scanned", True),
        limit=getattr(args, "limit", None),
        dry_run=getattr(args, "dry_run", False),
        grok_review=getattr(args, "grok_review", False),
    )
    return _dump(run_review(cfg))


def cmd_catalog(args) -> int:
    from av_review.busy import vision_busy
    from av_review.catalog import run_catalog, run_catalog_judge
    judge = bool(getattr(args, "judge", False)) and not bool(getattr(args, "no_judge", False))
    if judge:
        busy = vision_busy()
        if busy.get("busy"):
            print("拒绝 --judge：当前有识图进程在跑，避免抢配额。请等空档再开。", file=sys.stderr)
            return _dump({"ok": False, "reason": "vision_busy", "busy": busy})
        doc = run_catalog_judge(limit=args.limit)
        j = doc.get("judge") or {}
        s = doc.get("summary") or {}
        return _dump({
            "ok": True,
            "generated_at": doc.get("generated_at"),
            "written": doc.get("written"),
            "summary": {
                "missing": s.get("missing"),
                "tier_keep": s.get("tier_keep"),
                "tier_review": s.get("tier_review"),
                "tier_drop": s.get("tier_drop"),
                "agree_pass": s.get("agree_pass"),
                "agree_fail": s.get("agree_fail"),
                "agree_uncertain": s.get("agree_uncertain"),
                "disagree": s.get("disagree"),
            },
            "judge": {k: j.get(k) for k in (
                "started_at","ended_at","models","n_todo","n_resumed","n_done","n_failed",
                "n_skipped","n_pending","done_priority","done_review","done_other"
            )},
            "cost": {k: (doc.get("cost") or {}).get(k) for k in (
                "ok","window_local","deepseek_cny_total","markdown"
            ) if k != "markdown"} | {"markdown": (doc.get("cost") or {}).get("markdown")},
            "notes": doc.get("notes"),
        })
    doc = run_catalog(judge=False, dry_run=bool(args.dry_run), limit=args.limit, include_strm=not args.no_strm)
    # compact stdout: drop full missing list to keep terminal small? still useful. Print summary + written.
    out = {k: doc[k] for k in doc if k != "missing"}
    out["missing_preview"] = [
        {"code": x["code"], "actress": x.get("actress"), "beauty_score": x.get("beauty_score"),
         "erotic_level": x.get("erotic_level"), "prefilter": x.get("prefilter")}
        for x in (doc.get("missing") or [])[:30]
    ]
    out["n_missing"] = len(doc.get("missing") or [])
    return _dump(out)


def cmd_daily(args) -> int:
    from av_review.catalog import run_catalog, run_catalog_judge
    from av_review.vision import ReviewCfg, run_review
    busy = vision_busy()
    skip_heavy = bool(getattr(args, "skip_if_busy", True)) and busy.get("busy")
    report = {"generated_at": now_iso(), "busy": busy, "skipped_heavy": skip_heavy, "review": None, "catalog": None}
    if skip_heavy:
        report["review"] = {"skipped": True, "reason": "vision_busy; daily will retry tomorrow or after the current scan"}
        report["catalog"] = run_catalog(judge=False, dry_run=bool(args.dry_run), include_strm=True)
        report["catalog_note"] = ("dry-run：只估算短名单，未写盘，未跑 --judge（识图仍在进行）。" if args.dry_run else "已出待判断短名单，未跑 --judge（识图仍在进行）。")
    else:
        cfg = ReviewCfg(
            codes=_parse_codes(getattr(args, "codes", None)),
            from_json=getattr(args, "from_json", None),
            skip_scanned=getattr(args, "skip_scanned", True),
            limit=0 if args.dry_run else getattr(args, "limit", None),
            dry_run=bool(args.dry_run),
            grok_review=False,  # daily never auto-grok
        )
        report["review"] = run_review(cfg)
        if getattr(args, "judge", False) and not args.dry_run:
            report["catalog"] = run_catalog_judge(limit=args.limit)
        else:
            report["catalog"] = run_catalog(judge=False, dry_run=bool(args.dry_run), include_strm=True)
    # shrink catalog in stdout
    cat = report.get("catalog") or {}
    if "missing" in cat:
        cat = {k: v for k, v in cat.items() if k != "missing"}
        cat["n_missing"] = report["catalog"]["summary"]["missing"] if report.get("catalog", {}).get("summary") else None
        report["catalog"] = cat
    return _dump(report)


def cmd_api(args) -> int:
    from av_review.api import serve
    serve(args.host, args.port)
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd in ("health", "status"):
        return _dump(_health())
    if args.cmd == "review":
        return cmd_review(args)
    if args.cmd == "catalog":
        return cmd_catalog(args)
    if args.cmd == "daily":
        return cmd_daily(args)
    if args.cmd == "api":
        return cmd_api(args)
    parser.print_help()
    return 2
