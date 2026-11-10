"""Bounded HTTP Range access for fragmented MP4 frame grabs.

ffmpeg `-ss` before `-i` on an HTTP(S) URL sends open-ended Range requests
(`bytes=0-` and `bytes=OFFSET-`) and seeks as if a progressive moov held
every sample. A fragmented file (ftyp + a small moov with mvex + moof/mdat,
optionally sidx or a tail mfra) then fails with "partial file" and writes
no packets. A closed Range of the first couple of megabytes, saved locally,
still decodes. This module only issues closed Ranges, keeps the redirect
cookie jar (115 CDN links 403 without the hop that set it), and builds a
small local init+fragment for the requested timestamp.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlparse

FMP4_HEADER_WINDOW = 2 * 1024 * 1024
FMP4_MAX_MOOV = 8 * 1024 * 1024
FMP4_MAX_MOOF = 4 * 1024 * 1024
FMP4_MAX_SIDX = 8 * 1024 * 1024
FMP4_MAX_MFRA = 32 * 1024 * 1024
FMP4_MAX_SPAN = 32 * 1024 * 1024
FMP4_MAX_READ = 32 * 1024 * 1024
FMP4_MAX_SCAN_BOXES = 20000
FMP4_RANGE_TIMEOUT = 45

# tfhd / trun / sample flags (ISO/IEC 14496-12)
TFHD_BASE_DATA_OFFSET = 0x000001
TFHD_DEFAULT_DURATION = 0x000008
TFHD_DEFAULT_SIZE = 0x000010
TFHD_DEFAULT_FLAGS = 0x000020
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000
TRUN_DATA_OFFSET = 0x000001
TRUN_FIRST_SAMPLE_FLAGS = 0x000004
TRUN_SAMPLE_DURATION = 0x000100
TRUN_SAMPLE_SIZE = 0x000200
TRUN_SAMPLE_FLAGS = 0x000400
TRUN_SAMPLE_CTS = 0x000800
SAMPLE_NON_SYNC = 0x00010000
SAMPLE_DEPENDS_YES = 0x01000000


class Fmp4RangeError(Exception):
    """Closed-range read or fragmented-MP4 index failed. Message has no URL."""


def _u32(buf: bytes) -> int:
    return int.from_bytes(buf, "big", signed=False)


def _u64(buf: bytes) -> int:
    return int.from_bytes(buf, "big", signed=False)


def _i32(buf: bytes) -> int:
    return int.from_bytes(buf, "big", signed=True)


def parse_box_header(buf: bytes, span: int, *, allow_eof: bool = False) -> tuple[int, bytes, int]:
    """Return (full_size, type, header_len). span is the container bytes left."""
    if len(buf) < 8 or span < 8:
        raise Fmp4RangeError("box header truncated")
    size = _u32(buf[0:4])
    typ = buf[4:8]
    hdr = 8
    if size == 1:
        if len(buf) < 16 or span < 16:
            raise Fmp4RangeError("64-bit box size truncated")
        size = _u64(buf[8:16])
        hdr = 16
    elif size == 0:
        if not allow_eof:
            raise Fmp4RangeError("box size extends to EOF")
        size = span
    if len(typ) != 4 or size < hdr:
        raise Fmp4RangeError("invalid box")
    return size, typ, hdr


def iter_boxes(buf: bytes, start: int, end: int):
    """Yield (offset, size, type, header_len) for complete boxes in buf[start:end]."""
    pos = start
    end = min(end, len(buf))
    while pos + 8 <= end:
        try:
            size, typ, hdr = parse_box_header(buf[pos:pos + 16], end - pos, allow_eof=False)
        except Fmp4RangeError:
            return
        if size < hdr or pos + size > end:
            return
        yield pos, size, typ, hdr
        pos += size


def _parse_content_range(value: str) -> tuple[int, int, int | None]:
    text = (value or "").strip()
    if not text.lower().startswith("bytes"):
        raise Fmp4RangeError("Content-Range missing")
    try:
        spec = text.split(None, 1)[1]
        range_part, total_part = spec.split("/", 1)
        start_s, end_s = range_part.split("-", 1)
        total = None if total_part.strip() == "*" else int(total_part)
        return int(start_s), int(end_s), total
    except (IndexError, ValueError) as exc:
        raise Fmp4RangeError("Content-Range unreadable") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture the redirect instead of dropping or rewriting Range."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class BoundedHttpFile:
    """HTTP(S) file read with closed Range requests only.

    Follows a short redirect chain and keeps cookies from those hops.
    Refuses a 200 whose Content-Length is larger than the window asked for,
    so a server that ignores Range cannot trick us into pulling the whole object.
    """

    def __init__(self, url: str, timeout: int = FMP4_RANGE_TIMEOUT):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise Fmp4RangeError("media URL is not http(s)")
        self.url = url
        self.timeout = timeout
        self.final_url = url
        self.size = 0
        self.fetched = 0
        self._parts: list[tuple[int, bytes]] = []
        jar = CookieJar()
        self._opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.ProxyHandler({}),
        )
        self._resolve()

    def close(self) -> None:
        return None

    def _exchange(self, url: str, range_spec: str, max_body: int) -> tuple[int, str, object, bytes]:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
                "Range": range_spec,
            },
            method="GET",
        )
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location") or ""
                exc.close()
                if not loc:
                    raise Fmp4RangeError(f"HTTP {exc.code} redirect without location") from None
                nxt = urljoin(url, loc)
                if urlparse(nxt).scheme not in ("http", "https"):
                    raise Fmp4RangeError("redirect left http(s)")
                return exc.code, nxt, exc.headers, b""
            exc.close()
            raise Fmp4RangeError(f"HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise Fmp4RangeError("HTTP connection failed") from None
        try:
            status = int(getattr(resp, "status", None) or resp.getcode() or 0)
            clen_raw = resp.headers.get("Content-Length")
            clen = int(clen_raw) if clen_raw and clen_raw.isdigit() else 0
            if clen > max_body:
                raise Fmp4RangeError("Range ignored or larger than the closed window; refused full-file read")
            data = resp.read(max_body + 1)
            if len(data) > max_body:
                raise Fmp4RangeError("Range body larger than the closed window; refused full-file read")
            return status, url, resp.headers, data
        finally:
            resp.close()

    def _resolve(self) -> None:
        url = self.url
        status = 0
        headers = None
        data = b""
        for _hop in range(6):
            status, nxt, headers, data = self._exchange(url, "bytes=0-0", 8)
            if status in (301, 302, 303, 307, 308):
                url = nxt
                continue
            break
        else:
            raise Fmp4RangeError("too many redirects")
        if status not in (200, 206):
            raise Fmp4RangeError(f"HTTP {status} opening media")
        self.final_url = url
        self.size = self._size_from(status, headers, data)
        if self.size <= 0:
            raise Fmp4RangeError("media length is zero")

    def _size_from(self, status: int, headers, data: bytes) -> int:
        cr = headers.get("Content-Range") if headers is not None else None
        if cr:
            _start, _end, total = _parse_content_range(cr)
            if not total:
                raise Fmp4RangeError("Content-Range has no total length")
            return int(total)
        if status == 200:
            clen_raw = headers.get("Content-Length") if headers is not None else None
            if clen_raw and clen_raw.isdigit() and int(clen_raw) == len(data):
                return int(clen_raw)
        raise Fmp4RangeError("server did not return a closed Content-Range total")

    def _cover(self, pos: int) -> tuple[int, bytes] | None:
        for start, blob in self._parts:
            if start <= pos < start + len(blob):
                return start, blob
        return None

    def _store(self, start: int, data: bytes) -> None:
        if not data:
            return
        self._parts.append((start, data))
        self._parts.sort(key=lambda item: item[0])
        merged: list[tuple[int, bytes]] = []
        for part_start, blob in self._parts:
            if not merged:
                merged.append((part_start, blob))
                continue
            prev_start, prev = merged[-1]
            prev_end = prev_start + len(prev)
            if part_start <= prev_end:
                if part_start + len(blob) > prev_end:
                    merged[-1] = (prev_start, prev + blob[prev_end - part_start :])
            else:
                merged.append((part_start, blob))
        self._parts = merged

    def _fetch(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if length > FMP4_MAX_READ:
            raise Fmp4RangeError("refusing a single range larger than the fragment cap")
        end = start + length - 1
        status, _url, headers, data = self._exchange(self.final_url, f"bytes={start}-{end}", length)
        if status == 200:
            raise Fmp4RangeError("server ignored Range and returned 200; refused full-file read")
        if status != 206:
            raise Fmp4RangeError(f"HTTP {status} on range")
        cr = headers.get("Content-Range") if headers is not None else None
        if not cr:
            raise Fmp4RangeError("206 without Content-Range")
        got_start, got_end, total = _parse_content_range(cr)
        if got_start != start:
            raise Fmp4RangeError("Content-Range start does not match the request")
        if total and self.size and total != self.size:
            raise Fmp4RangeError("Content-Range total changed")
        expect = got_end - got_start + 1
        data = data[:expect]
        if len(data) < expect and start + len(data) < self.size:
            raise Fmp4RangeError("short range body")
        self.fetched += len(data)
        self._store(start, data)
        return data

    def read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if start < 0 or start >= self.size:
            raise Fmp4RangeError("range starts outside the file")
        length = min(length, self.size - start)
        out = bytearray()
        pos = start
        end = start + length
        while pos < end:
            hit = self._cover(pos)
            if hit is not None:
                part_start, blob = hit
                off = pos - part_start
                take = min(len(blob) - off, end - pos)
                out += blob[off : off + take]
                pos += take
                continue
            nxt = end
            for part_start, _blob in self._parts:
                if pos < part_start < nxt:
                    nxt = part_start
            gap = nxt - pos
            chunk = self._fetch(pos, gap)
            if not chunk:
                raise Fmp4RangeError("empty range body")
            out += chunk
            pos += len(chunk)
        return bytes(out)


@dataclass
class Sample:
    dts: int
    duration: int
    size: int
    flags: int
    cto: int
    offset: int
    keyframe: bool


@dataclass
class Segment:
    start: int
    end: int
    t0: float
    t1: float
    sync: bool


@dataclass
class Fmp4Index:
    init_end: int
    duration: float
    source: str
    video_track_id: int
    video_timescale: int
    trex: dict
    segments: list[Segment] = field(default_factory=list)
    tfra: list[tuple[float, int]] = field(default_factory=list)
    streams: list[dict] = field(default_factory=list)
    cache: dict = field(default_factory=dict)


def _fullbox_version(box: bytes, hdr: int) -> int:
    if hdr >= len(box):
        raise Fmp4RangeError("full box truncated")
    return box[hdr]


def _timescale_duration(box: bytes, hdr: int) -> tuple[int, int]:
    """mvhd/mdhd timescale and duration. Duration may be the all-ones sentinel."""
    version = _fullbox_version(box, hdr)
    base = hdr + 4
    if version == 0:
        if base + 16 > len(box):
            raise Fmp4RangeError("mvhd/mdhd truncated")
        timescale = _u32(box[base + 8 : base + 12])
        duration = _u32(box[base + 12 : base + 16])
        if duration == 0xFFFFFFFF:
            duration = 0
    elif version == 1:
        if base + 28 > len(box):
            raise Fmp4RangeError("mvhd/mdhd truncated")
        timescale = _u32(box[base + 16 : base + 20])
        duration = _u64(box[base + 20 : base + 28])
        if duration == 0xFFFFFFFFFFFFFFFF:
            duration = 0
    else:
        raise Fmp4RangeError("unsupported mvhd/mdhd version")
    return timescale, duration


def _mehd_duration(box: bytes, hdr: int) -> int:
    version = _fullbox_version(box, hdr)
    base = hdr + 4
    if version == 0:
        if base + 4 > len(box):
            return 0
        dur = _u32(box[base : base + 4])
        return 0 if dur == 0xFFFFFFFF else dur
    if base + 8 > len(box):
        return 0
    return _u64(box[base : base + 8])


def _track_id(box: bytes, hdr: int) -> int:
    version = _fullbox_version(box, hdr)
    base = hdr + 4
    off = base + (16 if version == 1 else 8)
    if off + 4 > len(box):
        return 0
    return _u32(box[off : off + 4])


def _parse_stsd(box: bytes, hdr: int) -> dict | None:
    base = hdr + 8
    if base + 8 > len(box):
        return None
    entry_size = _u32(box[base : base + 4])
    codec = box[base + 4 : base + 8]
    if entry_size < 8 or base + entry_size > len(box):
        entry_size = min(entry_size, len(box) - base)
    entry = box[base : base + entry_size]
    width = height = 0
    if len(entry) >= 36 and codec not in (b"mp4a", b"enca", b"sowt", b"ac-3", b"ec-3"):
        width = int.from_bytes(entry[32:34], "big")
        height = int.from_bytes(entry[34:36], "big")
    kind = "video" if width and height else "audio"
    try:
        name = codec.decode("ascii")
    except UnicodeDecodeError:
        name = ""
    if not name:
        return None
    return {"codec_name": name, "codec_type": kind, "width": width, "height": height}


def _walk_trak(box: bytes) -> tuple[int, bytes, int, dict | None]:
    track_id = 0
    handler = b""
    timescale = 0
    stream = None

    def walk(buf: bytes, start: int, end: int) -> None:
        nonlocal track_id, handler, timescale, stream
        for off, size, typ, hdr in iter_boxes(buf, start, end):
            child = buf[off : off + size]
            if typ == b"tkhd":
                track_id = _track_id(child, hdr)
            elif typ == b"mdhd":
                timescale, _dur = _timescale_duration(child, hdr)
            elif typ == b"hdlr" and hdr + 12 <= len(child):
                handler = child[hdr + 8 : hdr + 12]
            elif typ == b"stsd":
                stream = _parse_stsd(child, hdr)
            elif typ not in (b"mdat", b"free", b"skip"):
                walk(buf, off + hdr, off + size)

    _size, _typ, hdr = parse_box_header(box[:16], len(box))
    walk(box, hdr, len(box))
    return track_id, handler, timescale, stream


def parse_moov(box: bytes) -> dict:
    info = {
        "mvhd_timescale": 0,
        "mvhd_duration": 0,
        "mehd": 0,
        "has_mvex": False,
        "trex": {},
        "video_track_id": 0,
        "video_timescale": 0,
        "streams": [],
    }
    size, typ, hdr = parse_box_header(box[:16], len(box))
    if typ != b"moov":
        raise Fmp4RangeError("expected moov")
    for off, child_size, child_type, child_hdr in iter_boxes(box, hdr, min(size, len(box))):
        child = box[off : off + child_size]
        if child_type == b"mvhd":
            info["mvhd_timescale"], info["mvhd_duration"] = _timescale_duration(child, child_hdr)
        elif child_type == b"mvex":
            info["has_mvex"] = True
            for soff, ssize, styp, shdr in iter_boxes(child, child_hdr, len(child)):
                sub = child[soff : soff + ssize]
                if styp == b"mehd":
                    info["mehd"] = _mehd_duration(sub, shdr)
                elif styp == b"trex" and shdr + 24 <= len(sub):
                    base = shdr + 4
                    track_id = _u32(sub[base : base + 4])
                    info["trex"][track_id] = {
                        "stsd_id": _u32(sub[base + 4 : base + 8]),
                        "duration": _u32(sub[base + 8 : base + 12]),
                        "size": _u32(sub[base + 12 : base + 16]),
                        "flags": _u32(sub[base + 16 : base + 20]),
                    }
        elif child_type == b"trak":
            track_id, handler, timescale, stream = _walk_trak(child)
            if stream:
                info["streams"].append(stream)
            if handler == b"vide" or (not info["video_track_id"] and stream and stream.get("codec_type") == "video"):
                info["video_track_id"] = track_id or info["video_track_id"]
                info["video_timescale"] = timescale or info["video_timescale"]
    if not info["video_timescale"]:
        info["video_timescale"] = info["mvhd_timescale"] or 1
    if not info["video_track_id"] and info["trex"]:
        info["video_track_id"] = next(iter(info["trex"]))
    return info


def _keyframe(flags: int) -> bool:
    return not (flags & (SAMPLE_NON_SYNC | SAMPLE_DEPENDS_YES))


def parse_moof_samples(moof: bytes, moof_off: int, video_track_id: int, trex: dict) -> tuple[list[Sample], bool]:
    """Return video samples and whether every tfhd is moof-relative."""
    size, typ, hdr = parse_box_header(moof[:16], len(moof))
    if typ != b"moof":
        raise Fmp4RangeError("expected moof")
    samples: list[Sample] = []
    relative = True
    saw_tfhd = False
    for off, child_size, child_type, child_hdr in iter_boxes(moof, hdr, min(size, len(moof))):
        if child_type != b"traf":
            continue
        traf = moof[off : off + child_size]
        track_id = 0
        base = moof_off
        default_dur = 0
        default_size = 0
        default_flags = 0
        tfdt = None
        traf_relative = False
        for soff, ssize, styp, shdr in iter_boxes(traf, child_hdr, len(traf)):
            sub = traf[soff : soff + ssize]
            if styp == b"tfhd":
                if shdr + 8 > len(sub):
                    continue
                flags = int.from_bytes(sub[shdr + 1 : shdr + 4], "big")
                track_id = _u32(sub[shdr + 4 : shdr + 8])
                cursor = shdr + 8
                saw_tfhd = True
                ext = trex.get(track_id) or {}
                default_dur = int(ext.get("duration") or 0)
                default_size = int(ext.get("size") or 0)
                default_flags = int(ext.get("flags") or 0)
                if flags & TFHD_BASE_DATA_OFFSET:
                    if cursor + 8 > len(sub):
                        raise Fmp4RangeError("tfhd truncated")
                    base = _u64(sub[cursor : cursor + 8])
                    cursor += 8
                    traf_relative = False
                elif flags & TFHD_DEFAULT_BASE_IS_MOOF:
                    base = moof_off
                    traf_relative = True
                else:
                    base = moof_off
                    traf_relative = False
                if flags & 0x000002:
                    cursor += 4
                if flags & TFHD_DEFAULT_DURATION:
                    default_dur = _u32(sub[cursor : cursor + 4])
                    cursor += 4
                if flags & TFHD_DEFAULT_SIZE:
                    default_size = _u32(sub[cursor : cursor + 4])
                    cursor += 4
                if flags & TFHD_DEFAULT_FLAGS:
                    default_flags = _u32(sub[cursor : cursor + 4])
                if not traf_relative:
                    relative = False
            elif styp == b"tfdt":
                version = _fullbox_version(sub, shdr)
                base_off = shdr + 4
                if version == 1 and base_off + 8 <= len(sub):
                    tfdt = _u64(sub[base_off : base_off + 8])
                elif base_off + 4 <= len(sub):
                    tfdt = _u32(sub[base_off : base_off + 4])
            elif styp == b"trun" and (not video_track_id or track_id == video_track_id):
                if shdr + 8 > len(sub):
                    continue
                version = sub[shdr]
                flags = int.from_bytes(sub[shdr + 1 : shdr + 4], "big")
                count = _u32(sub[shdr + 4 : shdr + 8])
                cursor = shdr + 8
                data_offset = 0
                if flags & TRUN_DATA_OFFSET:
                    if cursor + 4 > len(sub):
                        break
                    data_offset = _i32(sub[cursor : cursor + 4])
                    cursor += 4
                first_flags = default_flags
                if flags & TRUN_FIRST_SAMPLE_FLAGS:
                    if cursor + 4 > len(sub):
                        break
                    first_flags = _u32(sub[cursor : cursor + 4])
                    cursor += 4
                if flags & TRUN_DATA_OFFSET:
                    offset = base + data_offset
                elif samples:
                    offset = samples[-1].offset + samples[-1].size
                else:
                    offset = base
                if samples:
                    dts = samples[-1].dts + samples[-1].duration
                else:
                    dts = int(tfdt or 0)
                for i in range(count):
                    dur = default_dur
                    sz = default_size
                    fl = first_flags if i == 0 else default_flags
                    cto = 0
                    if flags & TRUN_SAMPLE_DURATION:
                        if cursor + 4 > len(sub):
                            break
                        dur = _u32(sub[cursor : cursor + 4])
                        cursor += 4
                    if flags & TRUN_SAMPLE_SIZE:
                        if cursor + 4 > len(sub):
                            break
                        sz = _u32(sub[cursor : cursor + 4])
                        cursor += 4
                    if flags & TRUN_SAMPLE_FLAGS:
                        if cursor + 4 > len(sub):
                            break
                        fl = _u32(sub[cursor : cursor + 4])
                        cursor += 4
                    if flags & TRUN_SAMPLE_CTS:
                        if cursor + 4 > len(sub):
                            break
                        raw = sub[cursor : cursor + 4]
                        cto = _i32(raw) if version == 1 else _u32(raw)
                        cursor += 4
                    if sz <= 0 or dur < 0:
                        continue
                    samples.append(
                        Sample(dts, dur, sz, fl, int(cto), int(offset), _keyframe(fl))
                    )
                    dts += dur
                    offset += sz
    if not saw_tfhd:
        relative = False
    return samples, relative


def parse_sidx(box: bytes, box_off: int) -> list[Segment]:
    size, typ, hdr = parse_box_header(box[:16], len(box))
    if typ != b"sidx":
        raise Fmp4RangeError("expected sidx")
    if hdr + 12 > len(box):
        raise Fmp4RangeError("sidx truncated")
    version = box[hdr]
    cursor = hdr + 4
    cursor += 4  # reference_ID
    timescale = _u32(box[cursor : cursor + 4]) or 1
    cursor += 4
    if version == 0:
        if cursor + 8 > len(box):
            raise Fmp4RangeError("sidx truncated")
        earliest = _u32(box[cursor : cursor + 4])
        first_offset = _u32(box[cursor + 4 : cursor + 8])
        cursor += 8
    else:
        if cursor + 16 > len(box):
            raise Fmp4RangeError("sidx truncated")
        earliest = _u64(box[cursor : cursor + 8])
        first_offset = _u64(box[cursor + 8 : cursor + 16])
        cursor += 16
    if cursor + 4 > len(box):
        raise Fmp4RangeError("sidx truncated")
    cursor += 2
    ref_count = int.from_bytes(box[cursor : cursor + 2], "big")
    cursor += 2
    anchor = box_off + min(size, len(box)) + first_offset
    time_ticks = earliest
    out: list[Segment] = []
    for _i in range(ref_count):
        if cursor + 12 > len(box):
            break
        word = _u32(box[cursor : cursor + 4])
        ref_type = word >> 31
        ref_size = word & 0x7FFFFFFF
        dur = _u32(box[cursor + 4 : cursor + 8])
        sap = _u32(box[cursor + 8 : cursor + 12])
        cursor += 12
        t0 = time_ticks / timescale
        t1 = (time_ticks + dur) / timescale
        if ref_type == 0 and ref_size > 0:
            out.append(Segment(anchor, anchor + ref_size, t0, t1, bool(sap >> 31)))
        anchor += ref_size
        time_ticks += dur
    if out and not any(seg.sync for seg in out):
        for seg in out:
            seg.sync = True
    return out


def _parse_tfra(box: bytes, hdr: int, timescale: int) -> tuple[int, list[tuple[float, int]]]:
    if hdr + 16 > len(box):
        return 0, []
    version = box[hdr]
    track_id = _u32(box[hdr + 4 : hdr + 8])
    packed = _u32(box[hdr + 8 : hdr + 12])
    count = _u32(box[hdr + 12 : hdr + 16])
    traf_len = ((packed >> 4) & 3) + 1
    trun_len = ((packed >> 2) & 3) + 1
    sample_len = (packed & 3) + 1
    cursor = hdr + 16
    scale = timescale or 1
    entries: list[tuple[float, int]] = []
    for _i in range(min(count, 2_000_000)):
        if version == 1:
            if cursor + 16 > len(box):
                break
            ticks = _u64(box[cursor : cursor + 8])
            moof_off = _u64(box[cursor + 8 : cursor + 16])
            cursor += 16
        else:
            if cursor + 8 > len(box):
                break
            ticks = _u32(box[cursor : cursor + 4])
            moof_off = _u32(box[cursor + 4 : cursor + 8])
            cursor += 8
        cursor += traf_len + trun_len + sample_len
        entries.append((ticks / scale, moof_off))
    return track_id, entries


def parse_mfra(blob: bytes, video_track_id: int, timescale: int) -> list[tuple[float, int]]:
    size, typ, hdr = parse_box_header(blob[:16], len(blob))
    if typ != b"mfra":
        return []
    chosen: list[tuple[float, int]] = []
    fallback: list[tuple[float, int]] = []
    for off, child_size, child_type, child_hdr in iter_boxes(blob, hdr, min(size, len(blob))):
        if child_type != b"tfra":
            continue
        track_id, entries = _parse_tfra(blob[off : off + child_size], child_hdr, timescale)
        if not entries:
            continue
        if video_track_id and track_id == video_track_id:
            chosen = entries
            break
        if len(entries) > len(fallback):
            fallback = entries
    entries = chosen or fallback
    entries.sort(key=lambda item: (item[0], item[1]))
    return entries


def _find_mfra(remote) -> bytes | None:
    if remote.size < 16:
        return None
    tail_n = min(4096, remote.size)
    tail_off = remote.size - tail_n
    tail = remote.read(tail_off, tail_n)
    idx = tail.rfind(b"mfro")
    if idx < 4:
        return None
    size_at = idx - 4
    mfro_size = _u32(tail[size_at : size_at + 4])
    if mfro_size < 16 or size_at + 8 >= len(tail):
        return None
    version = tail[idx + 4]
    if version == 0:
        if idx + 12 > len(tail):
            return None
        mfra_size = _u32(tail[idx + 8 : idx + 12])
    else:
        if idx + 16 > len(tail):
            return None
        mfra_size = _u64(tail[idx + 8 : idx + 16])
    if mfra_size < mfro_size or mfra_size > remote.size or mfra_size > FMP4_MAX_MFRA:
        return None
    mfro_end = tail_off + size_at + mfro_size
    mfra_start = mfro_end - mfra_size
    if mfra_start < 0:
        return None
    blob = remote.read(mfra_start, mfra_size)
    if len(blob) < 8 or blob[4:8] != b"mfra":
        return None
    return blob


def _read_box(remote, offset: int, limit: int) -> bytes:
    raw = remote.read(offset, 16)
    size, _typ, _hdr = parse_box_header(raw, remote.size - offset, allow_eof=True)
    if size > limit:
        raise Fmp4RangeError("box larger than the read cap")
    if size <= len(raw):
        return raw[:size]
    return remote.read(offset, size)


def _top_level_headers(remote, window: bytes) -> list[tuple[int, int, bytes, bool]]:
    """Boxes in the header window. The last item may extend past the window."""
    out = []
    pos = 0
    while pos + 8 <= len(window):
        size, typ, _hdr = parse_box_header(window[pos : pos + 16], remote.size - pos, allow_eof=True)
        complete = pos + size <= len(window) and size <= len(window) - pos
        out.append((pos, size, typ, complete))
        if not complete:
            break
        pos += size
        if len(out) > 64:
            break
    return out


def _seconds(ticks: int, timescale: int) -> float:
    if ticks <= 0 or timescale <= 0:
        return 0.0
    return ticks / timescale


def build_fmp4_index(remote) -> Fmp4Index | None:
    """Return an index when the object is a fragmented MP4, else None."""
    if remote.size < 16:
        return None
    window_n = min(remote.size, FMP4_HEADER_WINDOW)
    window = remote.read(0, window_n)
    if b"ftyp" not in window[:64] and b"moov" not in window[:4096] and b"moof" not in window[: FMP4_HEADER_WINDOW]:
        return None
    headers = _top_level_headers(remote, window)
    moov_off = moov_size = None
    sidx_off = sidx_size = None
    saw_moof = False
    for off, size, typ, _complete in headers:
        if typ == b"moov" and moov_off is None:
            moov_off, moov_size = off, size
        elif typ == b"sidx" and sidx_off is None:
            sidx_off, sidx_size = off, size
        elif typ == b"moof":
            saw_moof = True
    if moov_off is None:
        # moov can sit later; a fragmented file still has moof or mfra.
        if not saw_moof and _find_mfra(remote) is None:
            return None
        raise Fmp4RangeError("fragmented MP4 moov was not in the header window")
    if moov_size is None or moov_size > FMP4_MAX_MOOV:
        raise Fmp4RangeError("moov larger than the header cap")
    moov = _read_box(remote, moov_off, FMP4_MAX_MOOV)
    info = parse_moov(moov)
    init_end = moov_off + len(moov)
    if sidx_off is not None and sidx_size and sidx_size <= FMP4_MAX_SIDX:
        sidx = _read_box(remote, sidx_off, FMP4_MAX_SIDX)
    else:
        sidx = b""
    segments: list[Segment] = []
    if sidx:
        segments = parse_sidx(sidx, sidx_off or 0)
    fragmented = bool(info["has_mvex"] or saw_moof or segments)
    mfra_blob = None
    if not fragmented or not segments:
        mfra_blob = _find_mfra(remote)
        if mfra_blob:
            fragmented = True
    if not fragmented:
        return None
    timescale = int(info["video_timescale"] or info["mvhd_timescale"] or 1)
    duration = 0.0
    movie_scale = int(info["mvhd_timescale"] or timescale or 1)
    mehd_s = _seconds(int(info["mehd"] or 0), movie_scale)
    mvhd_s = _seconds(int(info["mvhd_duration"] or 0), movie_scale)
    if mehd_s > 1:
        duration = mehd_s
    elif mvhd_s > 1:
        duration = mvhd_s
    source = "sidx" if segments else ""
    tfra: list[tuple[float, int]] = []
    if not segments and mfra_blob:
        tfra = parse_mfra(mfra_blob, int(info["video_track_id"] or 0), timescale)
        source = "mfra"
    index = Fmp4Index(
        init_end=init_end,
        duration=duration,
        source=source or "scan",
        video_track_id=int(info["video_track_id"] or 0),
        video_timescale=timescale,
        trex=info["trex"],
        segments=list(segments),
        tfra=tfra,
        streams=list(info["streams"]),
    )
    if index.segments:
        index.duration = max(index.duration, index.segments[-1].t1)
    elif index.tfra:
        last = _realize_moof(remote, index, index.tfra[-1][1])
        index.duration = max(index.duration, last.t1)
    else:
        scanned = _scan_fragments(remote, index)
        if not scanned:
            raise Fmp4RangeError("fragmented MP4 has no readable moof index")
        index.segments = scanned
        index.source = "scan"
        index.duration = max(index.duration, scanned[-1].t1)
    if index.duration <= 0:
        raise Fmp4RangeError("fragmented MP4 duration is unknown")
    return index


def _scan_fragments(remote, index: Fmp4Index) -> list[Segment]:
    segments: list[Segment] = []
    offset = 0
    boxes = 0
    while offset + 8 <= remote.size and boxes < FMP4_MAX_SCAN_BOXES:
        boxes += 1
        try:
            raw = remote.read(offset, 16)
            size, typ, _hdr = parse_box_header(raw, remote.size - offset, allow_eof=True)
        except Fmp4RangeError:
            break
        if size < 8 or offset + size > remote.size + 8:
            break
        if typ == b"moof":
            if size > FMP4_MAX_MOOF:
                raise Fmp4RangeError("moof larger than the fragment cap during scan")
            seg = _realize_moof(remote, index, offset)
            segments.append(seg)
            offset = seg.end
            continue
        if typ in (b"mdat", b"free", b"skip") and size > 64 * 1024:
            offset += size
            continue
        offset += size
    return segments


def _realize_moof(remote, index: Fmp4Index, moof_off: int) -> Segment:
    cached = index.cache.get(moof_off)
    if isinstance(cached, Segment):
        return cached
    if moof_off < 0 or moof_off + 8 > remote.size:
        raise Fmp4RangeError("moof offset outside the file")
    raw = remote.read(moof_off, 16)
    size, typ, _hdr = parse_box_header(raw, remote.size - moof_off, allow_eof=True)
    if typ != b"moof":
        raise Fmp4RangeError("tfra/scan offset is not a moof")
    if size > FMP4_MAX_MOOF:
        raise Fmp4RangeError("moof larger than the fragment cap")
    moof = remote.read(moof_off, size)
    next_off = moof_off + size
    end = next_off
    if next_off + 8 <= remote.size:
        nh = remote.read(next_off, 16)
        nsize, ntyp, _nh = parse_box_header(nh, remote.size - next_off, allow_eof=True)
        if ntyp == b"mdat":
            end = next_off + nsize
    samples, _relative = parse_moof_samples(moof, moof_off, index.video_track_id, index.trex)
    scale = index.video_timescale or 1
    if samples:
        t0 = samples[0].dts / scale
        t1 = (samples[-1].dts + samples[-1].duration) / scale
        sync = bool(samples[0].keyframe)
    else:
        t0 = 0.0
        t1 = 0.0
        sync = True
    if end <= moof_off:
        raise Fmp4RangeError("fragment span is empty")
    seg = Segment(moof_off, end, t0, t1, sync)
    index.cache[moof_off] = seg
    return seg


def _pick_segment(segments: list[Segment], ss: float) -> int:
    if not segments:
        raise Fmp4RangeError("no fragments")
    chosen = 0
    for i, seg in enumerate(segments):
        if seg.t0 <= ss + 0.01:
            chosen = i
        if seg.t0 <= ss + 0.01 < seg.t1:
            return i
    return chosen


def _segment_pair(remote, index: Fmp4Index, ss: float) -> tuple[Segment, Segment | None]:
    if index.segments:
        i = _pick_segment(index.segments, ss)
        seg = index.segments[i]
        prev = index.segments[i - 1] if i > 0 and not seg.sync else None
        return seg, prev
    if not index.tfra:
        raise Fmp4RangeError("no fragment index")
    i = 0
    for n, (ticks, _off) in enumerate(index.tfra):
        if ticks <= ss + 0.05:
            i = n
    seg = _realize_moof(remote, index, index.tfra[i][1])
    if ss >= seg.t1 - 0.001 and i + 1 < len(index.tfra):
        i += 1
        seg = _realize_moof(remote, index, index.tfra[i][1])
    prev = _realize_moof(remote, index, index.tfra[i - 1][1]) if i > 0 and not seg.sync else None
    return seg, prev


def _moof_is_relative(moof: bytes) -> bool:
    try:
        size, typ, hdr = parse_box_header(moof[:16], len(moof))
    except Fmp4RangeError:
        return False
    if typ != b"moof":
        return False
    saw = False
    for off, child_size, child_type, child_hdr in iter_boxes(moof, hdr, min(size, len(moof))):
        if child_type != b"traf":
            continue
        traf = moof[off : off + child_size]
        for soff, ssize, styp, shdr in iter_boxes(traf, child_hdr, len(traf)):
            if styp != b"tfhd":
                continue
            sub = traf[soff : soff + ssize]
            if shdr + 4 > len(sub):
                return False
            flags = int.from_bytes(sub[shdr + 1 : shdr + 4], "big")
            saw = True
            if flags & TFHD_BASE_DATA_OFFSET or not (flags & TFHD_DEFAULT_BASE_IS_MOOF):
                return False
    return saw


def _keep_gop(samples: list[Sample], ss: float, timescale: int) -> list[Sample]:
    if not samples:
        raise Fmp4RangeError("moof has no video samples")
    scale = timescale or 1
    target = len(samples) - 1
    for i, sample in enumerate(samples):
        end = (sample.dts + sample.duration) / scale
        if end > ss + 1e-4:
            target = i
            break
    sync = target
    while sync > 0 and not samples[sync].keyframe:
        sync -= 1
    limit = samples[target].dts / scale + 0.5
    end_i = target
    while end_i + 1 < len(samples) and (samples[end_i].dts / scale) < limit:
        end_i += 1
    return samples[sync : end_i + 1]


def _load_sample_payload(remote, samples: list[Sample], max_span: int) -> bytes:
    if not samples:
        raise Fmp4RangeError("no samples to load")
    lo = min(sample.offset for sample in samples)
    hi = max(sample.offset + sample.size for sample in samples)
    if lo < 0 or hi < lo:
        raise Fmp4RangeError("sample offset is outside the file")
    if hi - lo > max_span:
        # Sample bytes are not one tight span. Pull each sample, but stop
        # rather than walking a multi-gigabyte mdat.
        payload = bytearray()
        for sample in samples:
            if sample.size > max_span or len(payload) + sample.size > max_span:
                break
            payload += remote.read(sample.offset, sample.size)
        if not payload:
            raise Fmp4RangeError("GOP exceeds the range cap; refused a full-fragment download")
        return bytes(payload)
    blob = remote.read(lo, hi - lo)
    parts = []
    for sample in samples:
        rel = sample.offset - lo
        piece = blob[rel : rel + sample.size]
        if len(piece) != sample.size:
            raise Fmp4RangeError("sample bytes missing inside the fragment")
        parts.append(piece)
    return b"".join(parts)


def _box(typ: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + typ + payload


def build_moof_mdat(track_id: int, base_dts: int, samples: list[Sample], payload: bytes) -> bytes:
    """A self-contained moof+mdat using default-base-is-moof and packed samples."""
    if not samples:
        raise Fmp4RangeError("cannot rebuild an empty fragment")
    if sum(sample.size for sample in samples) != len(payload):
        raise Fmp4RangeError("rebuilt sample sizes do not match the payload")
    entries = bytearray()
    for sample in samples:
        entries += int(sample.duration).to_bytes(4, "big")
        entries += int(sample.size).to_bytes(4, "big")
        entries += (int(sample.flags) & 0xFFFFFFFF).to_bytes(4, "big")
        entries += int(sample.cto).to_bytes(4, "big", signed=True)
    # version 1 so composition offsets stay signed
    trun_flags = TRUN_DATA_OFFSET | TRUN_SAMPLE_DURATION | TRUN_SAMPLE_SIZE | TRUN_SAMPLE_FLAGS | TRUN_SAMPLE_CTS
    trun_payload_len = 4 + 4 + 4 + len(entries)  # vf + count + data_offset + entries
    trun_box_len = 8 + trun_payload_len
    tfhd_payload = (TFHD_DEFAULT_BASE_IS_MOOF).to_bytes(4, "big") + int(track_id).to_bytes(4, "big")
    tfhd_box_len = 8 + len(tfhd_payload)
    tfdt_payload = bytes((1, 0, 0, 0)) + int(base_dts).to_bytes(8, "big", signed=False)
    tfdt_box_len = 8 + len(tfdt_payload)
    traf_payload_len = tfhd_box_len + tfdt_box_len + trun_box_len
    traf_box_len = 8 + traf_payload_len
    mfhd_payload = b"\x00\x00\x00\x00" + (1).to_bytes(4, "big")
    mfhd_box_len = 8 + len(mfhd_payload)
    moof_payload_len = mfhd_box_len + traf_box_len
    moof_len = 8 + moof_payload_len
    data_offset = moof_len + 8
    trun_head = bytes((1,)) + trun_flags.to_bytes(3, "big")
    trun_head += len(samples).to_bytes(4, "big")
    trun_head += int(data_offset).to_bytes(4, "big", signed=True)
    trun = _box(b"trun", trun_head + bytes(entries))
    tfhd = _box(b"tfhd", tfhd_payload)
    tfdt = _box(b"tfdt", tfdt_payload)
    traf = _box(b"traf", tfhd + tfdt + trun)
    mfhd = _box(b"mfhd", mfhd_payload)
    moof = _box(b"moof", mfhd + traf)
    if len(moof) != moof_len:
        raise Fmp4RangeError("rebuilt moof size mismatch")
    return moof + _box(b"mdat", payload)


def _append_segment(remote, index: Fmp4Index, seg: Segment, ss: float, max_span: int) -> tuple[bytes, float]:
    span = seg.end - seg.start
    if span <= 0 or seg.start < 0 or seg.end > remote.size + 1:
        raise Fmp4RangeError("fragment span is outside the file")
    moof_size = None
    raw = remote.read(seg.start, 16)
    size, typ, _hdr = parse_box_header(raw, remote.size - seg.start, allow_eof=True)
    if typ != b"moof":
        raise Fmp4RangeError("fragment does not start with moof")
    moof_size = size
    if moof_size > FMP4_MAX_MOOF:
        raise Fmp4RangeError("moof larger than the fragment cap")
    moof = remote.read(seg.start, moof_size)
    if _moof_is_relative(moof) and span <= max_span:
        return remote.read(seg.start, span), seg.t0
    samples, _rel = parse_moof_samples(moof, seg.start, index.video_track_id, index.trex)
    kept = _keep_gop(samples, ss, index.video_timescale)
    # If the kept GOP's own bytes already exceed the cap, _load_sample_payload trims.
    payload = _load_sample_payload(remote, kept, max_span)
    # _load may have truncated the payload. Keep only samples that fit.
    fit: list[Sample] = []
    used = 0
    for sample in kept:
        if used + sample.size > len(payload):
            break
        fit.append(sample)
        used += sample.size
    if not fit:
        raise Fmp4RangeError("GOP exceeds the range cap; refused a full-fragment download")
    payload = payload[:used]
    frag = build_moof_mdat(index.video_track_id or 1, fit[0].dts, fit, payload)
    scale = index.video_timescale or 1
    return frag, fit[0].dts / scale


def materialize_snippet(remote, index: Fmp4Index, ss: float, max_span: int = FMP4_MAX_SPAN) -> tuple[bytes, float]:
    """init segment + the fragment(s) covering ss. Does not read unrelated mdat."""
    if index.init_end <= 0 or index.init_end > FMP4_MAX_MOOV + 1024 * 1024:
        raise Fmp4RangeError("init segment is not a bounded header")
    seg, prev = _segment_pair(remote, index, float(ss))
    init = remote.read(0, index.init_end)
    pieces = [init]
    t0 = seg.t0
    if prev is not None and (prev.end - prev.start) <= max_span and (seg.end - seg.start) <= max_span:
        prev_bytes, t0 = _append_segment(remote, index, prev, prev.t0 + 0.01, max_span)
        cur_bytes, _cur_t0 = _append_segment(remote, index, seg, ss, max_span)
        pieces.append(prev_bytes)
        pieces.append(cur_bytes)
    else:
        cur_bytes, t0 = _append_segment(remote, index, seg, ss, max_span)
        pieces.append(cur_bytes)
    blob = b"".join(pieces)
    if len(blob) > max_span + len(init):
        raise Fmp4RangeError("local snippet exceeds the fragment cap")
    return blob, t0


class Fmp4Session:
    def __init__(self, remote: BoundedHttpFile, index: Fmp4Index):
        self.remote = remote
        self.index = index

    def close(self) -> None:
        self.remote.close()

    def snippet_for(self, ss: float, max_span: int = FMP4_MAX_SPAN) -> tuple[bytes, float]:
        return materialize_snippet(self.remote, self.index, ss, max_span=max_span)


def open_fmp4_session(url: str, timeout: int = FMP4_RANGE_TIMEOUT) -> Fmp4Session | None:
    """Open url and index it. None means the object is not a fragmented MP4."""
    remote = BoundedHttpFile(url, timeout=timeout)
    try:
        index = build_fmp4_index(remote)
    except Exception:
        remote.close()
        raise
    if index is None:
        remote.close()
        return None
    return Fmp4Session(remote, index)
