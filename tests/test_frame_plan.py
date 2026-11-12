"""Frame seek plan: 16-frame cap and spacing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402


class SeekPointsTests(unittest.TestCase):
    def test_long_movie_gets_sixteen(self):
        pts, meta = av_auto.seek_points(7200)
        self.assertEqual(len(pts), 16)
        labels = [label for label, _ in pts]
        self.assertIn("frame_120s", labels)
        self.assertIn("frame_85pct", labels)
        for i in range(len(pts) - 1):
            self.assertGreaterEqual(pts[i + 1][1] - pts[i][1], av_auto.SEEK_DEDUP)
        self.assertTrue(all(1 <= ss < 7200 for _, ss in pts))

    def test_cap_at_sixteen_for_very_long_movies(self):
        pts, _ = av_auto.seek_points(36000)
        self.assertLessEqual(len(pts), 16)

    def test_short_video_has_two_frames(self):
        pts, _ = av_auto.seek_points(60)
        self.assertEqual([label for label, _ in pts], ["frame_early", "frame_85pct"])

    def test_tiny_video_has_no_frames(self):
        pts, _ = av_auto.seek_points(8)
        self.assertEqual(pts, [])


if __name__ == "__main__":
    unittest.main()
