"""Full-library scan: skips, resume, stuck titles, URI hiding, 406 and risk stops.

These tests never call 115, MariaDB, Magpie, ffmpeg, or a real host.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402
import library_scan  # noqa: E402

GIB = 1024 ** 3
URI = "ed2k://|file|ABP-108 secret $(id)|42|0123456789abcdef0123456789abcdef|/"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=name"
TZ = library_scan.TZ


def _run_main(argv):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = av_auto.main(list(argv))
        except SystemExit as exc:
            return exc.code, out.getvalue(), err.getvalue()
    return rc, out.getvalue(), err.getvalue()


def _corpus():
    return {
        "titles": [
            {"code": "ABP-073", "user_verdict": "pending", "erotic_level": "high"},
            {"code": "ABP-108", "user_verdict": "pending", "erotic_level": None},
            {"code": "ABP-246", "user_verdict": "pending", "erotic_level": "low"},
            {"code": "IPX-668", "user_verdict": "skip", "erotic_level": "high"},
            {"code": "XV-165", "user_verdict": "skip", "erotic_level": None},
            {"code": "ZZ-001", "user_verdict": "pending", "erotic_level": None},
            {"code": "HPET-022", "user_verdict": "pending", "erotic_level": "mid"},
            {"code": "MFYD-175", "user_verdict": "pending", "erotic_level": None},
            {"code": "DONE-001", "user_verdict": "pending", "erotic_level": "high"},
            {"code": "DSOD-106", "user_verdict": "pending", "erotic_level": None},
            {"code": "ABP-950", "user_verdict": "pending", "erotic_level": "high"},
        ],
        "links": {
            "ABP-073": {"ed2k": [URI], "magnet": [MAGNET]},
            "ABP-108": {"ed2k": [], "magnet": [MAGNET]},
            "ABP-246": {"ed2k": ["ed2k://|file|later|1|aa|/",], "magnet": []},
            "IPX-668": {"ed2k": ["ed2k://|file|skip|1|aa|/"], "magnet": []},
            "XV-165": {"ed2k": ["ed2k://|file|skip2|1|aa|/"], "magnet": []},
            "HPET-022": {"ed2k": ["ed2k://|file|hard|1|aa|/"], "magnet": []},
            "MFYD-175": {"ed2k": [], "magnet": [MAGNET]},
            "DONE-001": {"ed2k": ["ed2k://|file|done|1|aa|/"], "magnet": []},
            "DSOD-106": {"ed2k": ["ed2k://|file|partial|1|aa|/"], "magnet": []},
            "ABP-950": {"ed2k": ["ed2k://|file|erotic|1|aa|/"], "magnet": []},
        },
    }


class Deps:
    def __init__(self, root: Path):
        self.state_dir = root
        self.submit_gap_sec = 48
        self.backoff_sec = 180
        self.fail_retry_sec = 60
        self.stuck_after_sec = 7200
        self.max_passes = None
        self.clock = datetime(2026, 11, 11, 1, 0, tzinfo=TZ)
        self.calls = []
        self.files = {}
        self.task_map = {}
        self.tasks_result = {}
        self.disk = set()
        self.space_sample = {
            "cloud_remain": 20228 * GIB,
            "offline_surplus": 2457,
            "frames_free": 213 * GIB,
            "media_free": 213 * GIB,
            "cache_free": 213 * GIB,
        }
        self._submit = None

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.clock += timedelta(seconds=float(seconds))

    def load_corpus(self):
        return _corpus()

    def review_status(self, code):
        if code in self.disk:
            return "complete"
        if code == "DSOD-106":
            return "incomplete"
        if code == "DONE-001":
            return "complete"
        return "missing"

    def files_of(self, code):
        self.calls.append(("files", code))
        return self.files.get(code, {"action": "none", "strm": ""})

    def tasks(self):
        self.calls.append(("tasks",))
        if self.tasks_result is None:
            return None
        return dict(self.task_map)

    def submit(self, code, uri):
        self.calls.append(("submit", code, uri))
        if self._submit:
            return self._submit(code, uri)
        return {"class": "ok", "detail": ""}

    def process_title(self, code):
        self.calls.append(("process", code))
        self.disk.add(code)
        return 0

    def frames_title(self, code, strm):
        self.calls.append(("frames", code, strm))
        if code == "ABP-108":
            return 1
        self.disk.add(code)
        return 0

    def space(self):
        self.calls.append(("space",))
        return dict(self.space_sample)

    def cost_of(self, code):
        return {"deepseek_cny": 0.25, "luna_usd_est": 0.01}


def _args(**kwargs):
    base = {"codes": [], "dry_run": False, "status": False, "once": True}
    base.update(kwargs)
    return SimpleNamespace(**base)


def _names(deps: Deps, kind: str):
    return [item[1] for item in deps.calls if item[0] == kind]


class PureTests(unittest.TestCase):
    def test_live_schema_rows_build_ed2k_then_magnet_without_source_url(self):
        page = "http://example.test/not-a-download"
        ed2k_rows = [
            {
                "code": "ABP-073",
                "file_name": "ad.mp4",
                "size_bytes": 1000,
                "file_hash": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "aich_hash": "SHOULD_NOT_APPEAR",
                "source_url": page,
            },
            {
                "code": "ABP-073",
                "file_name": "ABP-073|feature.mp4",
                "size_bytes": "2000000000",
                "file_hash": "ABCDEF0123456789ABCDEF0123456789",
                "source_url": page,
            },
            {
                "code": "NONAME",
                "file_name": "",
                "size_bytes": 4096,
                "file_hash": "AbC",
                "source_url": page,
            },
            {"code": "NOHASH", "file_name": "x.mp4", "size_bytes": 5000, "file_hash": "", "source_url": page},
            {"code": "NOSIZE", "file_name": "x.mp4", "size_bytes": 0, "file_hash": "CC", "source_url": page},
            {"code": "NONEG", "file_name": "x.mp4", "size_bytes": None, "file_hash": "DD", "source_url": page},
            {"code": "  ", "file_name": "x.mp4", "size_bytes": 10, "file_hash": "EE", "source_url": page},
        ]
        built = library_scan.ed2k_uris_from_rows(ed2k_rows)
        self.assertEqual(
            built,
            [
                ("ABP-073", library_scan.ed2k_uri("ABP-073|feature.mp4", 2000000000, "ABCDEF0123456789ABCDEF0123456789")),
                ("NONAME", library_scan.ed2k_uri("", 4096, "AbC")),
                ("ABP-073", library_scan.ed2k_uri("ad.mp4", 1000, "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")),
            ],
        )
        self.assertEqual(
            built[0][1],
            "ed2k://|file|ABP-073_feature.mp4|2000000000|abcdef0123456789abcdef0123456789|/",
        )
        self.assertEqual(built[1][1], "ed2k://|file|AbC.mp4|4096|abc|/")
        joined = "\n".join(uri for _code, uri in built)
        self.assertNotIn(page, joined)
        self.assertNotIn("SHOULD_NOT_APPEAR", joined)
        self.assertEqual(library_scan.ed2k_uri("a|b\nc\r", 3, "FF"), "ed2k://|file|a_bc|3|ff|/")

        magnet_rows = [
            {"code": "ABP-246", "infohash": "0123456789abcdef0123456789ABCDEF", "name": "later", "source_url": page},
            {"code": "ABP-108", "infohash": "  AbCDEF0123456789ABCDEF0123456789  ", "tracker_hint": "nope"},
            {"code": "NOMAG", "infohash": "  ", "source_url": page},
            {"code": "", "infohash": "abcdef", "source_url": page},
        ]
        magnets = library_scan.magnet_uris_from_rows(magnet_rows)
        self.assertEqual(
            magnets,
            [
                ("ABP-108", "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789"),
                ("ABP-246", "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef"),
            ],
        )
        self.assertNotIn(page, "\n".join(uri for _code, uri in magnets))
        picked = library_scan.pick_link([item[1] for item in built if item[0] == "ABP-073"], [magnets[0][1]])
        self.assertEqual(picked["kind"], "ed2k")
        self.assertIn("ABP-073_feature.mp4", picked["uri"])

    def test_pick_link_prefers_ed2k_and_ignores_junk(self):
        picked = library_scan.pick_link([URI, "http://nope"], [MAGNET])
        self.assertEqual(picked["kind"], "ed2k")
        self.assertEqual(picked["uri"], URI)
        magnet_only = library_scan.pick_link([], [MAGNET])
        self.assertEqual(magnet_only["kind"], "magnet")
        self.assertIsNone(library_scan.pick_link(["not-a-link"], []))

    def test_dense_bucket_treats_zero_frames_as_incomplete(self):
        self.assertEqual(
            library_scan.dense_bucket(
                frames_ready=False,
                review_complete=False,
                needs_tiebreak=False,
                tiebreak_done=False,
                has_manifest=True,
                has_review_file=False,
            ),
            "incomplete",
        )
        self.assertEqual(
            library_scan.dense_bucket(
                frames_ready=True,
                review_complete=True,
                needs_tiebreak=False,
                tiebreak_done=False,
                has_manifest=True,
                has_review_file=True,
            ),
            "complete",
        )
        self.assertEqual(
            library_scan.dense_bucket(
                frames_ready=True,
                review_complete=True,
                needs_tiebreak=True,
                tiebreak_done=False,
                has_manifest=True,
                has_review_file=True,
            ),
            "incomplete",
        )

    def test_erotic_level_does_not_skip_and_counts_match(self):
        records = library_scan.build_records(_corpus(), lambda code: "complete" if code == "DONE-001" else ("incomplete" if code == "DSOD-106" else "missing"))
        counts = library_scan.counts_of(records)
        self.assertEqual(counts["total"], 11)
        self.assertEqual(counts["with_link"], 10)
        self.assertEqual(counts["no_link"], 1)
        self.assertEqual(counts["already_reviewed"], 1)
        self.assertEqual(counts["skip"], 2)
        self.assertEqual(counts["hard_skip"], 2)
        self.assertEqual(counts["remaining"], 5)
        self.assertEqual(counts["incomplete"], 1)
        by = {row["code"]: row for row in records}
        self.assertEqual(by["ABP-950"]["erotic_level"], "high")
        self.assertEqual(library_scan.disposition(by["ABP-950"]), "work")
        self.assertEqual(library_scan.disposition(by["IPX-668"]), "user_skip")
        self.assertEqual(library_scan.disposition(by["ZZ-001"]), "no_link")
        self.assertEqual(library_scan.disposition(by["HPET-022"]), "hard_skip")
        self.assertEqual(library_scan.disposition(by["DSOD-106"]), "work")

    def test_submit_signals_and_uri_fields_are_not_classified_as_text(self):
        self.assertEqual(library_scan.classify_result({"errno": 406, "url": URI, "state": False}), "backoff")
        self.assertNotIn(URI, library_scan.signal_from_result({"errno": 406, "url": URI, "message": "HTTP 406"}))
        self.assertEqual(library_scan.classify_submit_text("请完成验证码"), "block")
        self.assertEqual(library_scan.classify_submit_text("触发风控"), "block")
        self.assertEqual(library_scan.classify_submit_text("账号异常"), "block")
        self.assertEqual(library_scan.classify_submit_text("错误 911"), "block")
        self.assertEqual(library_scan.classify_submit_text("id 9110"), "")
        self.assertEqual(library_scan.classify_submit_text(URI + " 406"), "backoff")

    def test_large_cache_and_115_are_not_a_full_disk(self):
        report = library_scan.assess_space({
            "cloud_remain": 20228 * GIB,
            "offline_surplus": 2457,
            "frames_free": 213 * GIB,
            "media_free": 213 * GIB,
            "cache_free": 213 * GIB,
        })
        self.assertFalse(report["disk_full"])
        self.assertEqual(report["submit_blocked"], "")
        self.assertTrue(report["process_ok"])
        self.assertNotIn("这是磁盘满", report["message"])

        low_quota = library_scan.assess_space({
            "cloud_remain": 20228 * GIB,
            "offline_surplus": 4,
            "frames_free": 213 * GIB,
            "media_free": 213 * GIB,
            "cache_free": 213 * GIB,
        })
        self.assertEqual(low_quota["submit_blocked"], "quota")
        self.assertFalse(low_quota["disk_full"])
        self.assertTrue(low_quota["process_ok"])
        self.assertNotIn("这是磁盘满", low_quota["message"])

        tight_cloud = library_scan.assess_space({
            "cloud_remain": 15 * GIB,
            "offline_surplus": 100,
            "frames_free": 213 * GIB,
            "media_free": 213 * GIB,
            "cache_free": 213 * GIB,
        })
        self.assertEqual(tight_cloud["submit_blocked"], "115-space")
        self.assertFalse(tight_cloud["disk_full"])
        self.assertNotIn("这是磁盘满", tight_cloud["message"])

        full_frames = library_scan.assess_space({
            "cloud_remain": 20228 * GIB,
            "offline_surplus": 100,
            "frames_free": 200 * 1024 ** 2,
            "media_free": 213 * GIB,
            "cache_free": 213 * GIB,
        })
        self.assertTrue(full_frames["disk_full"])
        self.assertEqual(full_frames["disk_full_where"], "frames")
        self.assertFalse(full_frames["process_ok"])
        self.assertIn("这是磁盘满", full_frames["message"])

        unknown = library_scan.assess_space({})
        self.assertFalse(unknown["disk_full"])
        self.assertEqual(unknown["submit_blocked"], "")
        self.assertNotIn("这是磁盘满", unknown["message"])

    def test_quota_parser_keeps_unknown_as_unknown(self):
        parsed = library_scan.parse_quota({"state": True, "data": {"surplus": 2457, "total": 3000, "used": 543}})
        self.assertEqual(parsed["surplus"], 2457)
        self.assertIsNone(library_scan.parse_quota({"data": {}})["surplus"])
        remain = library_scan.parse_cloud_remain_bytes({"rt_space_info": {"all_remain": {"size": 20228 * GIB}}})
        self.assertEqual(remain, 20228 * GIB)

    def test_progress_scrub_refuses_raw_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            doc = library_scan.empty_progress()
            doc["failed"] = {"ABP-073": {"reason": URI, "uri": URI}}
            library_scan.save_progress(path, doc)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("ed2k://", text)
        self.assertNotIn(URI, text)
        self.assertNotIn('"uri"', text)
        self.assertIn("[link]", text)

    def test_uri_file_is_mode_600_and_gone_before_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}

            def invoke(code, loaded):
                seen["code"] = code
                seen["loaded"] = loaded
                seen["left"] = list(root.glob("av-auto-url.*"))
                return {"class": "ok"}

            mode, outcome = library_scan.invoke_uri_file("ABP-108", URI, invoke, root)
        self.assertEqual(mode, 0o600)
        self.assertEqual(seen["loaded"], URI)
        self.assertEqual(seen["left"], [])
        self.assertEqual(outcome["class"], "ok")
        self.assertEqual(list(root.glob("av-auto-url.*")), [])


class FlushLog(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class RunnerTests(unittest.TestCase):
    def _capture(self, deps, **kwargs):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = library_scan.execute(_args(**kwargs), deps)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_counts_skip_reviewed_user_skip_and_no_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = Deps(Path(tmp))
            rc, out, _err = self._capture(deps, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("total: 11", out)
        self.assertIn("with_link: 10", out)
        self.assertIn("already_reviewed: 1", out)
        self.assertIn("skip: 2", out)
        self.assertIn("no_link: 1", out)
        self.assertIn("remaining: 5", out)
        self.assertIn("SKIP IPX-668: user_verdict=skip", out)
        self.assertIn("SKIP XV-165: user_verdict=skip", out)
        self.assertIn("SKIP ZZ-001: 没有 ed2k/magnet", out)
        self.assertIn("SKIP DONE-001: 已有成片抽帧 review", out)
        self.assertIn("SKIP HPET-022: hard-skip", out)
        self.assertIn("不刮削、不 promote", out)
        self.assertNotIn("submit", [item[0] for item in deps.calls])
        self.assertNotIn("process", [item[0] for item in deps.calls])
        self.assertNotIn("frames", [item[0] for item in deps.calls])
        self.assertFalse((Path(tmp) / "progress.json").exists())
        self.assertNotIn(URI, out)
        self.assertNotIn("ed2k://", out)
        self.assertNotIn("magnet:", out)

    def test_stuck_download_does_not_block_a_later_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            old = deps.clock - timedelta(hours=5)
            progress = library_scan.empty_progress()
            progress["in_progress"]["ABF-397"] = {
                "percent": 0.14,
                "rate": 0,
                "name": "ABF-397_Restored.mp4",
                "seen_at": old.isoformat(),
                "unchanged_since": old.isoformat(),
                "bucket": "run",
                "stuck": False,
            }
            library_scan.save_progress(root / "progress.json", progress)
            deps.load_corpus = lambda: {
                "titles": [
                    {"code": "ABF-397", "user_verdict": "pending", "erotic_level": None},
                    {"code": "ABP-073", "user_verdict": "pending", "erotic_level": "high"},
                ],
                "links": {
                    "ABF-397": {"ed2k": [URI], "magnet": []},
                    "ABP-073": {"ed2k": [URI], "magnet": []},
                },
            }
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/av-auto/ABP-073/ABP-073.strm"}
            deps.task_map["ABF-397"] = {
                "bucket": "run",
                "name": "ABF-397_Restored.mp4",
                "percentDone": 0.14,
                "rate": 0,
            }
            rc, out, _err = self._capture(deps)
            saved = json.loads((root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(_names(deps, "frames"), ["ABP-073"])
        self.assertEqual(deps.calls[-1][0] != "submit", True)
        self.assertNotIn("submit", [item[0] for item in deps.calls])
        self.assertIn("ABF-397", saved["stuck"])
        self.assertNotIn("ABP-073", saved["stuck"])
        self.assertIn("ABP-073", saved["done"])
        self.assertIn("STUCK ABF-397", out)
        self.assertNotIn(URI, out)
        self.assertNotIn(URI, json.dumps(saved))

    def test_fresh_in_progress_title_also_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = Deps(Path(tmp))
            deps.load_corpus = lambda: {
                "titles": [
                    {"code": "ABF-397", "user_verdict": "pending", "erotic_level": None},
                    {"code": "ABP-073", "user_verdict": "pending", "erotic_level": None},
                ],
                "links": {
                    "ABF-397": {"ed2k": [URI], "magnet": []},
                    "ABP-073": {"ed2k": [URI], "magnet": []},
                },
            }
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/AV/ABP-073/ABP-073.strm"}
            deps.task_map["ABF-397"] = {"bucket": "run", "name": "ABF-397.mp4", "percentDone": 0.14, "rate": 0}
            rc, out, _err = self._capture(deps)
            saved = json.loads((Path(tmp) / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(_names(deps, "frames"), ["ABP-073"])
        self.assertIn("ABF-397", saved["in_progress"])
        self.assertNotIn("ABF-397", saved["stuck"])
        self.assertIn("不堵住后面的番号", out)

    def test_resume_skips_finished_titles_and_retries_failures_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = Deps(Path(tmp))
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/av-auto/ABP-073/ABP-073.strm"}
            deps.files["ABP-108"] = {"action": "frames", "strm": "/media/av-auto/ABP-108/ABP-108.strm"}
            rc, _out, _err = self._capture(deps)
            self.assertEqual(rc, 0)
            first_frames = _names(deps, "frames")
            self.assertEqual(first_frames, ["ABP-073", "ABP-108"])
            deps.calls.clear()
            rc, _out, _err = self._capture(deps)
            self.assertEqual(rc, 0)
            self.assertEqual(_names(deps, "frames"), [])
            deps.clock += timedelta(seconds=120)
            rc, _out, _err = self._capture(deps)
            saved = json.loads((Path(tmp) / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertEqual(_names(deps, "frames"), ["ABP-108"])
        self.assertIn("ABP-073", saved["done"])
        self.assertIn("queue", saved)
        self.assertIn("skipped_no_link", saved)
        self.assertIn("ZZ-001", saved["skipped_no_link"])
        self.assertIn("costs", saved)
        self.assertIn("current", saved)

    def test_process_or_frames_never_promotes_or_scrapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = Deps(Path(tmp))
            deps.load_corpus = lambda: {
                "titles": [
                    {"code": "ABP-073", "user_verdict": "pending", "erotic_level": "high"},
                    {"code": "ABP-950", "user_verdict": "pending", "erotic_level": "high"},
                ],
                "links": {
                    "ABP-073": {"ed2k": [URI], "magnet": []},
                    "ABP-950": {"ed2k": [URI], "magnet": []},
                },
            }
            deps.files["ABP-073"] = {"action": "process", "strm": ""}
            deps.files["ABP-950"] = {"action": "frames", "strm": "/media/av-auto/ABP-950/ABP-950.strm"}
            rc, out, _err = self._capture(deps)
        self.assertEqual(rc, 0, out)
        kinds = [item[0] for item in deps.calls]
        self.assertIn("process", kinds)
        self.assertIn("frames", kinds)
        self.assertNotIn("promote", kinds)
        self.assertNotIn("scrape", kinds)
        self.assertNotIn("submit", kinds)
        self.assertIn("不刮削、不 promote", out)
        source = (ROOT / "scripts" / "library_scan.py").read_text(encoding="utf-8")
        self.assertNotIn("cmd_promote(", source)
        self.assertNotIn("_emby_refresh(", source)
        self.assertNotIn("verify5", source)
        self.assertNotIn("av-auto-506", source)

    def test_uri_does_not_land_in_logs_or_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            seen = []

            def perform(code, loaded):
                seen.append(loaded)
                during = list(root.glob("av-auto-url.*"))
                self.assertEqual(during, [])
                return {"rc": 0, "result": {"state": True, "errno": 0, "message": loaded, "url": loaded}, "dest": "/av-auto/_staging/" + code}

            deps.submit = library_scan.make_submit(perform, root)
            deps.load_corpus = lambda: {
                "titles": [{"code": "ABP-108", "user_verdict": "pending", "erotic_level": "high"}],
                "links": {"ABP-108": {"ed2k": [URI], "magnet": [MAGNET]}},
            }
            rc, out, err = self._capture(deps)
            text = (root / "progress.json").read_text(encoding="utf-8")
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen, [URI])
        self.assertNotIn(URI, out)
        self.assertNotIn(URI, err)
        self.assertNotIn(URI, text)
        self.assertNotIn("ed2k://", out + err + text)
        self.assertNotIn("magnet:", out + err + text)
        self.assertEqual(list(root.glob("av-auto-url.*")), [])
        saved = json.loads(text)
        self.assertEqual(saved["submitted"]["ABP-108"]["kind"], "ed2k")
        mode = stat.S_IMODE(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(mode, 0o600)

    def test_406_backs_off_and_risk_stops_new_submits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/av-auto/ABP-073/ABP-073.strm"}

            def submit(code, _uri):
                deps.calls.append(("submit-body", code))
                if code == "ABP-108":
                    return {"class": "backoff", "detail": "HTTP 406"}
                return {"class": "ok", "detail": ""}

            deps._submit = submit
            rc, out, _err = self._capture(deps)
            submitted = [item[1] for item in deps.calls if item[0] == "submit-body"]
            self.assertEqual(rc, 0, out)
            self.assertEqual(_names(deps, "frames"), ["ABP-073"])
            self.assertEqual(submitted, ["ABP-108"])
            self.assertIn("BACKOFF ABP-108", out)
            deps.calls.clear()
            rc, out, _err = self._capture(deps)
            self.assertEqual([item[1] for item in deps.calls if item[0] == "submit-body"], [])
            deps.clock += timedelta(seconds=180)
            rc, _out, _err = self._capture(deps)
            self.assertEqual([item[1] for item in deps.calls if item[0] == "submit-body"], ["ABP-108"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/av-auto/ABP-073/ABP-073.strm"}

            def submit(code, _uri):
                deps.calls.append(("submit-body", code))
                return {"class": "block", "detail": "需要验证码"}

            deps._submit = submit
            rc, out, _err = self._capture(deps)
            blocked = (root / "BLOCKED").read_text(encoding="utf-8")
            self.assertEqual(rc, 0, out)
            self.assertIn("ABP-073", _names(deps, "frames"))
            self.assertEqual([item[1] for item in deps.calls if item[0] == "submit-body"], ["ABP-108"])
            self.assertIn("验证码", blocked)
            self.assertIn("停止新的交磁", blocked)
            self.assertNotIn(URI, blocked)
            self.assertNotIn(URI, out)
            deps.calls.clear()
            rc, out, _err = self._capture(deps)
            self.assertEqual([item[1] for item in deps.calls if item[0] == "submit-body"], [])
            self.assertIn("停止新的交磁", out)

    def test_low_quota_still_processes_titles_that_already_have_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            deps.space_sample["offline_surplus"] = 4
            deps.files["ABP-073"] = {"action": "frames", "strm": "/media/av-auto/ABP-073/ABP-073.strm"}
            rc, out, _err = self._capture(deps)
            saved = json.loads((root / "progress.json").read_text(encoding="utf-8"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(_names(deps, "frames"), ["ABP-073"])
        self.assertNotIn("submit", [item[0] for item in deps.calls])
        self.assertEqual(saved["submit_hold"]["reason"], "quota")
        self.assertNotIn("这是磁盘满", out)

    def test_status_reports_counts_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            progress = library_scan.empty_progress()
            progress["done"] = {"ABP-073": {"at": "2026-11-11T01:00:00+08:00", "via": "frames"}}
            progress["costs"] = {"deepseek_cny": 1.5, "luna_usd_est": 0.2}
            progress["stuck"] = {"ABF-397": {"name": "ABF-397_Restored.mp4", "percent": 0.14}}
            library_scan.save_progress(root / "progress.json", progress)
            rc, out, _err = self._capture(deps, status=True)
        self.assertEqual(rc, 0)
        self.assertIn("remaining: 5", out)
        self.assertIn("done=1", out)
        self.assertIn("stuck=1", out)
        self.assertIn("deepseek_cny=1.5", out)
        self.assertIn("phase=", out)
        self.assertIn("updated_at=", out)
        self.assertIn("remaining=", out)
        self.assertNotIn(URI, out)
        self.assertNotIn("submit", [item[0] for item in deps.calls])

    def test_inventory_writes_progress_before_each_files_of_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deps = Deps(root)
            deps.load_corpus = lambda: {
                "titles": [
                    {"code": "ABP-073", "user_verdict": "pending", "erotic_level": "high"},
                    {"code": "ABP-108", "user_verdict": "pending", "erotic_level": None},
                ],
                "links": {
                    "ABP-073": {"ed2k": [URI], "magnet": []},
                    "ABP-108": {"ed2k": [], "magnet": [MAGNET]},
                },
            }
            log = FlushLog()
            seen = []

            def files_of(code):
                raw = (root / "progress.json").read_text(encoding="utf-8")
                self.assertNotIn(URI, raw)
                self.assertNotIn("magnet:", raw)
                doc = json.loads(raw)
                seen.append(dict(doc))
                self.assertEqual(doc["phase"], "inventory")
                self.assertEqual(doc["current"], code)
                self.assertEqual(doc["inventory_total"], 2)
                self.assertIn(f"核对 115 {doc['inventory_index']}/2 {code}", log.getvalue())
                self.assertGreater(log.flushes, 0)
                if code == "ABP-073":
                    self.assertEqual(doc["inventory_index"], 1)
                    self.assertNotIn("核对 115 2/2", log.getvalue())
                line = library_scan.format_status(doc)
                self.assertIn("phase=inventory", line)
                self.assertIn(f"current={code}", line)
                self.assertIn("updated_at=", line)
                self.assertNotIn(URI, line)
                return {"action": "none", "strm": ""}

            deps.files_of = files_of
            err = io.StringIO()
            with redirect_stdout(log), redirect_stderr(err):
                rc = library_scan.execute(_args(), deps)
            self.assertEqual(rc, 0, err.getvalue())
            self.assertEqual([item["current"] for item in seen], ["ABP-073", "ABP-108"])
            self.assertEqual([item["inventory_index"] for item in seen], [1, 2])
            text = log.getvalue() + (root / "progress.json").read_text(encoding="utf-8")
            self.assertNotIn(URI, text)
            self.assertNotIn("ed2k://", text)
            rc, out, _err = self._capture(deps, status=True)
            self.assertEqual(rc, 0)
            self.assertIn("phase=", out)
            self.assertIn("updated_at=", out)
            self.assertIn("done=", out)
            self.assertIn("remaining=", out)
            self.assertIn("inventory=", out)
            self.assertNotIn(URI, out)


class ExportAndHelpTests(unittest.TestCase):
    def test_export_builds_uris_from_hash_columns_and_skips_source_url(self):
        secret = "super-secret-password"
        page = "http://example.test/not-a-download"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / "connection.env"
            env.write_text(
                "\n".join([
                    "FINDDAV_HOST=127.0.0.1",
                    "FINDDAV_PORT=3316",
                    "FINDDAV_USER=findav_user",
                    f"FINDDAV_PASSWORD={secret}",
                    "FINDDAV_DB=findav",
                    "",
                ]),
                encoding="utf-8",
            )
            os.chmod(env, 0o600)
            fake = root / "bin"
            fake.mkdir()
            query_log = root / "queries.txt"
            (fake / "docker").write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "argv = sys.argv[1:]\n"
                "pwd = os.environ.get('MYSQL_PWD', '')\n"
                "if pwd and pwd in ' '.join(argv):\n"
                "    sys.stderr.write('password on argv\\n')\n"
                "    raise SystemExit(9)\n"
                "query = argv[-1] if argv else ''\n"
                "Path(os.environ['QUERY_LOG']).open('a', encoding='utf-8').write(query + '\\n')\n"
                "if 'source_url' in query:\n"
                "    sys.stderr.write('source_url selected\\n')\n"
                "    raise SystemExit(3)\n"
                "if 'FROM av_title' in query:\n"
                "    sys.stdout.write('ABP-073\\tpending\\thigh\\n')\n"
                "    sys.stdout.write('ABP-108\\tpending\\t\\n')\n"
                "elif 'FROM av_ed2k' in query:\n"
                "    if 'file_hash' not in query or 'size_bytes' not in query:\n"
                "        sys.stderr.write('ed2k columns missing\\n')\n"
                "        raise SystemExit(4)\n"
                "    sys.stdout.write('ABP-073\\tad.mp4\\t1000\\tBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\\n')\n"
                "    sys.stdout.write('ABP-073\\tABP-073|feature.mp4\\t2000000000\\tABCDEF0123456789ABCDEF0123456789\\n')\n"
                "    sys.stdout.write('NONAME\\t\\t4096\\tAbC\\n')\n"
                "    sys.stdout.write('NOHASH\\tx.mp4\\t5000\\t\\n')\n"
                "    sys.stdout.write('NOSIZE\\tx.mp4\\t0\\tCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC\\n')\n"
                "    sys.stdout.write('\\tx.mp4\\t10\\tDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD\\n')\n"
                "elif 'FROM av_magnet' in query:\n"
                "    if 'infohash' not in query:\n"
                "        sys.stderr.write('infohash missing\\n')\n"
                "        raise SystemExit(5)\n"
                "    sys.stdout.write('ABP-246\\t0123456789abcdef0123456789ABCDEF\\n')\n"
                "    sys.stdout.write('ABP-108\\t  AbCDEF0123456789ABCDEF0123456789  \\n')\n"
                "    sys.stdout.write('NOMAG\\t   \\n')\n"
                "    sys.stdout.write('\\tabcdef\\n')\n"
                "else:\n"
                "    sys.stderr.write('unexpected\\n')\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            (fake / "docker").chmod(0o755)
            dest = root / "corpus.tsv"
            env_run = os.environ.copy()
            env_run["PATH"] = str(fake) + os.pathsep + env_run.get("PATH", "")
            env_run["AV_AUTO_FINDAV_ENV"] = str(env)
            env_run["FINDAV_DB_CONTAINER"] = "findav-db"
            env_run["QUERY_LOG"] = str(query_log)
            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "export_findav_corpus.sh"), str(dest)],
                capture_output=True,
                text=True,
                env=env_run,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            blob = result.stdout + result.stderr
            queries = query_log.read_text(encoding="utf-8")
            self.assertNotIn(secret, blob)
            self.assertNotIn("ed2k://", blob)
            self.assertNotIn("magnet:", blob)
            self.assertNotIn(page, blob)
            self.assertNotIn("source_url", queries)
            self.assertIn("titles=2", result.stdout)
            self.assertIn("ed2k_rows=3", result.stdout)
            self.assertIn("magnet_rows=2", result.stdout)
            text = dest.read_text(encoding="utf-8")
            self.assertNotIn(page, text)
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
            parsed = library_scan.parse_corpus_tsv(text)
            expected_ed2k = library_scan.ed2k_uris_from_rows([
                {"code": "ABP-073", "file_name": "ad.mp4", "size_bytes": 1000, "file_hash": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB", "source_url": page},
                {"code": "ABP-073", "file_name": "ABP-073|feature.mp4", "size_bytes": 2000000000, "file_hash": "ABCDEF0123456789ABCDEF0123456789", "source_url": page},
                {"code": "NONAME", "file_name": "", "size_bytes": 4096, "file_hash": "AbC"},
                {"code": "NOHASH", "file_name": "x.mp4", "size_bytes": 5000, "file_hash": ""},
                {"code": "NOSIZE", "file_name": "x.mp4", "size_bytes": 0, "file_hash": "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"},
            ])
            got_ed2k = []
            for line in text.splitlines():
                if line.startswith("E\t"):
                    _kind, code, uri = line.split("\t", 2)
                    got_ed2k.append((code, uri))
            self.assertEqual(got_ed2k, expected_ed2k)
            expected_magnets = library_scan.magnet_uris_from_rows([
                {"code": "ABP-246", "infohash": "0123456789abcdef0123456789ABCDEF"},
                {"code": "ABP-108", "infohash": "  AbCDEF0123456789ABCDEF0123456789  "},
                {"code": "NOMAG", "infohash": "   "},
            ])
            got_magnets = []
            for line in text.splitlines():
                if line.startswith("M\t"):
                    _kind, code, uri = line.split("\t", 2)
                    got_magnets.append((code, uri))
            self.assertEqual(got_magnets, expected_magnets)
            picked = library_scan.pick_link(parsed["links"]["ABP-073"]["ed2k"], parsed["links"].get("ABP-108", {}).get("magnet"))
            self.assertEqual(picked["uri"], expected_ed2k[0][1])
            self.assertNotIn("NOHASH", parsed["links"])
            self.assertNotIn("NOSIZE", parsed["links"])
            self.assertNotIn("NOMAG", parsed["links"])

    def test_help_puts_the_code_after_the_subcommand(self):
        rc, out, err = _run_main(["--help"])
        self.assertEqual(rc, 0, err)
        self.assertIn("library-scan", out)
        rc, out, err = _run_main(["library-scan", "--help"])
        self.assertEqual(rc, 0, err)
        self.assertIn("av_auto library-scan", out)
        self.assertIn("av_auto library-scan ABP-073", out)
        self.assertIn("不刮削", out)
        self.assertIn("不 promote", out)
        self.assertIn("--dry-run", out)
        self.assertIn("--status", out)
        rc, _out, err = _run_main(["ABP-073", "library-scan"])
        self.assertEqual(rc, 2)
        self.assertIn("不是子命令", err)

    def test_wrapper_mentions_library_scan_without_a_baked_secret(self):
        text = (ROOT / "bin" / "av_auto").read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "export_findav_corpus.sh").read_text(encoding="utf-8")
        self.assertIn("library-scan", text)
        self.assertIn("export_findav_corpus.sh", text)
        self.assertIn("AV_AUTO_CORPUS_FILE", text)
        self.assertIn("ServerAliveInterval", text)
        self.assertNotIn("verify5", text)
        self.assertNotIn("ed2k://|file|", text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("ed2k_url", script)
        self.assertNotIn("source_url", script)
        self.assertIn("file_hash", script)
        self.assertIn("size_bytes", script)
        self.assertIn("infohash", script)
        self.assertEqual(library_scan.ED2K_FIELDS, ("code", "file_name", "size_bytes", "file_hash"))
        self.assertEqual(library_scan.MAGNET_FIELDS, ("code", "infohash"))
        syntax = subprocess.run(["bash", "-n", str(ROOT / "bin" / "av_auto")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        export_syntax = subprocess.run(
            ["bash", "-n", str(ROOT / "scripts" / "export_findav_corpus.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(export_syntax.returncode, 0, export_syntax.stderr)

    def test_default_frames_and_process_do_not_promote(self):
        with mock.patch.object(av_auto, "cmd_promote", side_effect=AssertionError("promote")), \
             mock.patch.object(av_auto, "_emby_refresh", side_effect=AssertionError("emby")), \
             mock.patch.object(av_auto, "_process_offline_title", return_value=0) as process, \
             mock.patch.object(av_auto, "cmd_frames", return_value=0) as frames, \
             mock.patch.object(av_auto, "_select_frames_dir", return_value=(Path("/tmp/frames"), 0)), \
             mock.patch.object(av_auto, "_process_review", return_value=({"agreement": "agree"}, 0)), \
             mock.patch.object(av_auto, "_complete_review", return_value={"agreement": "agree", "consensus": "pass"}), \
             mock.patch.object(av_auto, "tiebreak_trigger", return_value=""), \
             mock.patch.object(av_auto, "_process_grok", return_value=0) as grok, \
             mock.patch.object(av_auto, "_persist_frames_dir", return_value=0):
            self.assertEqual(library_scan._process_default("ABP-073"), 0)
            self.assertEqual(
                library_scan._frames_default("ABP-073", "/media/av-auto/ABP-073/ABP-073.strm"),
                0,
            )
        process.assert_called_once_with("ABP-073")
        self.assertEqual(frames.call_args.args[0].code, "ABP-073")
        self.assertEqual(frames.call_args.args[0].strm, "/media/av-auto/ABP-073/ABP-073.strm")
        grok.assert_called_once()


if __name__ == "__main__":
    unittest.main()
