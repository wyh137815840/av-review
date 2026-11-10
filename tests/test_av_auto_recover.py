"""Official-entry recovery: pending strm, ENOSPC wording, llm_runtime lifetime.

These tests never call 115, Magpie, ffmpeg, or a real Unraid host.
No API keys are written into fixtures.
"""
from __future__ import annotations

import errno
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

GIB = 1024 ** 3
FLOOR_10GIB_KB = 10 * 1024 * 1024  # 10485760 KB, the value media.cfg was set to

FAKE_SSH = r'''#!/usr/bin/env python3
import fcntl
import os
import re
import sys
import time
from pathlib import Path

argv_s = " ".join(sys.argv[1:])
stdin = sys.stdin.read()
mirror = Path(os.environ["HOST_MIRROR"])
seen = Path(os.environ["SEEN_DIR"])
seen.mkdir(parents=True, exist_ok=True)
apply_log = Path(os.environ["APPLY_LOG"])


def host_to_local(path: str) -> Path:
    path = path.strip().strip("'\"")
    prefix = "/mnt/user/appdata/moviepilot"
    if path.startswith(prefix):
        return mirror / path[len(prefix):].lstrip("/")
    if path.startswith("/config/"):
        return mirror / "config" / path[len("/config/"):]
    return mirror / "other" / path.lstrip("/")


def runtime_paths(text: str):
    found = []
    for raw in re.findall(r"/mnt/user/appdata/moviepilot\S*llm_runtime\S+", text):
        found.append(raw.strip().strip("'\""))
    return found


if os.environ.get("SHARE_FLOOR_FAIL") == "1" and "shareFloor" in argv_s:
    sys.exit(int(os.environ.get("PROBE_EXIT", "1")))

if "bash -s" in argv_s:
    for path in runtime_paths(stdin):
        dest = host_to_local(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("{}\n", encoding="utf-8")
    sys.exit(0)

if "docker exec" in argv_s and "av_auto.py" in argv_s:
    expect = int(os.environ.get("EXPECT_RUNS", "1"))
    (seen / f"entered-{os.getpid()}").write_text("1", encoding="utf-8")
    deadline = time.time() + 10

    def count(prefix: str) -> int:
        return len(list(seen.glob(prefix + "*")))

    while time.time() < deadline and count("entered-") < expect:
        time.sleep(0.05)
    match = re.search(r"AV_AUTO_LLM_FILE=(\S+)", argv_s)
    container = match.group(1) if match else ""
    local = host_to_local(container) if container else None
    state = "ok" if local is not None and local.is_file() else "missing"
    (seen / f"saw-{os.getpid()}").write_text(state + " " + container + "\n", encoding="utf-8")
    (seen / f"checked-{os.getpid()}").write_text("1", encoding="utf-8")
    while time.time() < deadline and count("checked-") < expect:
        time.sleep(0.05)
    sys.exit(int(os.environ.get("DOCKER_RC", "0")))

if "rm -f" in argv_s:
    for path in runtime_paths(argv_s):
        host_to_local(path).unlink(missing_ok=True)
    sys.exit(0)

if "av_auto_apply_fs" in argv_s:
    apply_log.parent.mkdir(parents=True, exist_ok=True)
    with apply_log.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write("apply\n")
        handle.flush()
    sys.exit(int(os.environ.get("APPLY_RC", "0")))

if "shareFloor" in argv_s:
    sys.stdout.write(os.environ.get("PROBE_STDOUT", ""))
    sys.exit(0)

sys.exit(0)
'''


class StopAfterStrm(Exception):
    pass


def _job(root: Path, name: str, kind: str, path: str, data: str | None = None) -> Path:
    job = root / name
    job.mkdir()
    (job / "kind").write_text(kind, encoding="utf-8")
    (job / "path").write_text(path, encoding="utf-8")
    if data is not None:
        (job / "data").write_text(data, encoding="utf-8")
    return job


