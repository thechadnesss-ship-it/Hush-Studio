"""Native MP4 stitcher: builds the long video by *byte-copying* encoded samples.

Why this exists
---------------
FFmpeg's concat demuxer + MP4 muxer handle every AAC frame individually. An 8 h mono
track is ~1.35 M frames, which measured ~42 s on a mid-range laptop (the mov muxer alone
~23 s), far over the 10 s budget. Every segment we join is the *same bytes* repeated, so
we can do much better:

  * copy each segment's raw sample bytes into one `mdat` (plain file I/O, disk-bound),
  * build the MP4 index (`moov`) directly from the source sample tables with numpy.

Still zero encoding (same "copy forever" rule), just without FFmpeg's per-packet overhead.

Scope: deliberately narrow. Inputs must be single-track MP4/M4A files whose samples are
stored contiguously (what FFmpeg writes for our audio-only m4a and video-only black loop),
without composition offsets (`ctts`; the black loop is built with `-bf 0`). Anything else
raises UnsupportedInput and the renderer falls back to the FFmpeg concat path.

Seams: each AAC segment keeps its encoder priming (1024 samples) and end padding, so every
join contains ≈ 21–43 ms of digital near-silence. Takes start and end with seam fades, so this
is inaudible. Only the very first segment's priming is skipped via the audio edit list.
"""
from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np


class UnsupportedInput(ValueError):
    pass


MOVIE_TIMESCALE = 1000
_WRITE_CHUNK = 4 << 20
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf"}
_IDENTITY_MATRIX = struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


# =========================================================================== parsing

def _iter_boxes(buf: bytes | memoryview, start: int = 0, end: int | None = None):
    end = len(buf) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            raise UnsupportedInput(f"corrupt box {typ!r} at {pos}")
        yield typ, pos + hdr, pos + size
        pos += size


def _find(buf, start, end, path: Sequence[bytes]):
    """Return (payload_start, payload_end) of the first box matching path, or None."""
    for typ, ps, pe in _iter_boxes(buf, start, end):
        if typ == path[0]:
            return (ps, pe) if len(path) == 1 else _find(buf, ps, pe, path[1:])
    return None


def _read_top_level(path: Path) -> tuple[bytes, tuple[int, int] | None]:
    """Read the moov box bytes and the mdat range without loading media data."""
    moov = None
    mdat = None
    size_total = path.stat().st_size
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= size_total:
            f.seek(pos)
            hdr = f.read(16)
            size, typ = struct.unpack_from(">I4s", hdr, 0)
            h = 8
            if size == 1:
                size = struct.unpack_from(">Q", hdr, 8)[0]
                h = 16
            elif size == 0:
                size = size_total - pos
            if size < h:
                raise UnsupportedInput(f"{path.name}: corrupt top-level box {typ!r}")
            if typ == b"moov":
                f.seek(pos)
                moov = f.read(size)
            elif typ == b"mdat":
                mdat = (pos + h, pos + size)
            pos += size
    if moov is None:
        raise UnsupportedInput(f"{path.name}: no moov box")
    return moov, mdat


@dataclass
class Track:
    """One track of a source file, reduced to what stitching needs."""
    path: Path
    handler: bytes                 # b"soun" | b"vide"
    timescale: int
    language: int
    sizes: np.ndarray              # uint32 per-sample sizes
    deltas: list[tuple[int, int]]  # stts run-length (count, delta)
    sync: np.ndarray | None        # 1-based sync sample numbers (None = all sync)
    data_start: int                # byte range of the (contiguous) sample data
    data_end: int
    media_time: int                # edit-list skip at the start (AAC priming), media timescale
    stsd: bytes                    # full stsd box (header included), copied verbatim
    hdlr: bytes                    # full hdlr box
    minf_extra: list[bytes]        # full vmhd/smhd/dinf boxes
    width: int = 0                 # tkhd 16.16 values
    height: int = 0
    volume: int = 0

    @property
    def n_samples(self) -> int:
        return int(self.sizes.size)

    @property
    def media_duration(self) -> int:
        return sum(c * d for c, d in self.deltas)

    @property
    def media_s(self) -> float:
        """Duration of all packets, priming/padding included (what a stitched copy occupies)."""
        return self.media_duration / self.timescale

    @property
    def priming_s(self) -> float:
        return self.media_time / self.timescale

    @property
    def byte_len(self) -> int:
        return self.data_end - self.data_start


