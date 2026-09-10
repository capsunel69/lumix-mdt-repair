#!/usr/bin/env python3
"""
mdt-repair: recover Panasonic LUMIX .MDT interrupted recordings.

When a LUMIX camera (GH5/GH5S/G9/S-series and similar) loses power or the
card stops mid-recording, it leaves an .MDT file: the raw 'mdat' payload of
an MP4 with no index ('moov'). The footage is intact but unplayable.

This tool rebuilds the moov by scanning the interleaved stream:
  * video samples are recovered by walking length-prefixed H.264 (AVCC) or
    HEVC (hvc1) NAL units -- each sample starts with an Access Unit Delimiter,
  * AAC audio frame boundaries are recovered by decoding the raw stream with
    libavcodec (same technique as untrunc), which reports bytes consumed
    per frame,
  * all container timing (frame rate, B-frame reordering/ctts pattern,
    edit lists, sample descriptions) is copied/derived from a healthy
    reference clip shot on the same camera with the same settings.

Unlike generic tools, the rebuilt file keeps BOTH tracks at original quality
and the method can be validated first: `selftest` re-derives the reference
clip's own index from its mdat and compares byte-for-byte against the index
the camera wrote.

Requires: Python 3.8+. For audio recovery, ffmpeg 4.x shared libraries
(libavcodec 57/58 -- the last versions exporting avcodec_decode_audio4).
With --video-only no libraries are needed.

Usage:
    mdt-repair selftest GOOD_CLIP.MP4
    mdt-repair repair CLIP.MDT GOOD_CLIP.MP4 [-o OUT.MP4] [--in-place]
    mdt-repair undo CLIP.MP4.mdt-recovery.json

MIT license. Born from recovering a real 70 GB / 98-minute GH5 V-Log clip.
"""

__version__ = "1.0.0"

import argparse
import ctypes
import ctypes.util
import json
import os
import struct
import sys
import time

# --------------------------------------------------------------------------
# MP4 box utilities
# --------------------------------------------------------------------------

def atoms(buf, off, end):
    """Iterate (type, payload_start, box_end) over boxes in buf[off:end]."""
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, off)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", buf, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr:
            return
        yield typ.decode("latin1"), off + hdr, off + size
        off += size


def find(buf, off, end, path):
    for typ, s, e in atoms(buf, off, end):
        if typ == path[0]:
            return (s, e) if len(path) == 1 else find(buf, s, e, path[1:])
    return None


def box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def fullbox(typ, verflags, payload):
    return box(typ, struct.pack(">I", verflags) + payload)


def read_moov(path):
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= fsize:
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                break
            size, typ = struct.unpack_from(">I4s", h, 0)
            if size == 1:
                size = struct.unpack_from(">Q", h, 8)[0]
            elif size == 0:
                size = fsize - pos
            if typ == b"moov":
                f.seek(pos)
                return f.read(size)
            pos += size
    raise SystemExit(f"error: no moov box in {path} -- is it a healthy MP4?")


def read_offsets(moov, stbl):
    r = find(moov, stbl[0], stbl[1], ["co64"])
    if r:
        n = struct.unpack_from(">I", moov, r[0] + 4)[0]
        return [struct.unpack_from(">Q", moov, r[0] + 8 + 8 * i)[0] for i in range(n)]
    r = find(moov, stbl[0], stbl[1], ["stco"])
    n = struct.unpack_from(">I", moov, r[0] + 4)[0]
    return [struct.unpack_from(">I", moov, r[0] + 8 + 4 * i)[0] for i in range(n)]


def read_stsz(moov, stbl):
    r = find(moov, stbl[0], stbl[1], ["stsz"])
    fixed, cnt = struct.unpack_from(">II", moov, r[0] + 4)
    if fixed:
        return [fixed] * cnt
    return [struct.unpack_from(">I", moov, r[0] + 12 + 4 * i)[0] for i in range(cnt)]


def read_stsc(moov, stbl):
    r = find(moov, stbl[0], stbl[1], ["stsc"])
    n = struct.unpack_from(">I", moov, r[0] + 4)[0]
    return [struct.unpack_from(">III", moov, r[0] + 8 + 12 * i) for i in range(n)]


# --------------------------------------------------------------------------
# Reference clip analysis
# --------------------------------------------------------------------------

