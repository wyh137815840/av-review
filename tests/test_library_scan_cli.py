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


if __name__ == "__main__":
    unittest.main()