def read_track(path: str | Path, handler: bytes) -> Track:
    path = Path(path)
    moov, mdat = _read_top_level(path)
    if mdat is None:
        raise UnsupportedInput(f"{path.name}: no mdat")
    mv = memoryview(moov)
    traks = [(ps, pe) for typ, ps, pe in _iter_boxes(mv, 8) if typ == b"trak"]
    if len(traks) != 1:
        raise UnsupportedInput(f"{path.name}: expected exactly one track, found {len(traks)}")
    for ts, te in traks:
        hd = _find(mv, ts, te, [b"mdia", b"hdlr"])
        if hd and bytes(mv[hd[0] + 8: hd[0] + 12]) == handler:
            return _parse_trak(path, handler, mv, ts, te, mdat)
    raise UnsupportedInput(f"{path.name}: no {handler.decode()} track")


def _full_box(mv, payload_range) -> bytes:
    """Re-serialise a box from its payload range (header is 8 bytes before payload)."""
    ps, pe = payload_range
    return bytes(mv[ps - 8: pe])


def _parse_trak(path: Path, handler: bytes, mv, ts: int, te: int, mdat: tuple[int, int]) -> Track:
    def need(p):
        r = _find(mv, ts, te, p)
        if r is None:
            raise UnsupportedInput(f"{path.name}: missing {b'/'.join(p).decode()}")
        return r

    stbl = need([b"mdia", b"minf", b"stbl"])
    if _find(mv, *stbl, [b"ctts"]):
        raise UnsupportedInput(f"{path.name}: has composition offsets (ctts); build with -bf 0")

    # --- mdhd
    ps, _ = need([b"mdia", b"mdhd"])
    ver = mv[ps]
    if ver == 1:
        timescale, _dur = struct.unpack_from(">IQ", mv, ps + 20)
        lang = struct.unpack_from(">H", mv, ps + 32)[0]
    else:
        timescale, _dur = struct.unpack_from(">II", mv, ps + 12)
        lang = struct.unpack_from(">H", mv, ps + 20)[0]

    # --- tkhd (width/height/volume)
    ps, _ = need([b"tkhd"])
    off = ps + (4 + 8 + 8 + 4 + 4 + 8 if mv[ps] == 1 else 4 + 4 + 4 + 4 + 4 + 4)
    off += 8  # reserved
    _layer, _alt, volume = struct.unpack_from(">hhH", mv, off)
    width, height = struct.unpack_from(">II", mv, off + 2 + 2 + 2 + 2 + 36)

    # --- sample tables
    ps, pe = _find(mv, *stbl, [b"stsz"]) or (None, None)
    if ps is None:
        raise UnsupportedInput(f"{path.name}: no stsz")
    sample_size, count = struct.unpack_from(">II", mv, ps + 4)
    if sample_size:
        sizes = np.full(count, sample_size, dtype=np.uint32)
    else:
        sizes = np.frombuffer(mv, dtype=">u4", count=count, offset=ps + 12).astype(np.uint32)

    ps, _ = need([b"mdia", b"minf", b"stbl", b"stts"])
    n = struct.unpack_from(">I", mv, ps + 4)[0]
    stts = np.frombuffer(mv, dtype=">u4", count=2 * n, offset=ps + 8).reshape(n, 2)
    deltas = [(int(c), int(d)) for c, d in stts]

    ps, _ = need([b"mdia", b"minf", b"stbl", b"stsc"])
    n = struct.unpack_from(">I", mv, ps + 4)[0]
    stsc = np.frombuffer(mv, dtype=">u4", count=3 * n, offset=ps + 8).reshape(n, 3).astype(np.int64)

    co = _find(mv, *stbl, [b"stco"])
    if co:
        n = struct.unpack_from(">I", mv, co[0] + 4)[0]
        chunk_off = np.frombuffer(mv, dtype=">u4", count=n, offset=co[0] + 8).astype(np.int64)
    else:
        co = _find(mv, *stbl, [b"co64"])
        if not co:
            raise UnsupportedInput(f"{path.name}: no chunk offsets")
        n = struct.unpack_from(">I", mv, co[0] + 4)[0]
        chunk_off = np.frombuffer(mv, dtype=">u8", count=n, offset=co[0] + 8).astype(np.int64)

    ss = _find(mv, *stbl, [b"stss"])
    sync = None
    if ss:
        n = struct.unpack_from(">I", mv, ss[0] + 4)[0]
        sync = np.frombuffer(mv, dtype=">u4", count=n, offset=ss[0] + 8).astype(np.int64)

    # --- verify samples are contiguous and inside mdat
    n_chunks = chunk_off.size
    spc = np.zeros(n_chunks, dtype=np.int64)
    for i, (first, per, _sdi) in enumerate(stsc):
        last = stsc[i + 1][0] - 1 if i + 1 < len(stsc) else n_chunks
        spc[first - 1: last] = per
    if spc.sum() != sizes.size:
        raise UnsupportedInput(f"{path.name}: stsc/stsz sample count mismatch")
    chunk_bytes = np.add.reduceat(sizes.astype(np.int64), np.concatenate(([0], np.cumsum(spc)[:-1]))) \
        if n_chunks else np.zeros(0, dtype=np.int64)
    if n_chunks > 1 and not np.array_equal(chunk_off[1:], chunk_off[:-1] + chunk_bytes[:-1]):
        raise UnsupportedInput(f"{path.name}: samples are not stored contiguously")
    data_start = int(chunk_off[0])
    data_end = data_start + int(sizes.sum(dtype=np.int64))
    if data_start < mdat[0] or data_end > mdat[1]:
        raise UnsupportedInput(f"{path.name}: sample data outside mdat")

    # --- edit list (priming skip)
    media_time = 0
    el = _find(mv, ts, te, [b"edts", b"elst"])
    if el:
        ver = mv[el[0]]
        n = struct.unpack_from(">I", mv, el[0] + 4)[0]
        fmt, step = (">QqI", 20) if ver == 1 else (">IiI", 12)
        entries = [struct.unpack_from(fmt, mv, el[0] + 8 + i * step) for i in range(n)]
        if len(entries) != 1 or entries[0][1] < 0:
            raise UnsupportedInput(f"{path.name}: unsupported edit list {entries}")
        media_time = int(entries[0][1])

    stsd = _full_box(mv, need([b"mdia", b"minf", b"stbl", b"stsd"]))
    hdlr = _full_box(mv, need([b"mdia", b"hdlr"]))
    minf = need([b"mdia", b"minf"])
    extra = [_full_box(mv, (ps, pe)) for typ, ps, pe in _iter_boxes(mv, *minf)
             if typ in (b"vmhd", b"smhd", b"dinf")]

    return Track(
        path=path, handler=handler,
        timescale=int(timescale), language=int(lang), sizes=sizes, deltas=deltas, sync=sync,
        data_start=data_start, data_end=data_end, media_time=media_time,
        stsd=stsd, hdlr=hdlr, minf_extra=extra, width=int(width), height=int(height), volume=int(volume),
    )


