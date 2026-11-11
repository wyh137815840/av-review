"""Auto-promote switch and official /AV merge.

These tests never call 115, Emby, Magpie, or ssh. Pickcodes used here are
fake and must not show up in command output.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

DS = "deepseek/deepseek-flash"
LU = "chatgpt-api/gpt-6-luna"
CODE = "ABP-073"
PC = "pc-not-real"


def _review(code: str, ds: str, lu: str, agreement: str, consensus: str) -> dict:
    def row(verdict: str) -> dict:
        return {"ok": True, "verdict": verdict, "hits": [], "reason": verdict, "confidence": "mid"}

    return {
        "code": code,
        "valid_frames": [{"label": "frame_120s", "ss": 120, "file": f"{code}_frame_120s.jpg"}],
        "models": {DS: row(ds), LU: row(lu)},
        "agreement": agreement,
        "consensus": consensus,
    }


def _grok(code: str, kept: str, trigger: str = "disagree") -> dict:
    return {
        "code": code,
        "trigger": trigger,
        "verdict": kept,
        "kept_verdict": kept,
        "tiebreak": {"kept_verdict": kept, "resolved": kept in {"pass", "fail"}},
    }


def _run_main(argv: list[str]):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = av_auto.main(list(argv))
        except SystemExit as exc:
            return exc.code, out.getvalue(), err.getvalue()
    return rc, out.getvalue(), err.getvalue()


def _file(name: str, fid: str, size: int = 900_000_000) -> dict:
    return {"n": name, "fid": fid, "fc": "1", "s": size, "pc": PC}


def _dir(name: str, fid: str) -> dict:
    return {"n": name, "cid": fid, "fid": "", "fc": "0"}


class Pan:
    """In-memory parent listings. dir_id is a stale path index on purpose."""

    def __init__(self):
        self.moves: list[tuple[str, str]] = []
        self.children: dict[str, list[dict]] = {}
        self.dir_ids: dict[str, str] = {}
        self.fail_ids: set[str] = set()
        self.post_lists: dict[str, int] = {}
        self.lag_once: set[str] = set()
        self.hide_ids: set[str] = set()
        self.path_after_move = False
        self.moved = False

    def dir_id(self, path: str) -> str:
        if self.moved and self.path_after_move and path.rstrip("/").endswith("/" + CODE):
            raise AssertionError("post-move path lookup is not a move receipt")
        return self.dir_ids.get(path, "")

    def list_children(self, fid: str):
        items = list(self.children.get(str(fid), []))
        if self.moved:
            self.post_lists[str(fid)] = self.post_lists.get(str(fid), 0) + 1
            if str(fid) in self.lag_once and self.post_lists[str(fid)] == 1:
                items = [item for item in items if not _is_dir_row(item)]
                items = [item for item in items if _row_id(item) not in self._moved_ids()]
            if self.hide_ids:
                items = [item for item in items if _row_id(item) not in self.hide_ids]
        return items

    def move(self, fid: str, parent: str):
        if fid in self.fail_ids:
            raise RuntimeError("115 refused")
        self.moves.append((str(fid), str(parent)))
        item = self._take(str(fid))
        self.children.setdefault(str(parent), []).append(item)
        self.moved = True
        return {"state": True, "errno": 0}

    def _moved_ids(self) -> set[str]:
        return {fid for fid, _parent in self.moves}

    def _take(self, fid: str) -> dict:
        found = None
        for folder, rows in self.children.items():
            kept = []
            for row in rows:
                if _row_id(row) == fid and found is None:
                    found = row
                else:
                    kept.append(row)
            self.children[folder] = kept
        if found is None:
            found = _file(fid, fid)
        return found


def _row_id(item: dict) -> str:
    if str(item.get("fc")) == "0":
        return str(item.get("cid") or "")
    return str(item.get("fid") or "")


def _is_dir_row(item: dict) -> bool:
    return str(item.get("fc")) == "0"


@contextmanager
def _fast_clock():
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(sec):
        clock["t"] += float(sec)

    with mock.patch.object(av_auto.time, "monotonic", monotonic), mock.patch.object(av_auto.time, "sleep", sleep):
        yield clock


class PromoteMoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.official = self.root / "official-strm"
        self.staging = self.root / "staging-strm"
        self.addCleanup(self.tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ,
            {"AV_AUTO_PROMOTE_VERIFY_SEC": "1.2", "AV_AUTO_PROMOTE_VERIFY_INTERVAL": "0.4"},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _paths(self) -> dict:
        return {
            "official_115": "/AV",
            "staging_115": "/av-auto",
            "official_strm": self.official,
            "staging_strm": self.staging,
            "play_base": "http://play.invalid/no-query",
            "min_size_mb": 300,
            "emby_library": "AV",
        }

    def _rest(self, existing: Path | None = None, *, forbid_destination: bool = False):
        def build_play_url(_base, _pc, name):
            return f"play:{name}"

        def destination(_root, code, _kind):
            if forbid_destination:
                raise AssertionError("second official strm directory")
            return self.official / code

        def find_existing(_root, _code):
            return existing

        def safe_name(name):
            return str(name)

        return (build_play_url, destination, find_existing, safe_name, None)

    def _pan_base(self) -> Pan:
        pan = Pan()
        pan.dir_ids = {
            "/AV": "avid",
            "/av-auto": "stage",
            f"/av-auto/{CODE}": "src",
            f"/AV/{CODE}": "",
        }
        pan.children = {
            "avid": [],
            "stage": [_dir(CODE, "src")],
            "src": [_file(f"{CODE}.mp4", "feat")],
        }
        return pan

    def _staging_file(self) -> Path:
        folder = self.staging / CODE
        folder.mkdir(parents=True)
        path = folder / f"{CODE}.strm"
        path.write_text("staging\n", encoding="utf-8")
        return path

    def _run(self, pan, *args, **kwargs) -> str:
        with _fast_clock(), mock.patch.object(av_auto, "_emby_refresh", return_value="emby ok"):
            out = io.StringIO()
            with redirect_stdout(out):
                line = av_auto._promote_one(pan, self._rest(*args, **kwargs), self._paths(), CODE, False)
        self.assertNotIn(PC, out.getvalue() + line)
        self.assertNotIn("magnet:", line)
        self.assertNotIn("ed2k://", line)
        return line

    def test_stale_path_index_does_not_fail_a_landed_folder_move(self):
        pan = self._pan_base()
        pan.path_after_move = True
        pan.lag_once.add("avid")
        # Path index keeps seeing the old folder and never the new path.
        pan.dir_ids[f"/av-auto/{CODE}"] = "src"
        pan.dir_ids[f"/AV/{CODE}"] = ""
        staging = self._staging_file()
        line = self._run(pan)
        self.assertTrue(line.startswith("OK "), line)
        self.assertEqual(pan.moves, [("src", "avid")])
        self.assertFalse(staging.exists())
        official = self.official / CODE / f"{CODE}.strm"
        self.assertTrue(official.is_file(), line)
        self.assertEqual(official.read_text(encoding="utf-8"), f"play:{CODE}.mp4\n")
        self.assertNotIn(PC, official.read_text(encoding="utf-8"))

    def test_failed_move_leaves_the_title_and_staging_strm(self):
        pan = self._pan_base()
        pan.hide_ids.add("src")
        staging = self._staging_file()
        line = self._run(pan)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("仍在 av-auto", line)
        self.assertIn("staging strm 保留", line)
        self.assertEqual(pan.moves, [("src", "avid")])
        self.assertTrue(staging.is_file())
        self.assertFalse((self.official / CODE).exists())

    def test_rejected_move_does_not_roll_anything_back(self):
        pan = self._pan_base()
        pan.fail_ids.add("src")
        staging = self._staging_file()
        line = self._run(pan)
        self.assertIn("115 搬家失败", line)
        self.assertEqual(pan.moves, [])
        self.assertTrue(staging.is_file())
        self.assertFalse((self.official / CODE).exists())

    def test_merge_adds_new_files_and_does_not_overwrite_or_make_a_second_dir(self):
        pan = self._pan_base()
        pan.dir_ids[f"/AV/{CODE}"] = "dest"
        pan.children["avid"] = [_dir(CODE, "dest")]
        pan.children["dest"] = [_file(f"{CODE}.mp4", "old")]
        pan.children["src"] = [
            _file(f"{CODE}.mp4", "dup"),
            _file(f"{CODE}-UC.mp4", "new"),
        ]
        pan.lag_once.add("dest")
        library = self.official / "already" / CODE
        library.mkdir(parents=True)
        nfo = library / "movie.nfo"
        nfo.write_text("nfo-kept\n", encoding="utf-8")
        censored = library / f"{CODE}.strm"
        censored.write_text("censored-kept\n", encoding="utf-8")
        staging = self._staging_file()
        line = self._run(pan, library, forbid_destination=True)
        self.assertTrue(line.startswith("OK "), line)
        self.assertIn("未新建第二夹", line)
        self.assertIn("同名未覆盖", line)
        self.assertEqual(pan.moves, [("new", "dest")])
        self.assertEqual(nfo.read_text(encoding="utf-8"), "nfo-kept\n")
        self.assertEqual(censored.read_text(encoding="utf-8"), "censored-kept\n")
        added = library / f"{CODE}-UC.strm"
        self.assertTrue(added.is_file(), line)
        self.assertEqual(added.read_text(encoding="utf-8"), f"play:{CODE}-UC.mp4\n")
        self.assertNotIn(PC, added.read_text(encoding="utf-8"))
        self.assertTrue(staging.is_file())
        self.assertEqual(sorted(p.name for p in library.iterdir()), [f"{CODE}-UC.strm", f"{CODE}.strm", "movie.nfo"])
        self.assertFalse((self.official / CODE).exists())

    def test_merge_failure_rolls_new_files_back_and_keeps_staging(self):
        pan = self._pan_base()
        pan.dir_ids[f"/AV/{CODE}"] = "dest"
        pan.children["dest"] = [_file(f"{CODE}.mp4", "old")]
        pan.children["src"] = [_file("A.mp4", "file-a"), _file("B.mp4", "file-b")]
        pan.fail_ids.add("file-b")
        library = self.official / CODE
        library.mkdir(parents=True)
        censored = library / f"{CODE}.strm"
        censored.write_text("censored-kept\n", encoding="utf-8")
        (library / "movie.nfo").write_text("nfo-kept\n", encoding="utf-8")
        staging = self._staging_file()
        line = self._run(pan, library, forbid_destination=True)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("已搬回 av-auto", line)
        self.assertIn("staging strm 保留", line)
        self.assertEqual(pan.moves, [("file-a", "dest"), ("file-a", "src")])
        self.assertNotIn(("file-b", "dest"), pan.moves)
        self.assertEqual(censored.read_text(encoding="utf-8"), "censored-kept\n")
        self.assertFalse((library / "A.strm").exists())
        self.assertTrue(staging.is_file())

    def test_files_that_never_appear_in_dest_are_moved_back(self):
        pan = self._pan_base()
        pan.dir_ids[f"/AV/{CODE}"] = "dest"
        pan.children["dest"] = [_file(f"{CODE}.mp4", "old")]
        pan.children["src"] = [_file(f"{CODE}-UC.mp4", "new")]
        pan.hide_ids.add("new")
        staging = self._staging_file()
        library = self.official / CODE
        library.mkdir(parents=True)
        (library / f"{CODE}.strm").write_text("censored-kept\n", encoding="utf-8")
        line = self._run(pan, library, forbid_destination=True)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("已搬回 av-auto", line)
        self.assertEqual(pan.moves, [("new", "dest"), ("new", "src")])
        self.assertTrue(staging.is_file())
        self.assertFalse((library / f"{CODE}-UC.strm").exists())
        self.assertEqual((library / f"{CODE}.strm").read_text(encoding="utf-8"), "censored-kept\n")

    def test_dry_run_reports_merge_without_moving(self):
        pan = self._pan_base()
        pan.dir_ids[f"/AV/{CODE}"] = "dest"
        pan.children["dest"] = [_file(f"{CODE}.mp4", "old")]
        pan.children["src"] = [_file(f"{CODE}.mp4", "dup"), _file(f"{CODE}-UC.mp4", "new")]
        with _fast_clock():
            line = av_auto._promote_one(pan, self._rest(forbid_destination=False), self._paths(), CODE, True)
        self.assertIn("并入已有", line)
        self.assertIn("不覆盖", line)
        self.assertIn("不新建第二夹", line)
        self.assertNotIn("FAIL", line)
        self.assertEqual(pan.moves, [])

        pan.dir_ids[f"/AV/{CODE}"] = ""
        pan.children["dest"] = []
        pan.children["src"] = [_file(f"{CODE}.mp4", "feat")]
        with _fast_clock():
            fresh = av_auto._promote_one(pan, self._rest(), self._paths(), CODE, True)
        self.assertIn("（新建）", fresh)
        self.assertEqual(pan.moves, [])

    def test_already_moved_title_finishes_strm_without_another_move(self):
        pan = self._pan_base()
        pan.dir_ids[f"/av-auto/{CODE}"] = ""
        pan.dir_ids[f"/AV/{CODE}"] = "dest"
        pan.children["dest"] = [_file(f"{CODE}.mp4", "feat"), _file(f"{CODE}-UC.mp4", "new")]
        library = self.official / CODE
        library.mkdir(parents=True)
        censored = library / f"{CODE}.strm"
        censored.write_text("censored-kept\n", encoding="utf-8")
        (library / "movie.nfo").write_text("nfo-kept\n", encoding="utf-8")
        staging = self._staging_file()
        line = self._run(pan, library, forbid_destination=True)
        self.assertTrue(line.startswith("OK "), line)
        self.assertIn("未再搬家", line)
        self.assertEqual(pan.moves, [])
        self.assertEqual(censored.read_text(encoding="utf-8"), "censored-kept\n")
        self.assertEqual((library / "movie.nfo").read_text(encoding="utf-8"), "nfo-kept\n")
        self.assertEqual((library / f"{CODE}-UC.strm").read_text(encoding="utf-8"), f"play:{CODE}-UC.mp4\n")
        self.assertEqual(sorted(p.name for p in library.iterdir()), [f"{CODE}-UC.strm", f"{CODE}.strm", "movie.nfo"])
        self.assertFalse(staging.exists())


class PassDecisionTests(unittest.TestCase):
    def test_pass_matrix(self):
        agree_pass = _review(CODE, "pass", "pass", "agree", "pass")
        agree_fail = _review(CODE, "fail", "fail", "agree", "fail")
        disagree = _review(CODE, "pass", "fail", "disagree", "uncertain")
        partial = _review(CODE, "pass", "uncertain", "partial", "uncertain")
        unsure = _review(CODE, "uncertain", "uncertain", "agree", "uncertain")
        self.assertTrue(av_auto.review_passed(agree_pass, _grok(CODE, "fail")))
        self.assertFalse(av_auto.review_passed(agree_fail, _grok(CODE, "pass")))
        self.assertTrue(av_auto.review_passed(disagree, _grok(CODE, "pass")))
        self.assertFalse(av_auto.review_passed(disagree, _grok(CODE, "fail")))
        self.assertFalse(av_auto.review_passed(disagree, None))
        self.assertTrue(av_auto.review_passed(partial, _grok(CODE, "pass", "uncertain")))
        self.assertTrue(av_auto.review_passed(unsure, _grok(CODE, "pass", "uncertain")))
        self.assertFalse(av_auto.review_passed(unsure, _grok(CODE, "uncertain", "uncertain")))
        self.assertEqual(av_auto.REVIEW_MODELS, (DS, LU))
        self.assertNotIn("grok", " ".join(av_auto.REVIEW_MODELS).lower())

    def test_switch_file_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auto_promote.on"
            with mock.patch.dict(os.environ, {"AV_AUTO_AUTO_PROMOTE": "", "AV_AUTO_AUTO_PROMOTE_FILE": str(path)}):
                self.assertFalse(av_auto.auto_promote_enabled())
                path.write_text("", encoding="utf-8")
                self.assertTrue(av_auto.auto_promote_enabled())
                path.write_text("off\n", encoding="utf-8")
                self.assertFalse(av_auto.auto_promote_enabled())
                path.write_text("on\n", encoding="utf-8")
                self.assertTrue(av_auto.auto_promote_enabled())
                os.environ["AV_AUTO_AUTO_PROMOTE"] = "0"
                self.assertFalse(av_auto.auto_promote_enabled())
                os.environ["AV_AUTO_AUTO_PROMOTE"] = "1"
                path.write_text("off\n", encoding="utf-8")
                self.assertTrue(av_auto.auto_promote_enabled())


class AutoPromoteProcessTests(unittest.TestCase):
    def _layout(self, code: str) -> dict:
        return {
            "inbox": True,
            "dest": False,
            "dest_keepers": 0,
            "inbox_path": f"/av-auto/_staging/{code}",
            "dest_path": f"/av-auto/{code}",
        }

    def _run(self, codes, review_of, grok_kept: str = "", env: dict | None = None, flag_file: Path | None = None):
        calls = {"promote": [], "grok": [], "submit": 0}

        def fake_clean(args):
            return 0

        def fake_strm(args):
            return 0

        def fake_frames(args):
            folder = Path(args.out)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{args.code}_frame_120s.jpg"
            (folder / name).write_bytes(b"\xff\xd8" + b"\x00" * 800)
            manifest = {
                "code": args.code,
                "precheck": True,
                "shots": [{"label": "frame_120s", "ss": 120, "ok": True, "valid": True, "file": name}],
            }
            (folder / "frames_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return 0

        def fake_review(args):
            pair = review_of(args.code)
            (Path(args.out) / "review.json").write_text(
                json.dumps(_review(args.code, pair[0], pair[1], pair[2], pair[3])),
                encoding="utf-8",
            )
            if flag_file is not None and args.code == codes[-1] and len(codes) > 1:
                flag_file.write_text("on\n", encoding="utf-8")
            return 0

        def fake_grok(args):
            calls["grok"].append(args.code)
            kept = grok_kept or "fail"
            (Path(args.frames_dir) / "grok_review.json").write_text(
                json.dumps(_grok(args.code, kept)),
                encoding="utf-8",
            )
            return 0

        def fake_promote(args):
            calls["promote"].append((list(args.codes), bool(args.dry_run)))
            print(f"OK {args.codes[0]}: test promote")
            return 0

        def boom(*_a, **_k):
            calls["submit"] += 1
            raise AssertionError("must not submit")

        base_env = {
            "AV_AUTO_FRAMES_ROOT": "",
            "AV_AUTO_FRAMES_TMP": "",
            "AV_AUTO_AUTO_PROMOTE": "",
            "AV_AUTO_AUTO_PROMOTE_FILE": str(flag_file) if flag_file else "",
        }
        if env:
            base_env.update(env)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_env["AV_AUTO_FRAMES_ROOT"] = str(root / "frames")
            base_env["AV_AUTO_FRAMES_TMP"] = str(root / "tmpframes")
            with mock.patch.dict(os.environ, base_env, clear=False), \
                 mock.patch.object(av_auto, "_offline_layout", side_effect=self._layout), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=False), \
                 mock.patch.object(av_auto, "cmd_clean", side_effect=fake_clean), \
                 mock.patch.object(av_auto, "cmd_strm", side_effect=fake_strm), \
                 mock.patch.object(av_auto, "cmd_frames", side_effect=fake_frames), \
                 mock.patch.object(av_auto, "cmd_review", side_effect=fake_review), \
                 mock.patch.object(av_auto, "cmd_grok_review", side_effect=fake_grok), \
                 mock.patch.object(av_auto, "cmd_submit", side_effect=boom), \
                 mock.patch.object(av_auto, "cmd_promote", side_effect=fake_promote):
                rc, out, err = _run_main(["process", *codes])
        return rc, out, err, calls

    def test_switch_off_does_not_promote_a_pass(self):
        rc, out, err, calls = self._run(
            [CODE],
            lambda _c: ("pass", "pass", "agree", "pass"),
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["promote"], [])
        self.assertEqual(calls["submit"], 0)
        self.assertIn("未交磁、未刮削、未 promote", out)
        self.assertIn("不 promote", out)

    def test_agree_pass_promotes_and_fail_does_not(self):
        rc, out, err, calls = self._run(
            [CODE],
            lambda _c: ("pass", "pass", "agree", "pass"),
            env={"AV_AUTO_AUTO_PROMOTE": "1"},
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["promote"], [([CODE], False)])
        self.assertIn("已 promote", out)
        self.assertEqual(calls["grok"], [])
        self.assertNotIn("cursor/grok-4.7", out)

        rc, out, err, calls = self._run(
            ["ABP-108"],
            lambda _c: ("fail", "fail", "agree", "fail"),
            env={"AV_AUTO_AUTO_PROMOTE": "1"},
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["promote"], [])
        self.assertIn("SKIP promote ABP-108", out)
        self.assertIn("不是 pass", out)
        self.assertIn("未 promote", out)

    def test_grok_tiebreak_pass_promotes_and_grok_fail_does_not(self):
        rc, out, err, calls = self._run(
            [CODE],
            lambda _c: ("pass", "fail", "disagree", "uncertain"),
            grok_kept="pass",
            env={"AV_AUTO_AUTO_PROMOTE": "1"},
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["grok"], [CODE])
        self.assertEqual(calls["promote"], [([CODE], False)])
        self.assertIn("已 promote", out)

        rc, out, err, calls = self._run(
            ["ABP-108"],
            lambda _c: ("pass", "fail", "disagree", "uncertain"),
            grok_kept="fail",
            env={"AV_AUTO_AUTO_PROMOTE": "1"},
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["grok"], ["ABP-108"])
        self.assertEqual(calls["promote"], [])
        self.assertIn("SKIP promote ABP-108", out)

    def test_flag_is_reread_for_the_next_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "auto_promote.on"
            rc, out, err, calls = self._run(
                [CODE, "ABP-108"],
                lambda _c: ("pass", "pass", "agree", "pass"),
                flag_file=flag,
            )
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls["promote"], [(["ABP-108"], False)])
        self.assertIn(f"PROCESS {CODE}: 完成（未交磁、未刮削、未 promote）", out)
        self.assertIn("PROCESS ABP-108: 完成（未交磁、未刮削，已 promote）", out)

    def test_promote_failure_leaves_process_failed(self):
        def fail_promote(args):
            print(f"FAIL {args.codes[0]}: 搬家未在正式库落地，片子仍在 av-auto，staging strm 保留")
            return 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "AV_AUTO_FRAMES_ROOT": str(root / "frames"),
                "AV_AUTO_FRAMES_TMP": str(root / "tmpframes"),
                "AV_AUTO_AUTO_PROMOTE": "1",
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(av_auto, "_offline_layout", side_effect=self._layout), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=False), \
                 mock.patch.object(av_auto, "cmd_clean", return_value=0), \
                 mock.patch.object(av_auto, "cmd_strm", return_value=0), \
                 mock.patch.object(av_auto, "cmd_frames", side_effect=self._frames), \
                 mock.patch.object(av_auto, "cmd_review", side_effect=self._pass_review), \
                 mock.patch.object(av_auto, "cmd_grok_review", side_effect=AssertionError("grok")), \
                 mock.patch.object(av_auto, "cmd_submit", side_effect=AssertionError("submit")), \
                 mock.patch.object(av_auto, "cmd_promote", side_effect=fail_promote):
                rc, out, err = _run_main(["process", CODE])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL ABP-073", err)
        self.assertIn("staging strm 保留", out)
        self.assertNotIn("PROCESS 全部完成", out)

    @staticmethod
    def _frames(args):
        folder = Path(args.out)
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{args.code}_frame_120s.jpg"
        (folder / name).write_bytes(b"\xff\xd8" + b"\x00" * 800)
        (folder / "frames_manifest.json").write_text(json.dumps({
            "code": args.code,
            "precheck": True,
            "shots": [{"label": "frame_120s", "ss": 120, "ok": True, "valid": True, "file": name}],
        }), encoding="utf-8")
        return 0

    @staticmethod
    def _pass_review(args):
        (Path(args.out) / "review.json").write_text(
            json.dumps(_review(args.code, "pass", "pass", "agree", "pass")),
            encoding="utf-8",
        )
        return 0


if __name__ == "__main__":
    unittest.main()
