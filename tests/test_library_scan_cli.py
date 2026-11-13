"""library-scan entry and auto-promote switch wiring."""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402


class LibraryScanCliTests(unittest.TestCase):
    def test_library_scan_subcommand_is_registered(self):
        parser = av_auto.build_parser()
        sub = next(a for a in parser._actions if getattr(a, "choices", None))
        self.assertIn("library-scan", sub.choices)

    def test_library_scan_module_is_importable(self):
        import library_scan  # noqa: F401
        self.assertTrue(hasattr(library_scan, "cmd_library_scan"))

    def test_auto_promote_switch_env(self):
        with mock.patch.dict(os.environ, {"AV_AUTO_AUTO_PROMOTE": "0"}):
            self.assertFalse(av_auto.auto_promote_enabled())
        with mock.patch.dict(os.environ, {"AV_AUTO_AUTO_PROMOTE": "1"}):
            self.assertTrue(av_auto.auto_promote_enabled())


class SubmitSignalTests(unittest.TestCase):
    def test_911_inside_ed2k_hash_is_not_risk(self):
        import library_scan
        text = "0 0 aa47bc0912200fd911d4c0e1634c086e www.98T.la@259LUXU-1393_restored_wm.mp4 0"
        self.assertEqual(library_scan.classify_submit_text(text), "")

    def test_real_911_still_blocks(self):
        import library_scan
        self.assertEqual(library_scan.classify_submit_text("115 返回 911 疑似风控"), "block")

    def test_406_backoff_still_works(self):
        import library_scan
        self.assertEqual(library_scan.classify_submit_text("115 HTTP 406"), "backoff")

    def test_406_inside_hash_is_not_backoff(self):
        import library_scan
        self.assertEqual(library_scan.classify_submit_text("hash aa406bbccdd file.mp4"), "")


if __name__ == "__main__":
    unittest.main()