class EnospcAndPendingTests(unittest.TestCase):
    def test_queue_format_is_kind_path_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = io.StringIO()
            with mock.patch.object(av_auto, "DROP_ROOT", root), \
                 mock.patch.object(av_auto, "_stat_free_bytes", return_value=213 * GIB), \
                 redirect_stdout(out):
                av_auto._queue_fs(
                    "write_text",
                    Path("/media/av-auto/ABP-073/ABP-073.strm"),
                    "http://example.test/play\n",
                )
            jobs = [p for p in root.iterdir() if p.is_dir()]
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual((job / "kind").read_text(encoding="utf-8"), "write_text")
            self.assertEqual(
                (job / "path").read_text(encoding="utf-8"),
                "/media/av-auto/ABP-073/ABP-073.strm",
            )
            self.assertEqual((job / "data").read_text(encoding="utf-8"), "http://example.test/play\n")

    def test_stale_floor_is_not_called_a_full_disk(self):
        note = av_auto.classify_enospc(
            free_bytes=213 * GIB,
            configured_floor_bytes=FLOOR_10GIB_KB * 1024,
            cfg_mtime=200,
            shfs_start=100,
            path="/media/av-auto/ABP-073/ABP-073.strm",
        )
        self.assertEqual(note["kind"], "stale_floor")
        self.assertIn("不是磁盘满", note["message"])
        self.assertIn("还没吃到", note["message"])
        self.assertIn("shareFloor", note["message"])
        self.assertNotIn("这是磁盘满", note["message"])

        live = av_auto.classify_enospc(
            free_bytes=213 * GIB,
            configured_floor_bytes=FLOOR_10GIB_KB * 1024,
            live_floor_bytes=220 * GIB,
            cfg_mtime=100,
            shfs_start=200,
            path="/media/av-auto/ABP-073",
        )
        self.assertEqual(live["kind"], "stale_floor")
        self.assertIn("高于配置", live["message"])
        self.assertNotIn("这是磁盘满", live["message"])

    def test_reservation_and_full_disk_stay_distinct(self):
        reserved = av_auto.classify_enospc(
            free_bytes=5 * GIB,
            configured_floor_bytes=FLOOR_10GIB_KB * 1024,
            live_floor_bytes=FLOOR_10GIB_KB * 1024,
            cfg_mtime=100,
            shfs_start=200,
        )
        self.assertEqual(reserved["kind"], "reservation")
        self.assertIn("不是磁盘满", reserved["message"])
        self.assertIn("预留", reserved["message"])
        self.assertNotIn("还没吃到", reserved["message"])
        self.assertNotIn("这是磁盘满", reserved["message"])

        full = av_auto.classify_enospc(
            free_bytes=100 * 1024 * 1024,
            configured_floor_bytes=FLOOR_10GIB_KB * 1024,
            cfg_mtime=200,
            shfs_start=100,
        )
        self.assertEqual(full["kind"], "full")
        self.assertIn("这是磁盘满", full["message"])
        self.assertNotIn("不是磁盘满", full["message"])

        unknown_floor = av_auto.classify_enospc(free_bytes=213 * GIB)
        self.assertEqual(unknown_floor["kind"], "not_full")
        self.assertIn("不是磁盘满", unknown_floor["message"])
        self.assertNotIn("这是磁盘满", unknown_floor["message"])

        blind = av_auto.classify_enospc(free_bytes=None)
        self.assertEqual(blind["kind"], "unknown")
        self.assertIn("不要把它当成磁盘满", blind["message"])
        self.assertNotIn("这是磁盘满", blind["message"])

    def test_media_write_prints_the_stale_floor_note(self):
        real_mkdir = Path.mkdir

        def mkdir(self, *args, **kwargs):
            if str(self).startswith("/media/"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_mkdir(self, *args, **kwargs)

        env = {
            "AV_AUTO_SHARE_FLOOR_KB": str(FLOOR_10GIB_KB),
            "AV_AUTO_SHARE_CFG_MTIME": "200",
            "AV_AUTO_SHFS_START": "100",
            "AV_AUTO_LIVE_FLOOR_KB": "",
            "AV_AUTO_SHARE_CFG": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch.object(av_auto, "DROP_ROOT", Path(tmp)), \
                 mock.patch.object(av_auto, "_stat_free_bytes", return_value=213 * GIB), \
                 mock.patch.object(Path, "mkdir", mkdir), \
                 mock.patch.dict(os.environ, env, clear=False), \
                 redirect_stdout(out):
                av_auto.media_write_text(
                    Path("/media/av-auto/ABP-073/ABP-073.strm"),
                    "http://example.test/play\n",
                )
        text = out.getvalue()
        self.assertIn("不是磁盘满", text)
        self.assertIn("还没吃到", text)
        self.assertIn("/media/av-auto/ABP-073/ABP-073.strm", text)
        self.assertNotIn("这是磁盘满", text)
        self.assertNotIn("shfs 未吃到新 shareFloor），已排队", text)

    def test_cfg_file_mtime_marks_a_stale_floor(self):
        self.assertEqual(av_auto.parse_share_floor_kb('shareFloor="10485760"\n'), FLOOR_10GIB_KB)
        self.assertEqual(av_auto.parse_share_floor_kb("shareFloor=10485760\n"), FLOOR_10GIB_KB)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "media.cfg"
            cfg.write_text('shareFloor="10485760"\n', encoding="utf-8")
            os.utime(cfg, (200, 200))
            env = {
                "AV_AUTO_SHARE_CFG": str(cfg),
                "AV_AUTO_SHARE_FLOOR_KB": "",
                "AV_AUTO_LIVE_FLOOR_KB": "",
                "AV_AUTO_SHARE_CFG_MTIME": "",
                "AV_AUTO_SHFS_START": "50",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                note = av_auto.classify_enospc(
                    free_bytes=213 * GIB,
                    configured_floor_bytes=av_auto._configured_floor_bytes(),
                    cfg_mtime=av_auto._cfg_mtime(),
                    shfs_start=av_auto._shfs_start(),
                )
        self.assertEqual(note["kind"], "stale_floor")
        self.assertIn("不是磁盘满", note["message"])

    def test_apply_pending_lands_a_media_write_and_drops_the_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "pending"
            queue.mkdir()
            target = root / "library" / "ABP-073" / "ABP-073.strm"
            _job(queue, "100", "mkdir", str(target.parent))
            _job(queue, "200", "write_text", str(target), "http://example.test/play\n")

            def is_media(path: Path) -> bool:
                return str(path).startswith(str(root / "library"))

            with mock.patch.object(av_auto, "DROP_ROOT", queue), \
                 mock.patch.object(av_auto, "_is_media_path", side_effect=is_media):
                applied, blocked = av_auto.apply_pending_queue()
            self.assertEqual((applied, blocked), (2, 0))
            self.assertEqual(target.read_text(encoding="utf-8"), "http://example.test/play\n")
            self.assertEqual(list(queue.iterdir()), [])

    def test_apply_pending_keeps_the_job_when_the_write_returns_enospc(self):
        real_write = Path.write_text

        def write_text(self, *args, **kwargs):
            if self.name == "ABP-073.strm":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(self, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "pending"
            queue.mkdir()
            target = root / "library" / "ABP-073" / "ABP-073.strm"
            job = _job(queue, "200", "write_text", str(target), "http://example.test/play\n")

            def is_media(path: Path) -> bool:
                return str(path).startswith(str(root / "library"))

            with mock.patch.object(av_auto, "DROP_ROOT", queue), \
                 mock.patch.object(av_auto, "_is_media_path", side_effect=is_media), \
                 mock.patch.object(Path, "write_text", write_text):
                applied, blocked = av_auto.apply_pending_queue()
            self.assertEqual((applied, blocked), (0, 1))
            self.assertTrue(job.is_dir())
            self.assertFalse(target.exists())

    def test_apply_pending_does_not_touch_a_non_media_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "pending"
            queue.mkdir()
            outside = root / "not-media" / "note.txt"
            job = _job(queue, "200", "write_text", str(outside), "http://example.test/play\n")
            with mock.patch.object(av_auto, "DROP_ROOT", queue):
                applied, blocked = av_auto.apply_pending_queue()
            self.assertEqual((applied, blocked), (0, 0))
            self.assertTrue(job.is_dir())
            self.assertFalse(outside.exists())

    def test_pending_strm_is_not_reported_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "pending"
            queue.mkdir()
            _job(
                queue,
                "200",
                "write_text",
                "/media/av-auto/ABP-073/ABP-073.strm",
                "http://example.test/play\n",
            )
            _job(
                queue,
                "100",
                "write_text",
                "/media/av-auto/ABP-108/ABP-108.strm",
                "http://example.test/other\n",
            )
            paths = {"staging_strm": Path("/media/av-auto"), "official_strm": Path("/media/AV")}
            out = io.StringIO()
            err = io.StringIO()
            env = {"AV_AUTO_PENDING_STRM_ROOT": str(root / "materialized")}
            with mock.patch.object(av_auto, "DROP_ROOT", queue), \
                 mock.patch.object(av_auto, "apply_pending_queue", return_value=(0, 1)), \
                 mock.patch.object(av_auto, "_find_strm_for_code", return_value=None), \
                 mock.patch.object(av_auto, "_paths", return_value=paths), \
                 mock.patch.object(av_auto, "_read_strm_url", side_effect=StopAfterStrm), \
                 mock.patch.dict(os.environ, env, clear=False), \
                 redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(StopAfterStrm):
                    av_auto.cmd_frames(SimpleNamespace(
                        code="ABP-073",
                        strm="",
                        out=str(root / "frames"),
                        cleanup=False,
                        no_precheck=True,
                        no_reuse=True,
                    ))
            self.assertNotIn("找不到", err.getvalue())
            self.assertNotIn("找不到", out.getvalue())
            self.assertIn("不报缺失", out.getvalue())
            materialized = root / "materialized" / "ABP-073" / "ABP-073.strm"
            self.assertIn("http://example.test/play", materialized.read_text(encoding="utf-8"))
            self.assertNotIn("other", materialized.read_text(encoding="utf-8"))

            err2 = io.StringIO()
            with mock.patch.object(av_auto, "DROP_ROOT", root / "empty"), \
                 mock.patch.object(av_auto, "apply_pending_queue", return_value=(0, 0)), \
                 mock.patch.object(av_auto, "_find_strm_for_code", return_value=None), \
                 mock.patch.object(av_auto, "_paths", return_value=paths), \
                 redirect_stderr(err2):
                with self.assertRaises(SystemExit) as caught:
                    av_auto._strm_for_frames("ABP-246", paths)
            self.assertEqual(caught.exception.code, 1)
            self.assertIn("找不到 ABP-246 的 strm", err2.getvalue())

    def test_process_does_not_requeue_when_pending_already_has_the_strm(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp) / "pending"
            queue.mkdir()
            _job(
                queue,
                "200",
                "write_text",
                "/media/av-auto/ABP-073/ABP-073.strm",
                "http://example.test/play\n",
            )
            with mock.patch.object(av_auto, "DROP_ROOT", queue), \
                 mock.patch.object(av_auto, "apply_pending_queue", return_value=(0, 1)), \
                 mock.patch.object(av_auto, "_staging_strm_ready", return_value=False), \
                 mock.patch.object(av_auto, "cmd_strm", side_effect=AssertionError("requeued")):
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = av_auto._process_strm("ABP-073")
            self.assertEqual(rc, 0)
            self.assertIn("已在 pending", out.getvalue())
            self.assertNotIn("找不到", out.getvalue())


class OfficialEntryTests(unittest.TestCase):
    def _run(self, tmp: Path, args: list[str], extra: dict | None = None):
        fake_dir = tmp / "bin"
        fake_dir.mkdir()
        ssh = fake_dir / "ssh"
        ssh.write_text(FAKE_SSH, encoding="utf-8")
        ssh.chmod(0o755)
        mirror = tmp / "mirror"
        seen = tmp / "seen"
        mirror.mkdir()
        seen.mkdir()
        apply_log = tmp / "apply.log"
        env = os.environ.copy()
        env["PATH"] = str(fake_dir) + os.pathsep + env.get("PATH", "")
        env["HOST_MIRROR"] = str(mirror)
        env["SEEN_DIR"] = str(seen)
        env["APPLY_LOG"] = str(apply_log)
        env.update(extra or {})
        result = subprocess.run(
            ["bash", str(ROOT / "bin" / "av_auto"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=25,
        )
        return result, mirror, seen, apply_log

    def _saw(self, seen: Path) -> list[str]:
        return [
            path.read_text(encoding="utf-8").strip()
            for path in sorted(seen.glob("saw-*"))
        ]

    def test_apply_fs_still_runs_when_process_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _mirror, seen, apply_log = self._run(
                Path(tmp),
                ["process", "ABP-073"],
                {"DOCKER_RC": "1", "APPLY_RC": "0"},
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(apply_log.read_text(encoding="utf-8").count("apply\n"), 1)
            saw = self._saw(seen)
            self.assertEqual(len(saw), 1)
            self.assertTrue(saw[0].startswith("ok "), saw)
            self.assertNotRegex(saw[0], r"llm_runtime\.json$")
            self.assertRegex(saw[0], r"llm_runtime\.[^/\s]+\.json$")
            self.assertNotIn("sk-", result.stdout + result.stderr)

    def test_apply_fs_failure_is_reported_when_python_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _mirror, _seen, apply_log = self._run(
                Path(tmp),
                ["process", "ABP-073"],
                {"DOCKER_RC": "0", "APPLY_RC": "4"},
            )
            self.assertEqual(result.returncode, 4, result.stderr)
            self.assertIn("av_auto_apply_fs 退出 4", result.stderr)
            self.assertEqual(apply_log.read_text(encoding="utf-8").count("apply\n"), 1)

    def test_overlapping_official_runs_keep_their_own_llm_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_dir = root / "bin"
            fake_dir.mkdir()
            ssh = fake_dir / "ssh"
            ssh.write_text(FAKE_SSH, encoding="utf-8")
            ssh.chmod(0o755)
            mirror = root / "mirror"
            seen = root / "seen"
            mirror.mkdir()
            seen.mkdir()
            apply_log = root / "apply.log"
            env = os.environ.copy()
            env["PATH"] = str(fake_dir) + os.pathsep + env.get("PATH", "")
            env["HOST_MIRROR"] = str(mirror)
            env["SEEN_DIR"] = str(seen)
            env["APPLY_LOG"] = str(apply_log)
            env["EXPECT_RUNS"] = "2"
            env["DOCKER_RC"] = "0"
            env["APPLY_RC"] = "0"
            command = ["bash", str(ROOT / "bin" / "av_auto"), "process", "ABP-073"]
            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            first_out = first.communicate(timeout=25)
            second_out = second.communicate(timeout=25)
            self.assertEqual(first.returncode, 0, first_out[1])
            self.assertEqual(second.returncode, 0, second_out[1])
            saw = self._saw(seen)
            self.assertEqual(len(saw), 2, saw)
            self.assertTrue(all(line.startswith("ok ") for line in saw), saw)
            paths = [line.split(" ", 1)[1] for line in saw]
            self.assertEqual(len(set(paths)), 2, paths)
            for path in paths:
                self.assertNotRegex(path, r"llm_runtime\.json$")
                self.assertRegex(path, r"/llm_runtime\.[^/]+\.json$")
            left = list(mirror.rglob("llm_runtime*"))
            self.assertEqual(left, [])
            self.assertEqual(apply_log.read_text(encoding="utf-8").count("apply\n"), 2)
            blob = first_out[0] + first_out[1] + second_out[0] + second_out[1]
            self.assertNotIn("sk-", blob)

    def test_share_floor_helper_is_diagnostic_only(self):
        stale = "floor=10485760\nmtime=200\nstart=100\nfree=228698386432\n"
        with tempfile.TemporaryDirectory() as tmp:
            result, _mirror, _seen, apply_log = self._run(
                Path(tmp),
                ["share-floor"],
                {"PROBE_STDOUT": stale},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("不是磁盘满", result.stdout)
            self.assertIn("过期 floor", result.stdout)
            self.assertIn("不会 reload shfs", result.stdout)
            self.assertNotIn("这是磁盘满", result.stdout)
            self.assertFalse(apply_log.exists())
            self.assertNotIn("docker exec", result.stdout)

        reserved = "floor=10485760\nmtime=100\nstart=300\nfree=5368709120\n"
        with tempfile.TemporaryDirectory() as tmp:
            result, _mirror, _seen, apply_log = self._run(
                Path(tmp),
                ["share-floor"],
                {"PROBE_STDOUT": reserved},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("预留", result.stdout)
            self.assertIn("不是磁盘满", result.stdout)
            self.assertNotIn("过期 floor", result.stdout)
            self.assertFalse(apply_log.exists())

        with tempfile.TemporaryDirectory() as tmp:
            result, _mirror, _seen, apply_log = self._run(
                Path(tmp),
                ["share-floor"],
                {"SHARE_FLOOR_FAIL": "1", "PROBE_EXIT": "9"},
            )
            self.assertEqual(result.returncode, 9, result.stderr)
            self.assertIn("没有 reload", result.stderr)
            self.assertFalse(apply_log.exists())

    def test_wrapper_does_not_reload_shfs_or_bake_a_key(self):
        text = (ROOT / "bin" / "av_auto").read_text(encoding="utf-8")
        for banned in ("killall", "SIGHUP", "rc.disk", "emhttp"):
            self.assertNotIn(banned, text)
        self.assertNotIn("sk-", text)
        self.assertNotRegex(text, r"api_key\"\s*:\s*\"")
        self.assertIn("av_auto_apply_fs", text)
        self.assertIn("AV_AUTO_LLM_FILE", text)
        syntax = subprocess.run(["bash", "-n", str(ROOT / "bin" / "av_auto")], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == "__main__":
    unittest.main()