class Ref:
    """Everything we learn from a healthy clip shot with the same settings."""

    def __init__(self, path):
        self.path = path
        self.moov = read_moov(path)
        self.vtrak = self.atrak = None
        for typ, s, e in atoms(self.moov, 8, len(self.moov)):
            if typ != "trak":
                continue
            h = find(self.moov, s, e, ["mdia", "hdlr"])
            kind = self.moov[h[0] + 8:h[0] + 12]
            if kind == b"vide":
                self.vtrak = (s, e)
            elif kind == b"soun":
                self.atrak = (s, e)
        if not self.vtrak:
            raise SystemExit("error: reference clip has no video track")

        # video timing
        vstbl = find(self.moov, *self.vtrak, path=["mdia", "minf", "stbl"])
        self.vstbl = vstbl
        stsd = find(self.moov, vstbl[0], vstbl[1], ["stsd"])
        codec = self.moov[stsd[0] + 12:stsd[0] + 16]
        if codec in (b"hvc1", b"hev1"):
            self.hevc = True
        elif codec in (b"avc1", b"avc3"):
            self.hevc = False
        else:
            raise SystemExit(f"error: unsupported video codec {codec!r} "
                             "(need avc1/H.264 or hvc1/HEVC)")
        r = find(self.moov, vstbl[0], vstbl[1], ["stts"])
        n = struct.unpack_from(">I", self.moov, r[0] + 4)[0]
        deltas = {struct.unpack_from(">II", self.moov, r[0] + 8 + 8 * i)[1]
                  for i in range(n)}
        if len(deltas) != 1:
            raise SystemExit("error: reference video has variable frame durations "
                             "-- unsupported")
        self.vdelta = deltas.pop()
        md = find(self.moov, *self.vtrak, path=["mdia", "mdhd"])
        if self.moov[md[0]] != 0:
            raise SystemExit("error: version-1 mdhd in reference -- unsupported")
        self.vtimescale = struct.unpack_from(">I", self.moov, md[0] + 12)[0]

        # ctts (B-frame reordering) pattern
        self.ctts_period = None
        r = find(self.moov, vstbl[0], vstbl[1], ["ctts"])
        if r:
            n = struct.unpack_from(">I", self.moov, r[0] + 4)[0]
            expanded = []
            for i in range(n):
                cnt, offv = struct.unpack_from(">Ii", self.moov, r[0] + 8 + 8 * i)
                expanded.extend([offv] * cnt)
                if len(expanded) > 500_000:
                    break
            for p in range(1, min(65, len(expanded))):
                pat = expanded[:p]
                if all(expanded[i] == pat[i % p] for i in range(len(expanded))):
                    self.ctts_period = pat
                    break
            if self.ctts_period is None:
                raise SystemExit("error: reference ctts is not periodic -- cannot "
                                 "infer B-frame timing for the broken clip")

        # audio
        self.asc = None
        self.adelta = None
        if self.atrak:
            astbl = find(self.moov, *self.atrak, path=["mdia", "minf", "stbl"])
            self.astbl = astbl
            r = find(self.moov, astbl[0], astbl[1], ["stts"])
            n = struct.unpack_from(">I", self.moov, r[0] + 4)[0]
            deltas = {struct.unpack_from(">II", self.moov, r[0] + 8 + 8 * i)[1]
                      for i in range(n)}
            if len(deltas) != 1:
                raise SystemExit("error: variable audio frame duration -- unsupported")
            self.adelta = deltas.pop()
            md = find(self.moov, *self.atrak, path=["mdia", "mdhd"])
            self.atimescale = struct.unpack_from(">I", self.moov, md[0] + 12)[0]
            self.asc = self._extract_asc()

        # movie timescale
        mv = find(self.moov, 8, len(self.moov), ["mvhd"])
        self.mvtimescale = struct.unpack_from(">I", self.moov, mv[0] + 12)[0]

    def _extract_asc(self):
        stsd = find(self.moov, self.astbl[0], self.astbl[1], ["stsd"])
        i = self.moov.find(b"esds", stsd[0], stsd[1])
        if i < 0:
            raise SystemExit("error: no esds in reference audio track")
        p = i + 8
        buf = self.moov

        def rdlen(p):
            l = 0
            for _ in range(4):
                b = buf[p]; p += 1
                l = (l << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            return l, p

        assert buf[p] == 0x03; _, p = rdlen(p + 1); p += 3
        assert buf[p] == 0x04; _, p = rdlen(p + 1); p += 13
        assert buf[p] == 0x05; l, p = rdlen(p + 1)
        return buf[p:p + l]

    def summary(self):
        fps = self.vtimescale / self.vdelta
        codec = "HEVC" if self.hevc else "H.264"
        s = [f"video: {codec} {fps:.3f} fps (delta {self.vdelta}@{self.vtimescale})"]
        if self.ctts_period:
            s.append(f"B-frame ctts period: {self.ctts_period}")
        if self.atrak:
            s.append(f"audio: AAC ASC={self.asc.hex()} "
                     f"({self.adelta} samples/frame @{self.atimescale} Hz)")
        return "; ".join(s)


# --------------------------------------------------------------------------
# AAC frame-boundary recovery via libavcodec (ctypes)
# --------------------------------------------------------------------------

AV_CODEC_ID_AAC = 86018


class AudioUnavailable(Exception):
    pass


def _load_libav():
    extra = [
        "/opt/homebrew/opt/ffmpeg@4/lib",
        "/usr/local/opt/ffmpeg@4/lib",
        "/opt/homebrew/lib",
        "/usr/local/lib",
    ]
    cand_codec = []
    cand_util = []
    for d in extra:
        cand_codec += [os.path.join(d, n) for n in
                       ("libavcodec.58.dylib", "libavcodec.57.dylib")]
        cand_util += [os.path.join(d, n) for n in
                      ("libavutil.56.dylib", "libavutil.55.dylib")]
    cand_codec += ["libavcodec.so.58", "libavcodec.so.57",
                   "libavcodec.58.dylib", "libavcodec.57.dylib"]
    cand_util += ["libavutil.so.56", "libavutil.so.55",
                  "libavutil.56.dylib", "libavutil.55.dylib"]
    for lib in (ctypes.util.find_library("avcodec"),):
        if lib:
            cand_codec.append(lib)
    for lib in (ctypes.util.find_library("avutil"),):
        if lib:
            cand_util.append(lib)
    avc = avu = None
    for name in cand_codec:
        try:
            lib = ctypes.CDLL(name)
            lib.avcodec_decode_audio4  # removed in libavcodec >= 59
            avc = lib
            break
        except (OSError, AttributeError):
            continue
    for name in cand_util:
        try:
            avu = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if not avc or not avu:
        raise AudioUnavailable(
            "libavcodec 57/58 with avcodec_decode_audio4 not found.\n"
            "Install ffmpeg 4.x shared libraries, or rerun with --video-only.\n"
            "(ffmpeg 5+ removed the API that reports bytes-consumed per frame.)")
    return avc, avu


class AAC:
    """Recovers AAC frame boundaries by asking the decoder how many bytes
    each frame consumed. Struct offsets are discovered at runtime, so this
    stays independent of libavcodec header versions."""

    def __init__(self, asc: bytes):
        avc, avu = _load_libav()
        self.avc, self.avu = avc, avu
        avu.av_log_set_level(-8)
        for n, res in [("avcodec_find_decoder", ctypes.c_void_p),
                       ("avcodec_alloc_context3", ctypes.c_void_p),
                       ("avcodec_open2", ctypes.c_int),
                       ("av_packet_alloc", ctypes.c_void_p),
                       ("av_new_packet", ctypes.c_int),
                       ("avcodec_decode_audio4", ctypes.c_int)]:
            getattr(avc, n).restype = res
        avu.av_frame_alloc.restype = ctypes.c_void_p
        avu.av_mallocz.restype = ctypes.c_void_p
        avu.av_opt_find.restype = ctypes.c_void_p

        codec = avc.avcodec_find_decoder(AV_CODEC_ID_AAC)
        ctx = avc.avcodec_alloc_context3(ctypes.c_void_p(codec))
        assert codec and ctx
        self.ctx = ctx

        # locate extradata/extradata_size fields: they sit directly after the
        # 'flags'/'flags2' ints, whose offsets the AVOption table reveals.
        def opt_offset(name):
            o = avu.av_opt_find(ctypes.c_void_p(ctx), name, None, 0, 0)
            if not o:
                raise AudioUnavailable(f"AVOption {name!r} not found")
            return struct.unpack_from("<i", ctypes.string_at(o + 16, 4))[0]

        f1, f2 = opt_offset(b"flags"), opt_offset(b"flags2")
        if f2 != f1 + 4:
            raise AudioUnavailable("unexpected AVCodecContext layout")
        ed_off = (f2 + 4 + 7) & ~7
        buf = avu.av_mallocz(len(asc) + 64)
        ctypes.memmove(buf, asc, len(asc))
        ctypes.memmove(ctx + ed_off, struct.pack("<Q", buf), 8)
        ctypes.memmove(ctx + ed_off + 8, struct.pack("<i", len(asc)), 4)
        if avc.avcodec_open2(ctypes.c_void_p(ctx), ctypes.c_void_p(codec), None) != 0:
            raise AudioUnavailable("could not open AAC decoder")

        self.frame = avu.av_frame_alloc()
        self.pkt = avc.av_packet_alloc()
        # discover AVPacket data/size offsets empirically
        assert avc.av_new_packet(ctypes.c_void_p(self.pkt), 12345) == 0
        raw = ctypes.string_at(self.pkt, 96)
        self.size_off = next(o for o in range(0, 92, 4)
                             if struct.unpack_from("<i", raw, o)[0] == 12345)
        self.data_off = self.size_off - 8
        avc.av_packet_unref(ctypes.c_void_p(self.pkt))

    def frame_sizes(self, blob: bytes, allow_partial=False):
        """List of AAC frame sizes exactly covering blob, or None.
        allow_partial: return the decodable prefix instead of None."""
        avc = self.avc
        n = len(blob)
        if n == 0:
            return []
        assert avc.av_new_packet(ctypes.c_void_p(self.pkt), n) == 0
        base = struct.unpack_from("<Q", ctypes.string_at(self.pkt + self.data_off, 8))[0]
        ctypes.memmove(base, blob, n)
        sizes, pos = [], 0
        got = ctypes.c_int(0)
        while pos < n:
            ctypes.memmove(self.pkt + self.data_off, struct.pack("<Q", base + pos), 8)
            ctypes.memmove(self.pkt + self.size_off, struct.pack("<i", n - pos), 4)
            consumed = avc.avcodec_decode_audio4(
                ctypes.c_void_p(self.ctx), ctypes.c_void_p(self.frame),
                ctypes.byref(got), ctypes.c_void_p(self.pkt))
            if consumed <= 0:
                break
            sizes.append(consumed)
            pos += consumed
        ctypes.memmove(self.pkt + self.data_off, struct.pack("<Q", base), 8)
        ctypes.memmove(self.pkt + self.size_off, struct.pack("<i", n), 4)
        avc.av_packet_unref(ctypes.c_void_p(self.pkt))
        if pos == n:
            return sizes
        return sizes if allow_partial else None


# --------------------------------------------------------------------------
# Stream scanner
# --------------------------------------------------------------------------

VALID_NAL = frozenset(range(1, 13))
AUD_PAT_H264 = b"\x00\x00\x00\x02\x09"   # 4-byte length 2 + AUD NAL header
AUD_PAT_HEVC = b"\x00\x00\x00\x03\x46"   # length 3 + HEVC AUD (type 35)
HEVC_IRAP = frozenset(range(16, 22))


def _aud_pat(hevc):
    return AUD_PAT_HEVC if hevc else AUD_PAT_H264


def walk_video_chunk(f, off, file_end, hevc=False):
    """Walk AVCC NALs from off. Sample boundaries at AUD NALs.
    Returns (sample_sizes, idr_flags, end_offset)."""
    samples, idrs = [], []
    cur = 0
    cur_idr = False
    started = False
    pos = off
    need = 6 if hevc else 5
    min_l = 2 if hevc else 1
    aud = 35 if hevc else 9
    while pos + need <= file_end:
        f.seek(pos)
        hdr = f.read(need)
        if len(hdr) < need:
            break
        L = struct.unpack_from(">I", hdr)[0]
        b0 = hdr[4]
        typ = (b0 >> 1) & 0x3F if hevc else (b0 & 0x1F)
        bad_typ = typ > 40 if hevc else typ not in VALID_NAL
        if (L < min_l or L > 8_000_000 or (b0 & 0x80)
                or bad_typ or pos + 4 + L > file_end):
            break
        if typ == aud:
            if started:
                samples.append(cur)
                idrs.append(cur_idr)
            started, cur, cur_idr = True, 0, False
        elif not started:
            break  # chunks must start with an AUD
        if (typ in HEVC_IRAP) if hevc else (typ == 5):
            cur_idr = True
        cur += 4 + L
        pos += 4 + L
    if started and cur:
        samples.append(cur)
        idrs.append(cur_idr)
    return samples, idrs, off + sum(samples)


def find_next_video(f, off, file_end, window=8 * 1024 * 1024, hevc=False):
    f.seek(off)
    data = f.read(min(window, file_end - off))
    i = data.find(_aud_pat(hevc))
    return off + i if i != -1 else None


def _mdat_box_off(f, file_end):
    """Offset of the first mdat box header in a leading-box prefix, else None."""
    pos = 0
    limit = min(file_end, 4 * 1024 * 1024)
    while pos + 8 <= limit:
        f.seek(pos)
        h = f.read(16)
        if len(h) < 8:
            return None
        size, typ = struct.unpack_from(">I4s", h, 0)
        hdr = 8
        if size == 1:
            if len(h) < 16:
                return None
            size = struct.unpack_from(">Q", h, 8)[0]
            hdr = 16
        elif size == 0:
            size = file_end - pos
        if typ == b"mdat":
            return pos
        if size < hdr:
            return None
        pos += size
    return None


def detect_stream_start(f, file_end, hevc=False):
    """The mdt usually begins with an unfinalized mdat header (8 bytes) plus
    8 scratch bytes. Some cameras prefix free/skip boxes; find the NALs."""
    candidates = [16, 8, 0]
    mdat = _mdat_box_off(f, file_end)
    if mdat:
        candidates = [mdat + 16, mdat + 8, mdat] + candidates
    probe = min(file_end, 4_000_000)
    for off in candidates:
        if off >= file_end:
            continue
        s, _, _ = walk_video_chunk(f, off, min(file_end, off + probe), hevc)
        if s:
            return off
    nx = find_next_video(f, 0, min(file_end, 2 << 20), hevc=hevc)
    if nx is None:
        kind = "HEVC" if hevc else "H.264"
        raise SystemExit(f"error: no {kind} stream found near start of file")
    return nx


def scan(mdt_path, ref, state_path, video_only=False, budget=None, quiet=False):
    """Resumable scan. Returns state dict with vchunks/achunks."""
    fe = os.path.getsize(mdt_path)
    f = open(mdt_path, "rb")
    aac = None
    if not video_only:
        aac = AAC(ref.asc) if ref.asc else None
        if aac is None:
            raise SystemExit("error: reference has no audio track; "
                             "use --video-only")

    if state_path and os.path.exists(state_path):
        st = json.load(open(state_path))
    else:
        st = {"pos": detect_stream_start(f, fe, hevc=ref.hevc),
              "vchunks": [], "achunks": [],
              "anomalies": [], "done": False}

    t0 = time.time()
    last_print = 0.0
    pos = st["pos"]
    avg_aframe = [400.0]  # running average for the estimation fallback

    def est_sizes(blob_len):
        k = max(1, round(blob_len / avg_aframe[0]))
        b = blob_len // k
        r = blob_len - b * k
        return [b + 1] * r + [b] * (k - r)

    while pos < fe and not st["done"]:
        if budget and time.time() - t0 > budget:
            break
        samples, idrs, vend = walk_video_chunk(f, pos, fe, hevc=ref.hevc)
        if not samples:
            st["anomalies"].append(["video_resync", pos])
            nx = find_next_video(f, pos + 1, fe, hevc=ref.hevc)
            if nx is None:
                st["done"] = True
                break
            pos = nx
            continue
        st["vchunks"].append([pos, samples, [int(b) for b in idrs]])

        nxt = find_next_video(f, vend, fe, hevc=ref.hevc)
        # audio region [vend, nxt); a false AUD match inside audio data makes
        # decode fail -- extend the region past the phantom match and retry.
        sizes = None
        if aac and nxt is not None and nxt > vend:
            for _ in range(5):
                blob_len = nxt - vend
                if blob_len > 4_000_000:
                    break
                f.seek(vend)
                sizes = aac.frame_sizes(f.read(blob_len))
                if sizes is not None:
                    break
                nxt2 = find_next_video(f, nxt + 1, fe, hevc=ref.hevc)
                if nxt2 is None:
                    break
                nxt = nxt2
        if nxt is None:
            # tail: recover as many complete audio frames as possible
            if aac and fe - vend > 0:
                f.seek(vend)
                tail = aac.frame_sizes(f.read(min(fe - vend, 4_000_000)),
                                       allow_partial=True)
                if tail:
                    st["achunks"].append([vend, tail])
                lost = fe - vend - sum(tail or [])
                if lost:
                    st["anomalies"].append(["tail_truncated_bytes", vend, lost])
            st["done"] = True
            pos = fe
            break
        if aac and nxt > vend:
            if sizes is None:
                sizes = est_sizes(nxt - vend)
                st["anomalies"].append(["audio_estimated", vend, nxt - vend])
            st["achunks"].append([vend, sizes])
            for s in sizes[-8:]:
                avg_aframe[0] = avg_aframe[0] * 0.9 + s * 0.1
        pos = nxt

        if not quiet and time.time() - last_print > 2:
            last_print = time.time()
            print(f"\r  scanning... {100.0 * pos / fe:5.1f}%  "
                  f"({sum(len(c[1]) for c in st['vchunks'])} video frames)",
                  end="", flush=True)

    st["pos"] = pos
    if state_path:
        json.dump(st, open(state_path, "w"))
    if not quiet:
        print(f"\r  scanned {100.0 * pos / fe:5.1f}%  "
              f"video frames: {sum(len(c[1]) for c in st['vchunks'])}  "
              f"audio frames: {sum(len(c[1]) for c in st['achunks'])}  "
              f"anomalies: {len(st['anomalies'])}")
    return st


# --------------------------------------------------------------------------
# moov construction
# --------------------------------------------------------------------------

def make_stsc(chunks):
    e = []
    for ci, c in enumerate(chunks, 1):
        n = len(c[1])
        if not e or e[-1][1] != n:
            e.append((ci, n))
    return fullbox(b"stsc", 0, struct.pack(">I", len(e)) +
                   b"".join(struct.pack(">III", f, n, 1) for f, n in e))


def build_stbl(ref, chunks, video):
    sizes = [s for c in chunks for s in c[1]]
    n = len(sizes)
    parts = []
    src_stbl = ref.vstbl if video else ref.astbl
    stsd = find(ref.moov, src_stbl[0], src_stbl[1], ["stsd"])
    parts.append(box(b"stsd", ref.moov[stsd[0]:stsd[1]]))
    delta = ref.vdelta if video else ref.adelta
    parts.append(fullbox(b"stts", 0, struct.pack(">III", 1, n, delta)))
    if video and ref.ctts_period:
        pat = ref.ctts_period
        ents = []
        i = 0
        while i < n:
            v = pat[i % len(pat)]
            run = 1
            while i + run < n and pat[(i + run) % len(pat)] == v:
                run += 1
            ents.append((run, v))
            i += run
        parts.append(fullbox(b"ctts", 0, struct.pack(">I", len(ents)) +
                             b"".join(struct.pack(">Ii", c, o) for c, o in ents)))
    parts.append(make_stsc(chunks))
    parts.append(fullbox(b"stsz", 0, struct.pack(">II", 0, n) +
                         b"".join(struct.pack(">I", s) for s in sizes)))
    parts.append(fullbox(b"co64", 0, struct.pack(">I", len(chunks)) +
                         b"".join(struct.pack(">Q", c[0]) for c in chunks)))
    if video:
        sync, cum = [], 1
        for c in chunks:
            for flag in c[2]:
                if flag:
                    sync.append(cum)
                cum += 1
        if sync and len(sync) < n:
            parts.append(fullbox(b"stss", 0, struct.pack(">I", len(sync)) +
                                 b"".join(struct.pack(">I", s) for s in sync)))
    return box(b"stbl", b"".join(parts))


def build_moov(ref, st, video_only=False, offset_shift=0):
    V = [[c[0] + offset_shift, c[1], c[2]] for c in st["vchunks"]]
    A = [[c[0] + offset_shift, c[1]] for c in st["achunks"]]
    nv = sum(len(c[1]) for c in V)
    na = sum(len(c[1]) for c in A)
    vd = nv * ref.vdelta                                   # video media dur
    ad = na * ref.adelta if A else 0                       # audio media dur
    vd_mv = vd * ref.mvtimescale // ref.vtimescale
    ad_mv = ad * ref.mvtimescale // ref.atimescale if A else 0
    mvd = max(vd_mv, ad_mv)
    for val in (vd, ad, mvd):
        if val > 0xFFFFFFFF:
            raise SystemExit("error: duration overflows 32-bit box fields")

    moov = ref.moov

    def rebuild_trak(s, e, video):
        out = b""
        tkdur = vd_mv if video else ad_mv
        mddur = vd if video else ad
        for t, cs, ce in atoms(moov, s, e):
            raw = moov[cs:ce]
            if t == "tkhd":
                b2 = bytearray(raw)
                struct.pack_into(">I", b2, 20, tkdur)
                out += box(b"tkhd", bytes(b2))
            elif t == "edts":
                el = find(moov, cs, ce, ["elst"])
                if el:
                    b2 = bytearray(moov[el[0]:el[1]])
                    nent = struct.unpack_from(">I", b2, 4)[0]
                    if nent == 1:
                        struct.pack_into(">I", b2, 8, tkdur)
                    out += box(b"edts", box(b"elst", bytes(b2)))
                else:
                    out += box(b"edts", raw)
            elif t == "mdia":
                m = b""
                for t2, ms, me in atoms(moov, cs, ce):
                    if t2 == "mdhd":
                        b2 = bytearray(moov[ms:me])
                        struct.pack_into(">I", b2, 16, mddur)
                        m += box(b"mdhd", bytes(b2))
                    elif t2 == "minf":
                        mi = b""
                        for t3, ns, ne in atoms(moov, ms, me):
                            if t3 == "stbl":
                                mi += build_stbl(ref, V if video else A, video)
                            else:
                                mi += box(t3.encode(), moov[ns:ne])
                        m += box(b"minf", mi)
                    else:
                        m += box(t2.encode(), moov[ms:me])
                out += box(b"mdia", m)
            else:
                out += box(t.encode(), raw)
        return box(b"trak", out)

    out = b""
    for t, s, e in atoms(moov, 8, len(moov)):
        if t == "mvhd":
            b2 = bytearray(moov[s:e])
            struct.pack_into(">I", b2, 16, mvd)
            out += box(b"mvhd", bytes(b2))
        elif t == "trak":
            h = find(moov, s, e, ["mdia", "hdlr"])
            kind = moov[h[0] + 8:h[0] + 12]
            if kind == b"vide":
                out += rebuild_trak(s, e, True)
            elif kind == b"soun":
                if not (video_only or not A):
                    out += rebuild_trak(s, e, False)
            else:
                out += box(b"trak", moov[s:e])
        else:
            out += box(t.encode(), moov[s:e])
    return box(b"moov", out)


# --------------------------------------------------------------------------
# selftest: re-derive the reference's own index and compare
# --------------------------------------------------------------------------

def cmd_selftest(args):
    ref = Ref(args.reference)
    print(f"reference: {ref.summary()}")
    moov = ref.moov
    f = open(args.reference, "rb")
    fe = os.path.getsize(args.reference)

    vsz = read_stsz(moov, ref.vstbl)
    voffs = read_offsets(moov, ref.vstbl)
    si = ok = 0
    nchk = min(len(voffs), 50)
    for ci in range(nchk):
        samples, _, end = walk_video_chunk(f, voffs[ci], fe, hevc=ref.hevc)
        if samples == vsz[si:si + len(samples)]:
            ok += 1
        si += len(samples)
    print(f"video walker : {ok}/{nchk} chunks reproduce the camera's index exactly")
    vok = ok == nchk

    aok = True
    if ref.atrak:
        try:
            dec = AAC(ref.asc)
        except AudioUnavailable as ex:
            print(f"audio        : SKIPPED ({ex})")
            dec = None
        if dec:
            asz = read_stsz(moov, ref.astbl)
            aoffs = read_offsets(moov, ref.astbl)
            stsc = read_stsc(moov, ref.astbl)
            spc = []
            for i, (first, cnt, _) in enumerate(stsc):
                last = stsc[i + 1][0] if i + 1 < len(stsc) else len(aoffs) + 1
                spc += [cnt] * (last - first)
            si = ok = 0
            nchk = min(len(aoffs), 50)
            for ci in range(nchk):
                want = asz[si:si + spc[ci]]
                f.seek(aoffs[ci])
                got = dec.frame_sizes(f.read(sum(want)))
                if got == want:
                    ok += 1
                si += spc[ci]
            print(f"audio decoder: {ok}/{nchk} chunks reproduce the camera's "
                  f"index exactly")
            aok = ok == nchk
    print("PASS -- this reference clip is usable for repair" if vok and aok
          else "FAIL -- do not use this reference; try another clip")
    return 0 if vok and aok else 1


# --------------------------------------------------------------------------
# repair
# --------------------------------------------------------------------------

def _copy_with_progress(src, dst, offset, total):
    BUF = 32 * 1024 * 1024
    done = offset
    last = 0.0
    while done < total:
        chunk = src.read(min(BUF, total - done))
        if not chunk:
            break
        dst.write(chunk)
        done += len(chunk)
        if time.time() - last > 2:
            last = time.time()
            print(f"\r  copying... {100.0 * done / total:5.1f}%", end="", flush=True)
    print(f"\r  copied {done}/{total} bytes          ")
    return done


def cmd_repair(args):
    mdt, refp = args.mdt, args.reference
    if not os.path.exists(mdt):
        raise SystemExit(f"error: {mdt} not found")
    ref = Ref(refp)
    print(f"reference: {ref.summary()}")

    state_path = args.state or mdt + ".scan-state.json"
    st = scan(mdt, ref, state_path, video_only=args.video_only)
    if not st["done"]:
        raise SystemExit("scan incomplete (interrupted?) -- rerun to resume")
    for a in st["anomalies"]:
        print(f"  note: {a}")
    if not st["vchunks"]:
        raise SystemExit("error: no video recovered")

    nv = sum(len(c[1]) for c in st["vchunks"])
    dur = nv * ref.vdelta / ref.vtimescale
    print(f"  recovered {dur:.1f}s ({dur/60:.1f} min) of footage")

    size = os.path.getsize(mdt)
    hdr = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", size)

    if args.in_place:
        stream_start = detect_stream_start(open(mdt, "rb"), size, hevc=ref.hevc)
        if stream_start < 16:
            raise SystemExit("error: no 16-byte header slack; in-place repair "
                             "impossible -- rerun without --in-place")
        moov = build_moov(ref, st, args.video_only)
        sidecar = os.path.splitext(mdt)[0] + ".MP4.mdt-recovery.json"
        with open(mdt, "r+b") as f:
            orig16 = f.read(16)
            json.dump({"file": None, "orig_size": size,
                       "orig_first16_hex": orig16.hex()},
                      open(sidecar, "w"), indent=1)
            f.seek(size)
            f.write(moov)
            f.flush(); os.fsync(f.fileno())
            f.seek(0)
            f.write(hdr)
            f.flush(); os.fsync(f.fileno())
        out = os.path.splitext(mdt)[0] + ".MP4"
        if os.path.exists(out):
            out = os.path.splitext(mdt)[0] + "_repaired.MP4"
        os.rename(mdt, out)
        json.dump({"file": out, "orig_size": size,
                   "orig_first16_hex": orig16.hex(), "orig_name": mdt},
                  open(sidecar, "w"), indent=1)
        print(f"  in-place repair done -> {out}")
        print(f"  undo info saved to {sidecar}")
    else:
        out = args.output or os.path.splitext(mdt)[0] + "_repaired.MP4"
        moov = build_moov(ref, st, args.video_only)
        resume = 0
        if os.path.exists(out):
            resume = os.path.getsize(out)
            if resume >= size:
                resume = 0  # previous complete/overshoot run: start over
                os.unlink(out)
        mode = "r+b" if resume else "wb"
        with open(mdt, "rb") as src, open(out, mode) as dst:
            if resume:
                dst.seek(resume)
                src.seek(resume)
                print(f"  resuming copy at {resume}")
            else:
                dst.write(hdr)
                src.seek(16)
                resume = 16
            done = _copy_with_progress(src, dst, resume, size)
            if done < size:
                raise SystemExit("copy interrupted -- rerun to resume")
            dst.write(moov)
            dst.flush(); os.fsync(dst.fileno())
        print(f"  repair done -> {out}")

    if os.path.exists(state_path):
        os.unlink(state_path)
    print("verify with:  ffprobe " + out)
    return 0


def cmd_undo(args):
    rec = json.load(open(args.sidecar))
    path = rec["file"]
    with open(path, "r+b") as f:
        f.truncate(rec["orig_size"])
        f.seek(0)
        f.write(bytes.fromhex(rec["orig_first16_hex"]))
        f.flush(); os.fsync(f.fileno())
    orig = rec.get("orig_name") or os.path.splitext(path)[0] + ".mdt"
    os.rename(path, orig)
    print(f"restored original: {orig}")
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mdt-repair",
        description="Repair Panasonic LUMIX .MDT interrupted recordings "
                    "using a healthy clip from the same camera as reference.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest",
                       help="validate the method against a healthy clip")
    p.add_argument("reference")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("repair", help="repair an .MDT file")
    p.add_argument("mdt")
    p.add_argument("reference",
                   help="healthy MP4 from the same camera, same settings")
    p.add_argument("-o", "--output", help="output path (copy mode)")
    p.add_argument("--in-place", action="store_true",
                   help="repair the .MDT itself (seconds instead of a full "
                        "copy; reversible via 'undo')")
    p.add_argument("--video-only", action="store_true",
                   help="skip audio recovery (no libavcodec needed)")
    p.add_argument("--state", help="scan checkpoint file (default: "
                                   "MDT.scan-state.json)")
    p.set_defaults(fn=cmd_repair)

    p = sub.add_parser("undo", help="revert an --in-place repair")
    p.add_argument("sidecar", help="the .mdt-recovery.json file")
    p.set_defaults(fn=cmd_undo)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