# =========================================================================== writing

def _box(typ: bytes, *payload: bytes) -> bytes:
    body = b"".join(payload)
    return struct.pack(">I4s", 8 + len(body), typ) + body


def _full(typ: bytes, version: int, flags: int, *payload: bytes) -> bytes:
    return _box(typ, struct.pack(">I", (version << 24) | flags), *payload)


def _rle_merge(runs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for c, d in runs:
        if c == 0:
            continue
        if out and out[-1][1] == d:
            out[-1][0] += c
        else:
            out.append([c, d])
    return [(c, d) for c, d in out]


def _truncate_runs(runs: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    out, left = [], n
    for c, d in runs:
        if left <= 0:
            break
        out.append((min(c, left), d))
        left -= c
    return out


@dataclass
class _Piece:
    track: Track
    n_samples: int          # how many samples of `track` to use (prefix)
    delta: int | None = None  # override every sample's duration (re-time a still video loop)

    def runs(self) -> list[tuple[int, int]]:
        if self.delta is not None:
            return [(self.n_samples, self.delta)]
        return _truncate_runs(self.track.deltas, self.n_samples)

    @property
    def duration(self) -> int:
        return sum(c * d for c, d in self.runs())

    @property
    def nbytes(self) -> int:
        if self.n_samples == self.track.n_samples:
            return self.track.byte_len
        return int(self.track.sizes[: self.n_samples].sum(dtype=np.int64))


@dataclass
class _OutTrack:
    track_id: int
    template: Track
    pieces: list[_Piece]
    presentation: int          # edit duration in MOVIE timescale
    media_time: int            # edit media_time in media timescale
    chunk_offsets: list[int] = field(default_factory=list)

    def build(self) -> bytes:
        t = self.template
        sizes = np.concatenate([p.track.sizes[: p.n_samples] for p in self.pieces])
        runs: list[tuple[int, int]] = []
        for p in self.pieces:
            runs += p.runs()
        runs = _rle_merge(runs)
        media_dur = sum(c * d for c, d in runs)

        stts = _full(b"stts", 0, 0, struct.pack(">I", len(runs)),
                     np.array(runs, dtype=">u4").tobytes())
        if np.all(sizes == sizes[0]):
            stsz = _full(b"stsz", 0, 0, struct.pack(">II", int(sizes[0]), sizes.size))
        else:
            stsz = _full(b"stsz", 0, 0, struct.pack(">II", 0, sizes.size), sizes.astype(">u4").tobytes())
        # one chunk per piece
        stsc_rows: list[tuple[int, int, int]] = []
        for i, p in enumerate(self.pieces):
            if not stsc_rows or stsc_rows[-1][1] != p.n_samples:
                stsc_rows.append((i + 1, p.n_samples, 1))
        stsc = _full(b"stsc", 0, 0, struct.pack(">I", len(stsc_rows)),
                     np.array(stsc_rows, dtype=">u4").tobytes())
        co64 = _full(b"co64", 0, 0, struct.pack(">I", len(self.chunk_offsets)),
                     np.array(self.chunk_offsets, dtype=">u8").tobytes())
        stbl_children = [t.stsd, stts]
        if t.sync is not None:
            syncs, base = [], 0
            for p in self.pieces:
                s = p.track.sync
                syncs.append(s[s <= p.n_samples] + base)
                base += p.n_samples
            sync_all = np.concatenate(syncs)
            stbl_children.append(_full(b"stss", 0, 0, struct.pack(">I", sync_all.size),
                                       sync_all.astype(">u4").tobytes()))
        stbl_children += [stsc, stsz, co64]
        stbl = _box(b"stbl", *stbl_children)
        minf = _box(b"minf", *t.minf_extra, stbl)

        v_md = 1 if media_dur > 0xFFFFFFFF else 0
        if v_md:
            mdhd = _full(b"mdhd", 1, 0, struct.pack(">QQIQHH", 0, 0, t.timescale, media_dur, t.language, 0))
        else:
            mdhd = _full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, t.timescale, media_dur, t.language, 0))
        mdia = _box(b"mdia", mdhd, t.hdlr, minf)

        elst = _full(b"elst", 0, 0, struct.pack(">IIiI", 1, self.presentation, self.media_time, 0x00010000))
        edts = _box(b"edts", elst)

        is_audio = t.handler == b"soun"
        tkhd = _full(
            b"tkhd", 0, 0x3,
            struct.pack(">IIIII", 0, 0, self.track_id, 0, self.presentation),
            b"\0" * 8,
            struct.pack(">hhHH", 0, 1 if is_audio else 0, 0x0100 if is_audio else 0, 0),
            _IDENTITY_MATRIX,
            struct.pack(">II", t.width, t.height),
        )
        return _box(b"trak", tkhd, edts, mdia)


