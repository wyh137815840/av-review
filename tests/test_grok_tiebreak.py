"""Grok tiebreaker for av_auto dual-model reviews.

The default review path still rejects Grok and MiniMax. These tests never
call Magpie.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

DS = "deepseek/deepseek-flash"
LU = "chatgpt-api/gpt-6-luna"
SECRET = "unit-test-magpie-key-do-not-write"


def _jpeg(path: Path, size: int = 800) -> None:
    path.write_bytes(b"\xff\xd8" + b"\x00" * max(size - 2, 1))


def _shot(name: str, *, valid: bool = True, ok: bool = True, label: str = "frame_120s", ss: int = 120, discarded: bool = False) -> dict:
    return {
        "label": label,
        "ss": ss,
        "ok": ok,
        "valid": valid,
        "file": name,
        "discarded": discarded,
    }


def _review(ds: str, lu: str, agreement: str, consensus: str, frames: list[str]) -> dict:
    def row(verdict: str) -> dict:
        return {"ok": True, "verdict": verdict, "hits": [], "reason": verdict, "confidence": "mid"}

    return {
        "code": "ABP-123",
        "valid_frames": [{"label": "frame_120s", "ss": 120, "file": name} for name in frames],
        "models": {DS: row(ds), LU: row(lu)},
        "agreement": agreement,
        "consensus": consensus,
    }


def _manifest(shots: list[dict]) -> dict:
    return {"code": "ABP-123", "precheck": True, "shots": shots}


class TiebreakDecisionTests(unittest.TestCase):
    def test_trigger_matrix(self):
        self.assertEqual(av_auto.tiebreak_trigger(_review("pass", "fail", "disagree", "uncertain", [])), "disagree")
        self.assertEqual(av_auto.tiebreak_trigger(_review("uncertain", "uncertain", "agree", "uncertain", [])), "uncertain")
        self.assertEqual(av_auto.tiebreak_trigger(_review("pass", "uncertain", "partial", "uncertain", [])), "uncertain")
        self.assertEqual(av_auto.tiebreak_trigger(_review("fail", "uncertain", "partial", "uncertain", [])), "uncertain")
        self.assertEqual(av_auto.tiebreak_trigger(_review("pass", "pass", "agree", "pass", [])), "")
        self.assertEqual(av_auto.tiebreak_trigger(_review("fail", "fail", "agree", "fail", [])), "")
        # Model rows win over a stale consensus label.
        self.assertEqual(av_auto.tiebreak_trigger(_review("pass", "pass", "disagree", "uncertain", [])), "")
        self.assertEqual(av_auto.tiebreak_trigger(_review("pass", "fail", "agree", "pass", [])), "disagree")
        self.assertFalse(av_auto.review_needs_tiebreak(_review("pass", "pass", "agree", "pass", [])))
        self.assertTrue(av_auto.review_needs_tiebreak(_review("pass", "fail", "disagree", "uncertain", [])))

    def test_grok_pass_or_fail_is_kept(self):
        split = {DS: "pass", LU: "fail"}
        kept = av_auto.decide_tiebreak(split, "fail")
        self.assertEqual(kept["kept_verdict"], "fail")
        self.assertEqual(kept["kept_from"], av_auto.GROK_TIEBREAK_MODEL)
        self.assertEqual(kept["aligned_with"], [LU])
        self.assertIn("保留 fail", kept["conclusion"])
        self.assertTrue(kept["resolved"])

        both = av_auto.decide_tiebreak({DS: "uncertain", LU: "uncertain"}, "pass")
        self.assertEqual(both["kept_verdict"], "pass")
        self.assertIn("两家均为 uncertain", both["conclusion"])
        self.assertIn("保留 pass", both["conclusion"])

        partial = av_auto.decide_tiebreak({DS: "pass", LU: "uncertain"}, "fail")
        self.assertEqual(partial["kept_verdict"], "fail")
        self.assertEqual(partial["kept_from"], "cursor/grok-4.7")
        self.assertEqual(partial["aligned_with"], [])
        same = av_auto.decide_tiebreak({DS: "pass", LU: "uncertain"}, "pass")
        self.assertEqual(same["aligned_with"], [DS])
        self.assertIn("与 deepseek-flash 一致，保留 pass", same["conclusion"])

    def test_grok_uncertain_stays_unresolved(self):
        kept = av_auto.decide_tiebreak({DS: "pass", LU: "fail"}, "uncertain")
        self.assertEqual(kept["kept_verdict"], "uncertain")
        self.assertEqual(kept["kept_from"], "")
        self.assertFalse(kept["resolved"])
        self.assertIn("保留 uncertain", kept["conclusion"])
        other = av_auto.decide_tiebreak({DS: "uncertain", LU: "uncertain"}, "nope")
        self.assertEqual(other["grok_verdict"], "uncertain")
        self.assertEqual(other["kept_verdict"], "uncertain")

    def test_hook_is_generic_and_skips_settled_reviews(self):
        settled = _review("pass", "pass", "agree", "pass", [])
        self.assertIsNone(av_auto.grok_tiebreak_hook(settled, "/tmp/frames/ABP-123", "ABP-123"))
        split = _review("pass", "fail", "disagree", "uncertain", [])
        hook = av_auto.grok_tiebreak_hook(split, "/tmp/frames/ABP-123")
        self.assertEqual(
            hook,
            {
                "needed": True,
                "trigger": "disagree",
                "argv": ["grok-review", "ABP-123", "--frames-dir", "/tmp/frames/ABP-123"],
            },
        )

    def test_prior_rows_drop_grok_and_minimax(self):
        review = _review("pass", "fail", "disagree", "uncertain", [])
        review["models"]["cursor/grok-4.7"] = {"ok": True, "verdict": "pass", "reason": "ignore"}
        review["models"]["minimax-cn/x"] = {"ok": True, "verdict": "fail", "reason": "ignore"}
        prior = av_auto.prior_model_verdicts(review)
        self.assertEqual(set(prior), {DS, LU})


class FrameSelectionTests(unittest.TestCase):
    def test_only_valid_extracted_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = "ABP-123_frame_120s.jpg"
            names = {
                good: 800,
                "ABP-123_Thumb.jpg": 800,
                "Backdrop.jpg": 800,
                "cover.jpg": 800,
                "stills.jpg": 800,
                "ABP-123_frame_600s.jpg": 800,
                "ABP-123_empty.jpg": 100,
                "notes.txt": 800,
            }
            for name, size in names.items():
                _jpeg(root / name, size)
            shots = [
                _shot(good, label="frame_120s", ss=120),
                _shot("ABP-123_Thumb.jpg", label="Thumb", ss=1),
                _shot("Backdrop.jpg", label="Backdrop", ss=2),
                _shot("cover.jpg", label="cover", ss=3),
                _shot("stills.jpg", label="stills", ss=4),
                _shot("ABP-123_frame_600s.jpg", valid=False, label="frame_600s", ss=600),
                _shot("ABP-123_empty.jpg", label="frame_empty", ss=30),
                _shot("notes.txt", label="notes", ss=40),
                _shot("../outside.jpg", label="frame_out", ss=50),
                _shot("ABP-123_discarded.jpg", discarded=True, label="frame_discarded", ss=70),
            ]
            review = _review("pass", "fail", "disagree", "uncertain", [shot["file"] for shot in shots])
            chosen = av_auto.select_tiebreak_frames(root, _manifest(shots), review)
            self.assertEqual([item["file"] for item in chosen], [good])


class DefaultBanTests(unittest.TestCase):
    def test_review_models_and_parser_stay_dual_model(self):
        self.assertEqual(av_auto.REVIEW_MODELS, (DS, LU))
        self.assertIn("grok", av_auto.FORBIDDEN_MODEL_BITS)
        self.assertIn("minimax", av_auto.FORBIDDEN_MODEL_BITS)
        self.assertEqual(av_auto.GROK_TIEBREAK_MODEL, "cursor/grok-4.7")
        parser = av_auto.build_parser()
        review_args = parser.parse_args(["review", "ABP-123"])
        self.assertIs(review_args.func, av_auto.cmd_review)
        self.assertFalse(hasattr(review_args, "frames_dir"))
        grok_args = parser.parse_args(["grok-review", "--frames-dir", "/tmp/frames/ABP-123"])
        self.assertIs(grok_args.func, av_auto.cmd_grok_review)

    def test_default_chat_still_rejects_grok_and_minimax(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("default path posted a banned model")

        with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=boom):
            with self.assertRaises(SystemExit):
                av_auto._magpie_chat({"url": "http://127.0.0.1", "key": "x"}, "cursor/grok-4.7", [], "s", "u", 1, 8)
            with self.assertRaises(SystemExit):
                av_auto._assert_allowed_model("MiniMax-Text")
        self.assertEqual(av_auto._assert_allowed_model(DS), DS)
        self.assertEqual(av_auto._assert_allowed_model(LU), LU)
        with self.assertRaises(SystemExit):
            av_auto._post_magpie_chat({"url": "http://127.0.0.1", "key": "x"}, "minimax-cn/x", [], "s", "u", 1, 8)

    def test_runtime_loader_still_rejects_banned_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm.json"
            path.write_text(json.dumps({"api_key": SECRET, "review_models": ["cursor/grok-4.7"]}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AV_AUTO_LLM_FILE": str(path)}):
                with self.assertRaises(SystemExit):
                    av_auto._load_llm_runtime()
            path.write_text(json.dumps({"api_key": SECRET, "precheck_model": "minimax-cn/x"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AV_AUTO_LLM_FILE": str(path)}):
                with self.assertRaises(SystemExit):
                    av_auto._load_llm_runtime()
            path.write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AV_AUTO_LLM_FILE": str(path)}):
                cfg = av_auto._load_llm_runtime()
            self.assertEqual(tuple(cfg["review_models"]), av_auto.REVIEW_MODELS)
            self.assertEqual(cfg["precheck_model"], av_auto.PRECHECK_MODEL)

    def test_tiebreak_refuses_a_swapped_model_constant(self):
        with mock.patch.object(av_auto, "GROK_TIEBREAK_MODEL", "minimax-cn/x"):
            with self.assertRaises(SystemExit):
                av_auto._grok_tiebreak_once({"url": "http://127.0.0.1", "key": SECRET}, [], "hi")


class CommandTests(unittest.TestCase):
    def _layout(self, root: Path, ds: str, lu: str, agreement: str, consensus: str, *, precheck: bool = True) -> bytes:
        good = "ABP-123_frame_120s.jpg"
        _jpeg(root / good)
        for name in ("ABP-123_Thumb.jpg", "Backdrop.jpg", "cover.jpg", "stills.jpg", "ABP-123_invalid.jpg"):
            _jpeg(root / name)
        _jpeg(root / "ABP-123_empty.jpg", 120)
        shots = [
            _shot(good),
            _shot("ABP-123_Thumb.jpg", label="Thumb", ss=1),
            _shot("Backdrop.jpg", label="Backdrop", ss=2),
            _shot("cover.jpg", label="cover", ss=3),
            _shot("stills.jpg", label="stills", ss=4),
            _shot("ABP-123_invalid.jpg", valid=False, label="frame_600s", ss=600),
            _shot("ABP-123_empty.jpg", label="frame_empty", ss=30),
        ]
        review = _review(ds, lu, agreement, consensus, [shot["file"] for shot in shots])
        manifest = _manifest(shots)
        manifest["precheck"] = precheck
        (root / "review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (root / "frames_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "media_llm_route.json").write_text("{}\n", encoding="utf-8")
        return (root / "review.json").read_bytes()

    def test_skip_settled_review_does_not_call_magpie(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = self._layout(root, "pass", "pass", "agree", "pass")
            (root / "grok_review.json").write_text('{"stale":true}\n', encoding="utf-8")
            stale = (root / "grok_review.json").read_bytes()
            buf = io.StringIO()
            with mock.patch.object(av_auto, "_load_llm_runtime", side_effect=AssertionError("llm")):
                with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=AssertionError("post")):
                    with redirect_stdout(buf):
                        rc = av_auto.cmd_grok_review(argparse_ns(root))
            self.assertEqual(rc, 0)
            self.assertIn("tiebreak SKIP agree/pass", buf.getvalue())
            self.assertIn("已有 grok_review.json 未改动", buf.getvalue())
            self.assertEqual((root / "grok_review.json").read_bytes(), stale)
            self.assertEqual((root / "review.json").read_bytes(), before)

    def test_disagree_writes_sibling_and_keeps_grok_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = self._layout(root, "pass", "fail", "disagree", "uncertain")
            route = (root / "media_llm_route.json").read_bytes()
            calls = []

            def fake_post(cfg, model, images, system, user_text, timeout, max_tokens):
                calls.append(
                    {
                        "model": model,
                        "files": [path.name for path in images],
                        "system": system,
                        "user": user_text,
                        "key": cfg.get("key"),
                    }
                )
                return {
                    "ok": True,
                    "http": 200,
                    "elapsed_s": 0.2,
                    "parsed": {
                        "verdict": "fail",
                        "hits": ["grimace"],
                        "reason": "表情痛苦",
                        "confidence": "high",
                    },
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                }

            buf = io.StringIO()
            with mock.patch.object(av_auto, "_load_llm_runtime", return_value=_runtime()):
                with mock.patch.object(av_auto, "_magpie_chat", side_effect=AssertionError("default chat")):
                    with mock.patch.object(av_auto, "_precheck_image", side_effect=AssertionError("precheck")):
                        with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=fake_post):
                            with redirect_stdout(buf):
                                rc = av_auto.cmd_grok_review(argparse_ns(root))
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["model"], "cursor/grok-4.7")
            self.assertEqual(calls[0]["files"], ["ABP-123_frame_120s.jpg"])
            self.assertIs(calls[0]["system"], av_auto.REVIEW_SYSTEM)
            self.assertIn("有效抽帧", calls[0]["user"])
            self.assertNotIn("ABP-123_Thumb.jpg", calls[0]["user"])
            self.assertNotIn("Backdrop.jpg", calls[0]["user"])
            text = buf.getvalue()
            self.assertIn("tiebreak KEPT fail", text)
            self.assertIn("保留 fail", text)
            doc = json.loads((root / "grok_review.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["verdict"], "fail")
            self.assertEqual(doc["confidence"], "high")
            self.assertEqual(doc["reason"], "表情痛苦")
            self.assertEqual(doc["kept_verdict"], "fail")
            self.assertEqual(doc["grok"]["model"], "cursor/grok-4.7")
            self.assertEqual(doc["tiebreak"]["aligned_with"], [LU])
            self.assertEqual(doc["tiebreak"]["prior_verdicts"], {DS: "pass", LU: "fail"})
            self.assertEqual(doc["valid_n"], 1)
            self.assertNotIn(SECRET, (root / "grok_review.json").read_text(encoding="utf-8"))
            self.assertFalse((root / "grok_review.json.tmp").exists())
            self.assertEqual((root / "review.json").read_bytes(), before)
            self.assertEqual((root / "media_llm_route.json").read_bytes(), route)

    def test_both_uncertain_can_be_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = self._layout(root, "uncertain", "uncertain", "agree", "uncertain")

            def fake_post(_cfg, model, images, _system, _user, _timeout, _max_tokens):
                self.assertEqual(model, "cursor/grok-4.7")
                self.assertEqual([path.name for path in images], ["ABP-123_frame_120s.jpg"])
                return {
                    "ok": True,
                    "http": 200,
                    "parsed": {"verdict": "pass", "reason": "身材和皮肤都符合", "confidence": "mid"},
                    "usage": {},
                }

            with mock.patch.object(av_auto, "_load_llm_runtime", return_value=_runtime()):
                with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=fake_post):
                    with redirect_stdout(io.StringIO()):
                        rc = av_auto.cmd_grok_review(argparse_ns(root, code=""))
            self.assertEqual(rc, 0)
            doc = json.loads((root / "grok_review.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["trigger"], "uncertain")
            self.assertEqual(doc["kept_verdict"], "pass")
            self.assertIn("两家均为 uncertain", doc["conclusion"])
            self.assertEqual((root / "review.json").read_bytes(), before)

    def test_grok_uncertain_records_unresolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._layout(root, "pass", "fail", "disagree", "uncertain")

            def fake_post(*_args, **_kwargs):
                return {"ok": True, "http": 200, "parsed": {"verdict": "uncertain", "reason": "角度不够", "confidence": "low"}}

            buf = io.StringIO()
            with mock.patch.object(av_auto, "_load_llm_runtime", return_value=_runtime()):
                with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=fake_post):
                    with redirect_stdout(buf):
                        rc = av_auto.cmd_grok_review(argparse_ns(root))
            self.assertEqual(rc, 0)
            self.assertIn("tiebreak UNRESOLVED uncertain", buf.getvalue())
            doc = json.loads((root / "grok_review.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["verdict"], "uncertain")
            self.assertEqual(doc["kept_verdict"], "uncertain")
            self.assertFalse(doc["tiebreak"]["resolved"])

    def test_does_not_write_secret_or_unprechecked_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = self._layout(root, "pass", "fail", "disagree", "uncertain")

            def leak(*_args, **_kwargs):
                return {"ok": True, "http": 200, "parsed": {"verdict": "pass", "reason": SECRET, "confidence": "low"}}

            with mock.patch.object(av_auto, "_load_llm_runtime", return_value=_runtime()):
                with mock.patch.object(av_auto, "_post_magpie_chat", side_effect=leak):
                    with self.assertRaises(SystemExit):
                        av_auto.cmd_grok_review(argparse_ns(root))
            self.assertFalse((root / "grok_review.json").exists())
            self.assertFalse((root / "grok_review.json.tmp").exists())
            self.assertEqual((root / "review.json").read_bytes(), before)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._layout(root, "fail", "uncertain", "partial", "uncertain", precheck=False)
            with self.assertRaises(SystemExit):
                av_auto.cmd_grok_review(argparse_ns(root))
            self.assertFalse((root / "grok_review.json").exists())

    def test_code_mismatch_and_missing_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._layout(root, "pass", "fail", "disagree", "uncertain")
            with self.assertRaises(SystemExit):
                av_auto.cmd_grok_review(argparse_ns(root, code="SSIS-001"))
            self.assertFalse((root / "grok_review.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(SystemExit):
                av_auto.cmd_grok_review(argparse_ns(root, code="ABP-123"))


def _runtime() -> dict:
    return {
        "url": "http://127.0.0.1:9",
        "key": SECRET,
        "precheck_model": av_auto.PRECHECK_MODEL,
        "review_models": list(av_auto.REVIEW_MODELS),
    }


def argparse_ns(root: Path, code: str = "ABP-123"):
    return av_auto.build_parser().parse_args(["grok-review", code, "--frames-dir", str(root)] if code else ["grok-review", "--frames-dir", str(root)])


if __name__ == "__main__":
    unittest.main()
