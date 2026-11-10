"""Official post-offline `av_auto process` and the code-as-command pitfall.

These tests never call 115, Magpie, ffmpeg, or ssh to a real host.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

DS = "deepseek/deepseek-flash"
LU = "chatgpt-api/gpt-6-luna"
FIVE = ["ABP-073", "ABP-108", "ABP-246", "ABP-313", "ABP-383"]
OFFICIAL = "av_auto process " + " ".join(FIVE)


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


def _run_main(argv: list[str]):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = av_auto.main(list(argv))
        except SystemExit as exc:
            return exc.code, out.getvalue(), err.getvalue()
    return rc, out.getvalue(), err.getvalue()


class ArgvOrderTests(unittest.TestCase):
    def test_code_before_subcommand_is_rejected_with_the_official_command(self):
        with mock.patch.object(av_auto, "cmd_clean", side_effect=AssertionError("clean ran")):
            with mock.patch.object(av_auto, "cmd_process", side_effect=AssertionError("process ran")):
                rc, _out, err = _run_main(["ABP-073", "clean"])
        self.assertEqual(rc, 2)
        self.assertIn(av_auto.code_as_command_message(["ABP-073", "clean"]), err)
        self.assertIn("av_auto process ABP-073", err)
        self.assertNotIn("invalid choice", err)
        self.assertNotIn("argument cmd", err)

    def test_several_codes_before_a_subcommand_name_every_code(self):
        rc, _out, err = _run_main(FIVE + ["clean"])
        self.assertEqual(rc, 2)
        self.assertIn(OFFICIAL, err)
        self.assertIn("不是子命令", err)
        self.assertNotIn("invalid choice", err)

    def test_lowercase_code_and_bare_code_are_not_commands(self):
        rc, _out, err = _run_main(["abp-073", "frames"])
        self.assertEqual(rc, 2)
        self.assertIn("av_auto process ABP-073", err)
        self.assertIn("av_auto abp-073 frames", err)

        rc, _out, err = _run_main(["ABP-108"])
        self.assertEqual(rc, 2)
        self.assertIn("av_auto process ABP-108", err)

        rc, _out, err = _run_main(["--", "ABP-246", "process"])
        self.assertEqual(rc, 2)
        self.assertIn("av_auto process ABP-246", err)
        self.assertNotIn("invalid choice", err)

    def test_real_commands_still_parse_with_the_code_after_the_verb(self):
        with mock.patch.object(av_auto, "cmd_clean", return_value=0) as clean:
            rc, _out, err = _run_main(["clean", "--code", "ABP-073"])
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(clean.call_args.args[0].code, "ABP-073")

        with mock.patch.object(av_auto, "cmd_frames", return_value=0) as frames:
            rc, _out, _err = _run_main(["frames", "ABP-073"])
        self.assertEqual(rc, 0)
        self.assertEqual(frames.call_args.args[0].code, "ABP-073")

    def test_process_rejects_a_verb_mixed_into_the_code_list(self):
        with mock.patch.object(av_auto, "_process_offline_title", side_effect=AssertionError("ran")):
            rc, _out, err = _run_main(["process", "ABP-073", "clean"])
        self.assertEqual(rc, 2)
        self.assertIn("process 只接受番号", err)
        self.assertIn("av_auto process ABP-073", err)

    def test_help_shows_the_exact_post_offline_invocation(self):
        rc, out, err = _run_main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn(OFFICIAL, out)
        self.assertIn("番号不是子命令", out)
        self.assertEqual(err, "")

        rc, out, err = _run_main(["process", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn(OFFICIAL, out)
        self.assertIn("命令在前，番号在后", out)
        self.assertIn("不重新交磁，不刮削，不搬进 /AV", out)
        self.assertEqual(err, "")


class ProcessPipelineTests(unittest.TestCase):
    def _layout(self, code: str, *, inbox: bool, keepers: int) -> dict:
        return {
            "inbox": inbox,
            "dest": keepers > 0,
            "dest_keepers": keepers,
            "inbox_path": f"/av-auto/_staging/{code}",
            "dest_path": f"/av-auto/{code}",
        }

    @contextmanager
    def _patched(self, root: Path, calls: list, review_of, grok_trigger: str = ""):
        state = {"inbox": True, "strm": False}

        def layout(code):
            if state["inbox"]:
                return self._layout(code, inbox=True, keepers=0)
            return self._layout(code, inbox=False, keepers=1)

        def strm_ready(_code):
            return state["strm"]

        def candidates(code):
            return [root / code]

        def fake_clean(args):
            calls.append(("clean", args.code))
            return 0

        def fake_strm(args):
            calls.append(("strm", tuple(args.codes)))
            return 0

        def fake_frames(args):
            calls.append(("frames", args.code, bool(args.cleanup), bool(args.no_precheck), bool(args.no_reuse)))
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
            calls.append(("review", args.code, bool(args.cleanup), args.out))
            pair = review_of(args.code)
            (Path(args.out) / "review.json").write_text(
                json.dumps(_review(args.code, pair[0], pair[1], pair[2], pair[3])),
                encoding="utf-8",
            )
            return 0

        def fake_grok(args):
            calls.append(("grok", args.code, args.frames_dir))
            if not grok_trigger:
                raise AssertionError("grok ran for a settled review")
            (Path(args.frames_dir) / "grok_review.json").write_text(
                json.dumps({"code": args.code, "trigger": grok_trigger, "verdict": "fail"}),
                encoding="utf-8",
            )
            return 0

        def boom(*_args, **_kwargs):
            raise AssertionError("process must not submit, wait, scrape, or promote")

        with mock.patch.object(av_auto, "_offline_layout", side_effect=layout), \
             mock.patch.object(av_auto, "_staging_strm_ready", side_effect=strm_ready), \
             mock.patch.object(av_auto, "_candidate_frames_dirs", side_effect=candidates), \
             mock.patch.object(av_auto, "cmd_clean", side_effect=fake_clean), \
             mock.patch.object(av_auto, "cmd_strm", side_effect=fake_strm), \
             mock.patch.object(av_auto, "cmd_frames", side_effect=fake_frames), \
             mock.patch.object(av_auto, "cmd_review", side_effect=fake_review), \
             mock.patch.object(av_auto, "cmd_grok_review", side_effect=fake_grok), \
             mock.patch.object(av_auto, "cmd_submit", side_effect=boom), \
             mock.patch.object(av_auto, "cmd_wait", side_effect=boom), \
             mock.patch.object(av_auto, "cmd_intake", side_effect=boom), \
             mock.patch.object(av_auto, "cmd_promote", side_effect=boom):
            yield state

    def test_official_command_reviews_five_codes_and_skips_grok_when_they_agree(self):
        calls: list = []

        def agreed(_code):
            return ("pass", "pass", "agree", "pass")

        with tempfile.TemporaryDirectory() as tmp:
            with self._patched(Path(tmp), calls, agreed):
                rc, out, err = _run_main(["process", *FIVE])
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "")
        self.assertEqual([item[0] for item in calls], ["clean", "strm", "frames", "review"] * 5)
        self.assertEqual([item[1] for item in calls if item[0] == "clean"], FIVE)
        for kind, code, cleanup, no_precheck, no_reuse in [item for item in calls if item[0] == "frames"]:
            self.assertIn(code, FIVE)
            self.assertFalse(cleanup)
            self.assertFalse(no_precheck)
            self.assertFalse(no_reuse)
        for _kind, code, cleanup, _out in [item for item in calls if item[0] == "review"]:
            self.assertIn(code, FIVE)
            self.assertFalse(cleanup)
        self.assertNotIn(("grok",), [item[:1] for item in calls])
        self.assertEqual(out.count("SKIP grok-review"), 5)
        self.assertIn("未交磁、未刮削、未 promote", out)
        self.assertIn("PROCESS 全部完成: " + " ".join(FIVE), out)

    def test_second_run_skips_work_already_done(self):
        calls: list = []

        def agreed(_code):
            return ("fail", "fail", "agree", "fail")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._patched(root, calls, agreed) as state:
                rc, _out, err = _run_main(["process", "ABP-073"])
                self.assertEqual(rc, 0, err)
                review_bytes = (root / "ABP-073" / "review.json").read_bytes()
                manifest_bytes = (root / "ABP-073" / "frames_manifest.json").read_bytes()
                calls.clear()
                state["inbox"] = False
                state["strm"] = True
                rc, out, err = _run_main(["process", "ABP-073"])
                self.assertEqual(rc, 0, err)
                self.assertEqual(calls, [])
                self.assertIn("SKIP clean ABP-073", out)
                self.assertIn("SKIP strm ABP-073", out)
                self.assertIn("SKIP frames ABP-073", out)
                self.assertIn("SKIP review ABP-073", out)
                self.assertIn("SKIP grok-review ABP-073", out)
                self.assertEqual((root / "ABP-073" / "review.json").read_bytes(), review_bytes)
                self.assertEqual((root / "ABP-073" / "frames_manifest.json").read_bytes(), manifest_bytes)
                self.assertFalse((root / "ABP-073" / "grok_review.json").exists())

    def test_disagreement_runs_grok_once_and_rerun_skips_it(self):
        calls: list = []

        def split(_code):
            return ("pass", "fail", "disagree", "uncertain")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self._patched(root, calls, split, grok_trigger="disagree") as state:
                rc, out, err = _run_main(["process", "ABP-313"])
                self.assertEqual(rc, 0, err)
                self.assertEqual([item[0] for item in calls], ["clean", "strm", "frames", "review", "grok"])
                grok = json.loads((root / "ABP-313" / "grok_review.json").read_text(encoding="utf-8"))
                self.assertEqual(grok["trigger"], "disagree")
                self.assertNotIn("SKIP grok-review", out)
                calls.clear()
                state["inbox"] = False
                state["strm"] = True
                rc, out, err = _run_main(["process", "ABP-313"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [])
        self.assertIn("SKIP grok-review ABP-313: 已有 grok_review.json（trigger=disagree）", out)

    def test_uncertain_review_runs_grok_and_does_not_promote(self):
        calls: list = []

        def unsure(_code):
            return ("uncertain", "uncertain", "agree", "uncertain")

        with tempfile.TemporaryDirectory() as tmp:
            with self._patched(Path(tmp), calls, unsure, grok_trigger="uncertain"):
                rc, out, err = _run_main(["process", "ABP-383"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls[-1][0], "grok")
        self.assertEqual(calls[-1][1], "ABP-383")
        self.assertIn("不 promote", out)

    def test_one_missing_title_does_not_drop_the_rest_or_submit(self):
        calls: list = []

        def agreed(_code):
            return ("pass", "pass", "agree", "pass")

        with tempfile.TemporaryDirectory() as tmp:
            with self._patched(Path(tmp), calls, agreed) as state:
                def layout(code):
                    if code == "ABP-073":
                        return self._layout(code, inbox=False, keepers=0)
                    return self._layout(code, inbox=True, keepers=0)

                with mock.patch.object(av_auto, "_offline_layout", side_effect=layout):
                    rc, out, err = _run_main(["process", "ABP-073", "ABP-108"])
                self.assertTrue(state)
        self.assertEqual(rc, 1)
        self.assertIn("都没有正片", err)
        self.assertIn("PROCESS 未完成: ABP-073", err)
        self.assertNotIn("clean', 'ABP-073'", str(calls))
        self.assertEqual([item for item in calls if item[0] == "clean"], [("clean", "ABP-108")])
        self.assertIn("PROCESS ABP-108: 完成", out)
        self.assertIn("不交磁", err)

    def test_invalid_or_unchecked_frames_do_not_count_as_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "x.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 800)
            (folder / "frames_manifest.json").write_text(
                json.dumps({
                    "precheck": True,
                    "shots": [{"ok": False, "valid": False, "file": "x.jpg", "discarded": True}],
                }),
                encoding="utf-8",
            )
            self.assertFalse(av_auto._frames_ready(folder))
            (folder / "frames_manifest.json").write_text(
                json.dumps({
                    "precheck": False,
                    "shots": [{"ok": True, "valid": True, "file": "x.jpg"}],
                }),
                encoding="utf-8",
            )
            self.assertFalse(av_auto._frames_ready(folder))
            (folder / "review.json").write_text(
                json.dumps({
                    "code": "ABP-073",
                    "models": {
                        "cursor/grok-4.7": {"ok": True, "verdict": "pass", "reason": "no"},
                        "minimax-cn/x": {"ok": True, "verdict": "fail", "reason": "no"},
                        DS: {"ok": True, "verdict": "pass", "reason": "ok"},
                    },
                }),
                encoding="utf-8",
            )
            self.assertIsNone(av_auto._complete_review(folder))


class CleanRerunTests(unittest.TestCase):
    def _paths(self):
        return {
            "staging_inbox": "/av-auto/_staging",
            "staging_115": "/av-auto",
            "min_size_mb": 300,
        }

    def test_empty_inbox_succeeds_when_the_feature_is_already_staged(self):
        pan = mock.Mock()
        client = mock.Mock()
        pan.dir_id.return_value = "inbox"
        pan.ensure_dir.return_value = "dest"

        def videos(_pan, fid, min_bytes=None):
            self.assertEqual(fid, "dest")
            return [{"name": "ABP-073.mp4", "id": "file", "size": 900_000_000, "pickcode": "pc"}]

        with mock.patch.object(av_auto, "_paths", return_value=self._paths()), \
             mock.patch.object(av_auto, "_pan", return_value=(pan, ())), \
             mock.patch.object(av_auto, "_p115", return_value=client), \
             mock.patch.object(av_auto, "_walk", return_value=[]), \
             mock.patch.object(av_auto, "_videos_in", side_effect=videos):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = av_auto.cmd_clean(SimpleNamespace(code="abp-073"))
        self.assertEqual(rc, 0)
        self.assertIn("SKIP clean ABP-073", out.getvalue())
        self.assertNotIn("FAIL", out.getvalue())
        pan.move.assert_not_called()
        client.fs_delete.assert_called_with({"fid": "inbox", "ignore_warn": 1})

    def test_inbox_without_a_feature_anywhere_is_left_in_place(self):
        pan = mock.Mock()
        client = mock.Mock()
        pan.dir_id.return_value = "inbox"
        pan.ensure_dir.return_value = "dest"
        junk = [{"name": "promo.apk", "id": "junk", "is_dir": False, "size": 1000, "pickcode": ""}]
        with mock.patch.object(av_auto, "_paths", return_value=self._paths()), \
             mock.patch.object(av_auto, "_pan", return_value=(pan, ())), \
             mock.patch.object(av_auto, "_p115", return_value=client), \
             mock.patch.object(av_auto, "_walk", return_value=junk), \
             mock.patch.object(av_auto, "_videos_in", return_value=[]):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = av_auto.cmd_clean(SimpleNamespace(code="ABP-108"))
        self.assertEqual(rc, 1)
        self.assertIn("不搬、不删 inbox", out.getvalue())
        client.fs_delete.assert_not_called()
        pan.move.assert_not_called()

    def test_moved_feature_still_removes_the_inbox_directory(self):
        pan = mock.Mock()
        client = mock.Mock()
        pan.dir_id.return_value = "inbox"
        pan.ensure_dir.return_value = "dest"
        walks = {"n": 0}

        def walk(_pan, _fid):
            walks["n"] += 1
            if walks["n"] == 1:
                return [{
                    "name": "ABP-246.mp4",
                    "id": "file",
                    "is_dir": False,
                    "size": 900_000_000,
                    "pickcode": "pc",
                }]
            return []

        with mock.patch.object(av_auto, "_paths", return_value=self._paths()), \
             mock.patch.object(av_auto, "_pan", return_value=(pan, ())), \
             mock.patch.object(av_auto, "_p115", return_value=client), \
             mock.patch.object(av_auto, "_walk", side_effect=walk):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = av_auto.cmd_clean(SimpleNamespace(code="ABP-246"))
        self.assertEqual(rc, 0)
        pan.move.assert_called_once_with("file", "dest")
        client.fs_delete.assert_called_once_with({"fid": "inbox", "ignore_warn": 1})
        self.assertIn("KEEP ABP-246", out.getvalue())
        self.assertIn("已删空 inbox 夹", out.getvalue())


class ShellEntryTests(unittest.TestCase):
    def _shell_code_re(self) -> str:
        text = (ROOT / "bin" / "av_auto").read_text(encoding="utf-8")
        match = re.search(r"CATALOG_CODE_RE='([^']+)'", text)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_shell_regex_matches_python(self):
        pattern = self._shell_code_re()
        samples = {
            "ABP-073": True,
            "abp-108": True,
            "ABP-246": True,
            "ABP-313": True,
            "ABP-383": True,
            "FC2-PPV-1234567": True,
            "fc2-1234567": True,
            "123456-001": True,
            "ABP-073-4K": True,
            "clean": False,
            "process": False,
            "frames": False,
            "review": False,
            "grok-review": False,
            "promote": False,
            "submit": False,
            "--help": False,
            "status": False,
        }
        for token, expected in samples.items():
            self.assertEqual(av_auto.looks_like_catalog_code(token), expected, token)
            result = subprocess.run(
                ["bash", "-c", '[[ "$1" =~ $2 ]]', "bash", token, pattern],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode == 0, expected, token)

    def test_shell_rejects_before_ssh_and_matches_python(self):
        syntax = subprocess.run(["bash", "-n", str(ROOT / "bin" / "av_auto")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        text = (ROOT / "bin" / "av_auto").read_text(encoding="utf-8")
        self.assertIn("frames|review|grok-review|process)", text)

        with tempfile.TemporaryDirectory() as tmp:
            rc, err, logged = self._invoke(Path(tmp), ["ABP-073", "clean"])
        self.assertEqual(rc, 2)
        self.assertIn(av_auto.code_as_command_message(["ABP-073", "clean"]), err)
        self.assertEqual(logged, "")

        with tempfile.TemporaryDirectory() as tmp:
            rc, err, logged = self._invoke(Path(tmp), FIVE + ["clean"])
        self.assertEqual(rc, 2)
        self.assertIn(OFFICIAL, err)
        self.assertEqual(logged, "")

    def test_shell_still_forwards_real_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, err, logged = self._invoke(Path(tmp), ["status"])
        self.assertEqual(rc, 0, err)
        self.assertIn("av_auto.py", logged)
        self.assertIn("status", logged)
        self.assertNotIn("bash -s", logged)
        self.assertNotIn("llm_runtime.json", logged)

        with tempfile.TemporaryDirectory() as tmp:
            rc, err, logged = self._invoke(Path(tmp), ["process", "--help"])
        self.assertEqual(rc, 0, err)
        self.assertIn("av_auto.py", logged)
        self.assertIn("process", logged)
        self.assertIn("--help", logged)
        self.assertIn("bash -s", logged)
        self.assertIn("AV_AUTO_LLM_FILE=", logged)
        self.assertIn("llm_runtime.", logged)
        self.assertNotIn(
            "rm -f -- /mnt/user/appdata/moviepilot/config/scripts/av-auto-fs/llm_runtime.json",
            logged,
        )

    def _invoke(self, tmp: Path, args: list[str]):
        fake = tmp / "bin"
        fake.mkdir()
        log = tmp / "ssh.log"
        ssh = fake / "ssh"
        ssh.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SSH_LOG\"\ncat >/dev/null\nexit 0\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(fake) + os.pathsep + env.get("PATH", "")
        env["SSH_LOG"] = str(log)
        result = subprocess.run(
            ["bash", str(ROOT / "bin" / "av_auto"), *args],
            capture_output=True,
            text=True,
            env=env,
        )
        logged = log.read_text(encoding="utf-8") if log.exists() else ""
        return result.returncode, result.stderr, logged


class DurableFramesTests(unittest.TestCase):
    """Frames and review.json must survive outside container /tmp.

    The 2026-11-11 ABP-073/108/246/313/383 run reported 完成 while the
    artifacts existed only under /tmp/av-auto-frames.
    """

    def _layout(self, code: str, *, inbox: bool, keepers: int) -> dict:
        return {
            "inbox": inbox,
            "dest": keepers > 0,
            "dest_keepers": keepers,
            "inbox_path": f"/av-auto/_staging/{code}",
            "dest_path": f"/av-auto/{code}",
        }

    def _seed(self, folder: Path, code: str, review: dict, grok: dict | None = None) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{code}_frame_120s.jpg"
        (folder / name).write_bytes(b"\xff\xd8" + b"\x00" * 800)
        manifest = {
            "code": code,
            "precheck": True,
            "shots": [{"label": "frame_120s", "ss": 120, "ok": True, "valid": True, "file": name}],
        }
        (folder / "frames_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "review.json").write_text(json.dumps(review), encoding="utf-8")
        if grok is not None:
            (folder / "grok_review.json").write_text(json.dumps(grok), encoding="utf-8")

    def _envs(self, official: Path, scratch: Path):
        return mock.patch.dict(
            os.environ,
            {"AV_AUTO_FRAMES_ROOT": str(official), "AV_AUTO_FRAMES_TMP": str(scratch)},
            clear=False,
        )

    def test_candidate_order_prefers_the_official_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "official"
            scratch = root / "scratch"
            with self._envs(official, scratch):
                dirs = av_auto._candidate_frames_dirs("ABP-073")
        self.assertEqual(dirs[0], official / "ABP-073")
        self.assertEqual(dirs[1], scratch / "ABP-073")

    def test_process_writes_five_titles_to_the_official_frames_dir(self):
        calls: list = []

        def review_of(code):
            if code == "ABP-313":
                return ("pass", "fail", "disagree", "uncertain")
            return ("pass", "pass", "agree", "pass")

        def fake_clean(args):
            calls.append(("clean", args.code))
            return 0

        def fake_strm(args):
            calls.append(("strm", tuple(args.codes)))
            return 0

        def fake_frames(args):
            calls.append(("frames", args.code, str(args.out)))
            folder = Path(args.out)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{args.code}_frame_120s.jpg"
            (folder / name).write_bytes(b"\xff\xd8" + b"\x00" * 900)
            manifest = {
                "code": args.code,
                "precheck": True,
                "shots": [{"label": "frame_120s", "ss": 120, "ok": True, "valid": True, "file": name}],
            }
            (folder / "frames_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return 0

        def fake_review(args):
            calls.append(("review", args.code, str(args.out)))
            pair = review_of(args.code)
            (Path(args.out) / "review.json").write_text(
                json.dumps(_review(args.code, pair[0], pair[1], pair[2], pair[3])),
                encoding="utf-8",
            )
            return 0

        def fake_grok(args):
            calls.append(("grok", args.code, str(args.frames_dir)))
            (Path(args.frames_dir) / "grok_review.json").write_text(
                json.dumps({"code": args.code, "trigger": "disagree", "verdict": "fail", "role": "tiebreak"}),
                encoding="utf-8",
            )
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "official"
            scratch = root / "scratch"
            with self._envs(official, scratch), \
                 mock.patch.object(av_auto, "_offline_layout", side_effect=lambda code: self._layout(code, inbox=True, keepers=0)), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=False), \
                 mock.patch.object(av_auto, "cmd_clean", side_effect=fake_clean), \
                 mock.patch.object(av_auto, "cmd_strm", side_effect=fake_strm), \
                 mock.patch.object(av_auto, "cmd_frames", side_effect=fake_frames), \
                 mock.patch.object(av_auto, "cmd_review", side_effect=fake_review), \
                 mock.patch.object(av_auto, "cmd_grok_review", side_effect=fake_grok), \
                 mock.patch.object(av_auto, "cmd_submit", side_effect=AssertionError("submit")), \
                 mock.patch.object(av_auto, "cmd_promote", side_effect=AssertionError("promote")):
                rc, out, err = _run_main(["process", *FIVE])
                self.assertEqual(rc, 0, err)
                self.assertEqual(err, "")
                self.assertIn("PROCESS 全部完成: " + " ".join(FIVE), out)
                for code in FIVE:
                    folder = official / code
                    self.assertTrue(av_auto._frames_ready(folder), code)
                    self.assertIsNotNone(av_auto._complete_review(folder), code)
                    self.assertTrue((folder / f"{code}_frame_120s.jpg").stat().st_size > 500)
                    done = out.index(f"PROCESS {code}: 完成")
                    self.assertLess(out.rindex(f"persist {code}:", 0, done), done)
                    self.assertIn(str(folder), out)
                    frame_calls = [item for item in calls if item[0] == "frames" and item[1] == code]
                    self.assertEqual(frame_calls[0][2], str(folder))
                    self.assertFalse((scratch / code).exists(), code)
                review_313 = (official / "ABP-313" / "review.json").read_bytes()
                grok_313 = (official / "ABP-313" / "grok_review.json").read_bytes()
                self.assertNotEqual(review_313, grok_313)
                self.assertNotIn(b"cursor/grok-4.7", review_313)
                self.assertEqual(json.loads(grok_313)["trigger"], "disagree")
                for code in ("ABP-073", "ABP-108", "ABP-246", "ABP-383"):
                    self.assertFalse((official / code / "grok_review.json").exists(), code)

                calls.clear()
                planted = scratch / "ABP-073"
                self._seed(planted, "ABP-073", _review("ABP-073", "fail", "fail", "agree", "fail"))
                kept = (official / "ABP-073" / "review.json").read_bytes()
                with mock.patch.object(av_auto, "_offline_layout", side_effect=lambda code: self._layout(code, inbox=False, keepers=1)), \
                     mock.patch.object(av_auto, "_staging_strm_ready", return_value=True):
                    rc, out, err = _run_main(["process", "ABP-073"])
                    self.assertEqual(rc, 0, err)
                    self.assertEqual([item[0] for item in calls], [])
                    self.assertEqual((official / "ABP-073" / "review.json").read_bytes(), kept)
                    self.assertIn("SKIP frames ABP-073", out)
                    shutil.rmtree(scratch)
                    calls.clear()
                    rc, out, err = _run_main(["process", *FIVE])
                self.assertEqual(rc, 0, err)
                self.assertEqual([item[0] for item in calls], [])
                self.assertEqual((official / "ABP-073" / "review.json").read_bytes(), kept)
                self.assertTrue((official / "ABP-313" / "grok_review.json").is_file())

    def test_tmp_only_artifacts_are_copied_before_success(self):
        calls: list = []

        def boom(*_args, **_kwargs):
            raise AssertionError("tmp leftovers must be reused, not extracted again")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "official"
            scratch = root / "scratch"
            for code in FIVE:
                review = _review(code, "fail", "fail", "agree", "fail")
                grok = None
                if code == "ABP-246":
                    review = _review(code, "uncertain", "uncertain", "agree", "uncertain")
                    grok = {"code": code, "trigger": "uncertain", "verdict": "pass"}
                self._seed(scratch / code, code, review, grok)
            expected = {
                code: (scratch / code / "review.json").read_bytes()
                for code in FIVE
            }
            with self._envs(official, scratch), \
                 mock.patch.object(av_auto, "_offline_layout", side_effect=lambda code: self._layout(code, inbox=False, keepers=1)), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=True), \
                 mock.patch.object(av_auto, "cmd_frames", side_effect=boom), \
                 mock.patch.object(av_auto, "cmd_review", side_effect=boom), \
                 mock.patch.object(av_auto, "cmd_grok_review", side_effect=boom), \
                 mock.patch.object(av_auto, "cmd_clean", side_effect=lambda args: calls.append(args.code) or 0), \
                 mock.patch.object(av_auto, "cmd_strm", side_effect=boom):
                rc, out, err = _run_main(["process", *FIVE])
            self.assertEqual(rc, 0, err)
            self.assertNotIn("PROCESS 未完成", err)
            for code in FIVE:
                folder = official / code
                self.assertEqual((folder / "review.json").read_bytes(), expected[code])
                self.assertTrue(av_auto._frames_ready(folder), code)
                done = out.index(f"PROCESS {code}: 完成")
                self.assertLess(out.rindex(f"persist {code}:", 0, done), done)
            self.assertEqual(
                json.loads((official / "ABP-246" / "grok_review.json").read_text(encoding="utf-8"))["verdict"],
                "pass",
            )
            self.assertNotEqual(
                (official / "ABP-246" / "review.json").read_bytes(),
                (official / "ABP-246" / "grok_review.json").read_bytes(),
            )

    def test_persist_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocker = root / "official-blocked"
            blocker.write_text("not a directory", encoding="utf-8")
            scratch = root / "scratch"
            self._seed(scratch / "ABP-073", "ABP-073", _review("ABP-073", "pass", "pass", "agree", "pass"))
            with self._envs(blocker, scratch), \
                 mock.patch.object(av_auto, "_offline_layout", side_effect=lambda code: self._layout(code, inbox=False, keepers=1)), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=True), \
                 mock.patch.object(av_auto, "cmd_frames", side_effect=AssertionError("frames")), \
                 mock.patch.object(av_auto, "cmd_review", side_effect=AssertionError("review")), \
                 mock.patch.object(av_auto, "cmd_submit", side_effect=AssertionError("submit")), \
                 mock.patch.object(av_auto, "cmd_promote", side_effect=AssertionError("promote")):
                rc, out, err = _run_main(["process", "ABP-073", "ABP-108"])
            self.assertEqual(rc, 1)
            self.assertIn("FAIL ABP-073", err)
            self.assertIn("持久目录", err)
            self.assertNotIn("PROCESS ABP-073: 完成", out)
            self.assertNotIn("PROCESS 全部完成", out)
            self.assertTrue((scratch / "ABP-073" / "review.json").is_file())

    def test_frames_and_review_entries_persist_before_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            official = root / "official"
            scratch = root / "scratch"
            strm = root / "ABP-388.strm"
            strm.write_text("http://example.test/play\n", encoding="utf-8")

            def fake_ff(args, timeout):
                self.assertIn("ffprobe", args)
                return subprocess.CompletedProcess(args, 0, stdout='{"format":{"duration":"200"}}', stderr="")

            def fake_extract(media, dest, ss, session=None):
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"\xff\xd8" + b"\x00" * 800)
                return {"ss": ss, "ok": True, "degraded": False, "bytes": dest.stat().st_size, "error": ""}

            def fake_precheck(*_args, **_kwargs):
                return {"valid": True, "reason": "ok", "confidence": "mid", "uncertain": False}

            runtime = {
                "url": "http://127.0.0.1:9",
                "key": "not-a-real-key",
                "precheck_model": av_auto.PRECHECK_MODEL,
                "review_models": list(av_auto.REVIEW_MODELS),
            }
            with self._envs(official, scratch), \
                 mock.patch.object(av_auto, "_paths", return_value={"staging_strm": root, "official_strm": root}), \
                 mock.patch.object(av_auto, "_resolve_cdn", side_effect=RuntimeError("offline")), \
                 mock.patch.object(av_auto, "open_fmp4_session", return_value=None), \
                 mock.patch.object(av_auto, "_run_ff", side_effect=fake_ff), \
                 mock.patch.object(av_auto, "_extract_one", side_effect=fake_extract), \
                 mock.patch.object(av_auto, "_load_llm_runtime", return_value=runtime), \
                 mock.patch.object(av_auto, "_precheck_image", side_effect=fake_precheck):
                rc, out, err = _run_main(["frames", "ABP-388", "--strm", str(strm)])
            self.assertEqual(rc, 0, err)
            folder = official / "ABP-388"
            self.assertTrue(av_auto._frames_ready(folder), err)
            self.assertLess(out.index("persist ABP-388:"), out.index("frames 抽出"))
            self.assertFalse((scratch / "ABP-388").exists())

            work = scratch / "ABP-388"
            self._seed(work, "ABP-388", _review("ABP-388", "pass", "pass", "agree", "pass"))
            (work / "review.json").unlink()
            chats: list = []

            def fake_chat(*_args, **_kwargs):
                chats.append(1)
                return {
                    "ok": True,
                    "parsed": {"verdict": "pass", "hits": [], "reason": "ok", "confidence": "mid"},
                    "usage": {},
                    "elapsed_s": 0,
                    "http": 200,
                    "attempts": 1,
                }

            with self._envs(official, scratch), \
                 mock.patch.object(av_auto, "_load_llm_runtime", return_value=runtime), \
                 mock.patch.object(av_auto, "_chat_retry", side_effect=fake_chat):
                rc, out, err = _run_main(["review", "ABP-388", "--out", str(work), "--cleanup"])
            self.assertEqual(rc, 0, err)
            self.assertEqual(len(chats), 2)
            official_review = official / "ABP-388" / "review.json"
            self.assertIsNotNone(av_auto._complete_review(official / "ABP-388"))
            self.assertLess(out.index("persist ABP-388:"), out.index("agree/pass"))
            self.assertIn(str(official_review), out)
            self.assertTrue((official / "ABP-388" / "ABP-388_frame_120s.jpg").is_file())
            self.assertFalse((work / "ABP-388_frame_120s.jpg").exists())
            self.assertTrue((work / "review.json").is_file())

            blocker = root / "blocked"
            blocker.write_text("nope", encoding="utf-8")
            self._seed(work, "ABP-108", _review("ABP-108", "pass", "pass", "agree", "pass"))
            (work / "review.json").unlink()
            with self._envs(blocker, scratch), \
                 mock.patch.object(av_auto, "_load_llm_runtime", return_value=runtime), \
                 mock.patch.object(av_auto, "_chat_retry", side_effect=fake_chat):
                rc, out, err = _run_main(["review", "ABP-108", "--out", str(work)])
            self.assertEqual(rc, 1)
            self.assertNotIn("agree/pass", out)
            self.assertIn("持久目录", err)


if __name__ == "__main__":
    unittest.main()