ProgressFn = Callable[[float], None]


def video_sig(stsd: bytes) -> tuple[bytes, bytes, bytes]:
    """What must match for two H.264 pieces to share one sample entry: codec fourcc,
    width/height, and the avcC (SPS/PPS). btrt/colr/pasp are container-only and ignored."""
    entry = 16                                   # stsd hdr(8) + v/f(4) + count(4)
    fourcc = stsd[entry + 4: entry + 8]
    wh = stsd[entry + 32: entry + 36]            # visual sample entry: width@+32, height@+34
    avcc = b""
    for typ, ps, pe in _iter_boxes(stsd, entry + 8 + 78, len(stsd)):
        if typ == b"avcC":
            avcc = bytes(stsd[ps:pe])
    return fourcc, wh, avcc


@dataclass(frozen=True)
class StitchResult:
    path: Path
    duration_s: float
    audio_media_s: float
    video_frames: int
    bytes_written: int


def stitch(
    out_path: str | Path,
    audio_segments: Sequence[Path],
    video_loop: Path,
    duration_s: float,
    *,
    video_head: Sequence[Path] = (),
    loop_frame_s: float | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> StitchResult:
    """Write `out_path`: video = `video_head` pieces (e.g. the intro card) then `video_loop`
    repeated (trimmed); audio = the concatenated `audio_segments`, starting at t = 0.

    `audio_segments` may repeat the same path many times; each unique file is read once. An
    entry may be `(path, n_frames)` to use only that file's first n AAC frames (exact-length tail).
    `duration_s` is the presentation length; audio/video edit lists are clamped to it.
    Head and loop must share H.264 parameters (avcC) and timescale: they become one track.
    `loop_frame_s` shows each loop frame for that long (e.g. 1.0): a still black loop encoded at
    the intro's 30 fps then costs 30× fewer frames. The output is variable-frame-rate MP4.
    """
    out_path = Path(out_path)
    if not audio_segments:
        raise ValueError("no audio segments")

    cache: dict[Path, Track] = {}

    def track(p: Path, h: bytes) -> Track:
        key = Path(p).resolve()
        if key not in cache:
            cache[key] = read_track(key, h)
        return cache[key]

    norm = [(seg, None) if not isinstance(seg, tuple) else seg for seg in audio_segments]
    a_tracks = [track(p, b"soun") for p, _ in norm]
    a_counts = [n for _, n in norm]
    v = track(video_loop, b"vide")
    a0 = a_tracks[0]
    def entry_sig(stsd: bytes) -> tuple[bytes, bytes, bytes]:
        # stsd: hdr(8) v/f(4) count(4) | entry: size(4) type(4) ... channelcount@40 samplerate@48
        return stsd[20:24], stsd[40:44], stsd[48:52]

    for t in a_tracks[1:]:
        if t.timescale != a0.timescale or entry_sig(t.stsd) != entry_sig(a0.stsd):
            raise UnsupportedInput(f"{t.path.name}: audio sample entry differs from {a0.path.name}")
    heads = [track(p, b"vide") for p in video_head]
    for t in heads + [v]:
        if t.sync is not None and (t.sync.size == 0 or t.sync[0] != 1):
            raise UnsupportedInput(f"{t.path.name}: video must start with a keyframe")
    for t in heads:
        if t.timescale != v.timescale or video_sig(t.stsd) != video_sig(v.stsd):
            raise UnsupportedInput(f"{t.path.name}: H.264 parameters differ from {v.path.name}; "
                                   "encode both with AssetCache's shared recipe")

    # --- audio: every segment whole, or a prefix of n frames when given as (path, n);
    #     skip only the first segment's priming
    a_pieces = [_Piece(t, t.n_samples if n is None else min(int(n), t.n_samples))
                for t, n in zip(a_tracks, a_counts)]
    audio_media = sum(p.duration for p in a_pieces)
    audio_avail_s = (audio_media - a0.media_time) / a0.timescale

    total_s = min(float(duration_s), audio_avail_s)
    presentation = int(round(total_s * MOVIE_TIMESCALE))

    # --- video: head pieces, then whole loop copies, then a prefix of one more to cover total_s
    v_pieces: list[_Piece] = []
    need = int(np.ceil(total_s * v.timescale - 1e-6))
    for h in heads:
        if need <= 0:
            break
        v_pieces.append(_Piece(h, h.n_samples))
        need -= h.media_duration
    delta = None
    if loop_frame_s is not None:
        if len(v.deltas) != 1:
            raise UnsupportedInput(f"{v.path.name}: can only re-time a constant-frame-rate loop")
        delta = max(v.deltas[0][1], int(round(loop_frame_s * v.timescale)))
    per_copy = v.n_samples * delta if delta else v.media_duration
    while need > 0:
        if need >= per_copy:
            v_pieces.append(_Piece(v, v.n_samples, delta))
            need -= per_copy
        else:
            if delta:
                k = -(-need // delta)
            else:
                ends = np.cumsum(np.repeat([d for _, d in v.deltas], [c for c, _ in v.deltas]))
                k = int(np.searchsorted(ends, need)) + 1      # samples to cover `need`
            v_pieces.append(_Piece(v, min(k, v.n_samples), delta))
            need = 0

    a_out = _OutTrack(1, a0, a_pieces, presentation, a0.media_time)
    v_tmpl = heads[0] if heads else v           # the head's sample entry carries its colour info
    v_out = _OutTrack(2, v_tmpl, v_pieces, presentation, v_tmpl.media_time)

    # --- layout: ftyp | mdat(header 16B) [audio pieces][video pieces] | moov
    ftyp = _box(b"ftyp", b"isom", struct.pack(">I", 0x200), b"isom", b"iso2", b"avc1", b"mp41")
    mdat_hdr = 16
    pos = len(ftyp) + mdat_hdr
    for o, pieces in ((a_out, a_pieces), (v_out, v_pieces)):
        for p in pieces:
            o.chunk_offsets.append(pos)
            pos += p.nbytes
    mdat_size = pos - len(ftyp)

    mvhd = _full(
        b"mvhd", 0, 0,
        struct.pack(">IIIIIH", 0, 0, MOVIE_TIMESCALE, presentation, 0x00010000, 0x0100),
        b"\0" * 10, _IDENTITY_MATRIX, b"\0" * 24, struct.pack(">I", 3),
    )
    moov = _box(b"moov", mvhd, a_out.build(), v_out.build())

    # --- write: read each unique source's sample bytes once, write them many times
    blobs: dict[tuple[Path, int], bytes] = {}

    def blob(p: _Piece) -> bytes:
        key = (p.track.path, p.n_samples)
        if key not in blobs:
            with open(p.track.path, "rb") as f:
                f.seek(p.track.data_start)
                blobs[key] = f.read(p.nbytes)
            if len(blobs[key]) != p.nbytes:
                raise UnsupportedInput(f"{p.track.path.name}: truncated sample data")
        return blobs[key]

    total_bytes = len(ftyp) + mdat_size + len(moov)
    tmp = out_path.with_name(out_path.name + ".part")
    written = 0
    try:
        with open(tmp, "wb", buffering=0) as f:
            f.write(ftyp)
            f.write(struct.pack(">I4sQ", 1, b"mdat", mdat_size))
            written = len(ftyp) + mdat_hdr
            for p in a_pieces + v_pieces:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("cancelled")
                data = memoryview(blob(p))
                # Windows: one huge write() is ~20x slower than 4 MB chunks (measured).
                for i in range(0, len(data), _WRITE_CHUNK):
                    f.write(data[i: i + _WRITE_CHUNK])
                written += p.nbytes
                if on_progress:
                    on_progress(written / total_bytes)
            f.write(moov)
            written += len(moov)
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    if on_progress:
        on_progress(1.0)

    return StitchResult(
        path=out_path, duration_s=presentation / MOVIE_TIMESCALE, audio_media_s=audio_avail_s,
        video_frames=sum(p.n_samples for p in v_pieces), bytes_written=written,
    )


def audio_frame_info(audio_path: Path) -> tuple[int, int, int, int]:
    """(n_frames, frame_duration, priming, timescale) of an AAC m4a, as the stitcher lays it out."""
    t = read_track(audio_path, b"soun")
    return t.n_samples, t.deltas[0][1], t.media_time, t.timescale   # AAC: constant 1024-sample frames


def stitch_durations(audio_path: Path) -> tuple[float, float]:
    """(media_s, priming_s) of an audio segment, as the stitcher will lay it out."""
    t = read_track(audio_path, b"soun")
    return t.media_s, t.priming_s


def video_loop_duration(video_path: Path) -> float:
    return read_track(video_path, b"vide").media_s
