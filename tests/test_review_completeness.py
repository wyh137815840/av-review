"""Review completeness: a failed Magpie call must not count as a finished review.

These tests never call 115, Magpie, ffmpeg, or ssh.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

DS = "deepseek/deepseek-flash"
LU = "chatgpt-api/gpt-6-luna"


class ReviewCompletenessTests(unittest.TestCase):
    def _review(self, rows: dict, extra: dict | None = None) -> dict:
        doc = {"code": "TEST-001", "agreement": "agree", "consensus": "pass", "models": rows}
        if extra:
            doc.update(extra)
        return doc

    def test_complete_review_requires_ok_rows(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "review.json").write_text(json.dumps(self._review({
            DS: {"ok": True, "verdict": "pass"},
            LU: {"ok": True, "verdict": "pass"},
        })), encoding="utf-8")
        self.assertIsNotNone(av_auto._complete_review(folder))

        (folder / "review.json").write_text(json.dumps(self._review({
            DS: {"ok": False, "verdict": "uncertain"},
            LU: {"ok": False, "verdict": "uncertain"},
        }, {"complete": False})), encoding="utf-8")
        self.assertIsNone(av_auto._complete_review(folder))

    def test_complete_review_rejects_partial_failure(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "review.json").write_text(json.dumps(self._review({
            DS: {"ok": True, "verdict": "pass"},
            LU: {"ok": False, "verdict": "uncertain"},
        })), encoding="utf-8")
        self.assertIsNone(av_auto._complete_review(folder))

    def test_grok_failure_not_counted_as_done(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "grok_review.json").write_text(json.dumps({
            "code": "TEST-001", "trigger": "disagree", "verdict": "pass",
            "grok": {"ok": False}, "complete": False,
        }), encoding="utf-8")
        self.assertFalse(av_auto._grok_already_done(folder, "disagree", "TEST-001"))
        (folder / "grok_review.json").write_text(json.dumps({
            "code": "TEST-001", "trigger": "disagree", "verdict": "pass",
            "grok": {"ok": True},
        }), encoding="utf-8")
        self.assertTrue(av_auto._grok_already_done(folder, "disagree", "TEST-001"))

    def test_cmd_review_fails_nonzero_when_models_fail(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "frames_manifest.json").write_text(json.dumps({
            "code": "TEST-002", "precheck": True,
            "shots": [{"label": "frame_120s", "ss": 120, "ok": True, "valid": True, "file": "f.jpg"}],
        }), encoding="utf-8")
        (folder / "f.jpg").write_bytes(b"x" * 1024)
        with mock.patch.object(av_auto, "_load_llm_runtime",
                               return_value={"review_models": [DS, LU], "key": ""}), \
             mock.patch.object(av_auto, "_chat_retry",
                               return_value={"ok": False, "http": 503, "error": "upstream down", "parsed": None}), \
             mock.patch.object(av_auto, "_persist_frames_dir", return_value=0):
            rc = av_auto.cmd_review(SimpleNamespace(code="TEST-002", out=str(folder), cleanup=False))
        doc = json.loads((folder / "review.json").read_text(encoding="utf-8"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(doc.get("agreement"), "incomplete")
        self.assertFalse(doc.get("complete"))
        self.assertEqual(set(doc.get("incomplete_models") or []), {DS, LU})
        self.assertIsNone(av_auto._complete_review(folder))


if __name__ == "__main__":
    unittest.main()
