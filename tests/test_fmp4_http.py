"""Fragmented MP4 HTTP frame extraction.

The HTTP server accepts only closed Range requests. ffmpeg's own seek uses
open-ended ranges and must fail; the range indexer must still return a frame
without reading the whole object.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402
import fmp4_http as fmp4  # noqa: E402

FIXTURE = Path("/tmp/fmp4-http-test")


class MemFile:
    def __init__(self, data: bytes):
        self._data = data
        self.size = len(data)
        self.fetched = 0

    def read(self, start: int, length: int) -> bytes:
        if start < 0 or start >= self.size:
            raise fmp4.Fmp4RangeError("range starts outside the file")
        length = min(length, self.size - start)
        self.fetched += length
        return self._data[start : start + length]

    def close(self) -> None:
        return None


def _ffmpeg_mp4(name: str, flags: str, duration: int = 20, size: str = "160x120", bitrate: str = "") -> Path:
    FIXTURE.mkdir(parents=True, exist_ok=True)
    dest = FIXTURE / name
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration}:size={size}:rate=10",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-g",
        "10",
    ]
    if bitrate:
        cmd += ["-b:v", bitrate]
    cmd += ["-movflags", flags, str(dest)]
    subprocess.check_call(cmd)
    return dest


def _jpeg(path: Path) -> None:
    data = path.read_bytes()
    if len(data) < 500 or data[:2] != b"\xff\xd8":
        raise AssertionError(f"not a jpeg: {path} bytes={len(data)}")


def _psnr(a: Path, b: Path) -> float:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(a),
            "-i",
            str(b),
            "-lavfi",
            "psnr",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    for line in text.splitlines():
        if "average:" in line:
            # ... average:40.12 min:...
            part = line.split("average:", 1)[1].split()[0]
            return float(part)
    raise AssertionError(f"psnr missing rc={proc.returncode} {text[-400:]}")


class BoxTests(unittest.TestCase):
    def test_tfra_offset_past_2gib_stays_unsigned(self):
        offset = 2825144000
        self.assertGreater(offset, 2**31)
        # mfra / tfra version 0, one entry, 1-byte traf/trun/sample numbers.
        # packed length field: sample=0, trun=0, traf=0 -> value 0.
        tfra_payload = bytearray()
        tfra_payload += b"\x00\x00\x00\x00"  # version/flags
        tfra_payload += (1).to_bytes(4, "big")  # track id
        tfra_payload += (0).to_bytes(4, "big")  # length sizes
        tfra_payload += (1).to_bytes(4, "big")  # count
        tfra_payload += (1000).to_bytes(4, "big")  # time
        tfra_payload += offset.to_bytes(4, "big")  # moof offset, unsigned 32
        tfra_payload += b"\x01\x01\x01"
        tfra = (8 + len(tfra_payload)).to_bytes(4, "big") + b"tfra" + bytes(tfra_payload)
        mfro_size = 16
        mfra_size = 8 + len(tfra) + mfro_size
        mfro = mfro_size.to_bytes(4, "big") + b"mfro" + b"\x00\x00\x00\x00" + mfra_size.to_bytes(4, "big")
        mfra = mfra_size.to_bytes(4, "big") + b"mfra" + tfra + mfro
        entries = fmp4.parse_mfra(mfra, 1, 1000)
        self.assertEqual(entries, [(1.0, offset)])

    def test_content_range_total(self):
        start, end, total = fmp4._parse_content_range("bytes 0-0/2825144160")
        self.assertEqual((start, end, total), (0, 0, 2825144160))


class IndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mfra = _ffmpeg_mp4("mfra.mp4", "frag_keyframe+empty_moov+default_base_moof", 20)
        cls.sidx = _ffmpeg_mp4("sidx.mp4", "frag_keyframe+empty_moov+default_base_moof+global_sidx+skip_trailer", 20)
        cls.scan = _ffmpeg_mp4("scan.mp4", "frag_keyframe+empty_moov+default_base_moof+skip_trailer", 20)
        cls.absolute = _ffmpeg_mp4("absolute.mp4", "frag_keyframe+empty_moov", 12)
        cls.progressive = _ffmpeg_mp4("progressive.mp4", "+faststart", 4)

    def _index(self, path: Path):
        remote = MemFile(path.read_bytes())
        index = fmp4.build_fmp4_index(remote)
        return remote, index

    def test_progressive_is_not_fragmented(self):
        _remote, index = self._index(self.progressive)
        self.assertIsNone(index)

    def test_mfra_sidx_and_scan_index_the_duration(self):
        for path, source in ((self.mfra, "mfra"), (self.sidx, "sidx"), (self.scan, "scan")):
            remote, index = self._index(path)
            self.assertIsNotNone(index, path.name)
            self.assertEqual(index.source, source, path.name)
            self.assertGreater(index.duration, 18)
            self.assertLess(index.duration, 22)
            blob, t0 = fmp4.materialize_snippet(remote, index, 8.0)
            self.assertLess(len(blob), remote.size)
            self.assertGreater(t0, 4)
            self.assertLess(t0, 9)

    def test_absolute_offsets_rebuild_without_copying_the_file(self):
        remote, index = self._index(self.absolute)
        self.assertIsNotNone(index)
        before = remote.fetched
        blob, _t0 = fmp4.materialize_snippet(remote, index, 6.0)
        self.assertLess(len(blob), remote.size)
        self.assertLess(remote.fetched - before, remote.size)
        dest = FIXTURE / "absolute.jpg"
        session = fmp4.Fmp4Session(remote, index)
        row = av_auto._extract_fmp4_frame(session, dest, 6)
        self.assertTrue(row.get("ok"), row)
        _jpeg(dest)


class HttpExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Larger than the 2MB header window so a closed header range is not the whole file.
        cls.media = _ffmpeg_mp4(
            "http-large.mp4",
            "frag_keyframe+empty_moov+default_base_moof",
            16,
            size="640x360",
            bitrate="2000k",
        )
        cls.data = cls.media.read_bytes()
        cls.ranges: list[str] = []
        cls.sent = 0
        data = cls.data
        ranges = cls.ranges

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/redir":
                    body = b""
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{port}/media")
                    self.send_header("Set-Cookie", "cdn=ok; Path=/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/ignore":
                    self.send_response(200)
                    self.send_header("Content-Length", "2825144160")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                if path != "/media":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if "cdn=ok" not in (self.headers.get("Cookie") or ""):
                    body = b"no"
                    self.send_response(403)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                rng = self.headers.get("Range") or ""
                ranges.append(rng)
                if not rng.startswith("bytes=") or rng.endswith("-") or rng.endswith("-,"):
                    # open-ended range: what ffmpeg sends. Refuse it.
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(data)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                spec = rng.split("=", 1)[1].split(",", 1)[0]
                start_s, end_s = spec.split("-", 1)
                if start_s == "" or end_s == "":
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = int(start_s), int(end_s)
                if start > end or start >= len(data):
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, len(data) - 1)
                body = data[start : end + 1]
                Handler.sent_total[0] += len(body)
                self.send_response(206)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return None

        Handler.sent_total = [0]
        cls.sent_box = Handler.sent_total
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        cls.port = port
        cls.httpd = httpd
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_open_ended_range_is_refused_and_ffmpeg_cannot_seek(self):
        url = f"http://127.0.0.1:{self.port}/redir"
        dest = FIXTURE / "ffmpeg-http.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "8", "-i", url, "-frames:v", "1", str(dest)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(any(r.endswith("-") or "-," in r or r.rsplit("-", 1)[-1] == "" for r in self.ranges) or "416" in (proc.stderr or "") or proc.returncode != 0)

    def test_closed_range_extracts_two_different_frames(self):
        self.ranges.clear()
        self.sent_box[0] = 0
        url = f"http://127.0.0.1:{self.port}/redir"
        # Keep the header probe smaller than this fixture so the test can see
        # that mdat outside the indexed fragment is not transferred.
        previous = fmp4.FMP4_HEADER_WINDOW
        fmp4.FMP4_HEADER_WINDOW = 64 * 1024
        session = fmp4.open_fmp4_session(url, timeout=10)
        self.assertIsNotNone(session)
        try:
            self.assertIn(session.index.source, {"mfra", "sidx", "scan"})
            self.assertGreater(session.index.duration, 14)
            early = FIXTURE / "early.jpg"
            late = FIXTURE / "late.jpg"
            ref = FIXTURE / "ref-late.jpg"
            row_early = av_auto._extract_fmp4_frame(session, early, 2)
            row_late = av_auto._extract_fmp4_frame(session, late, 12)
            self.assertTrue(row_early.get("ok"), row_early)
            self.assertTrue(row_late.get("ok"), row_late)
            _jpeg(early)
            _jpeg(late)
            self.assertNotEqual(early.read_bytes(), late.read_bytes())
            subprocess.check_call(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    "12",
                    "-i",
                    str(self.media),
                    "-frames:v",
                    "1",
                    str(ref),
                ]
            )
            self.assertGreater(_psnr(late, ref), 20)
            self.assertLess(self.sent_box[0], int(len(self.data) * 0.5))
            self.assertTrue(self.ranges, "no range requests were recorded")
            for rng in self.ranges:
                self.assertRegex(rng, r"^bytes=\d+-\d+$")
            self.assertLess(session.remote.fetched, len(self.data) // 2)
        finally:
            fmp4.FMP4_HEADER_WINDOW = previous
            session.close()

    def test_ignored_range_does_not_download_the_body(self):
        url = f"http://127.0.0.1:{self.port}/ignore"
        with self.assertRaises(fmp4.Fmp4RangeError) as caught:
            fmp4.BoundedHttpFile(url, timeout=3)
        self.assertIn("refused", str(caught.exception).lower())


if __name__ == "__main__":
    unittest.main()
