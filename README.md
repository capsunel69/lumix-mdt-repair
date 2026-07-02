# lumix-mdt-repair

Recover Panasonic LUMIX `.MDT` files — interrupted recordings from GH5, GH5S, G9, S-series and similar cameras — **with both video and audio, at full original quality**.

## What is an .MDT file?

When a LUMIX camera loses power (dead battery, card failure, hard shutdown) mid-recording, it leaves behind a `.MDT` file instead of an `.MP4`. That file is the raw `mdat` payload of an MP4: all of your footage is in there, but the index (`moov` box) that tells players where every frame lives was never written. The camera offers to repair it on next boot, but this often fails or the option never appears.

Generic recovery tools struggle with these files. In the case that motivated this tool (a 70 GB, 98-minute GH5 V-Log clip), `untrunc` recovered video but silently **dropped the entire AAC audio track** and mis-stamped frame timing, inflating the apparent duration by 10 minutes.

## How it works

The tool rebuilds the index from scratch by scanning the interleaved stream:

- **Video** — LUMIX MP4s store H.264 in AVCC form (length-prefixed NAL units), and every frame begins with an Access Unit Delimiter NAL. The scanner hops NAL-to-NAL, grouping frames and recording sample sizes, keyframe positions, and chunk offsets. Because it seeks past frame payloads instead of reading them, scanning is fast even on huge files.
- **Audio** — raw AAC frames have no sync markers, so boundaries can't be found by parsing alone. The tool drives your system's `libavcodec` directly (via `ctypes`, no compilation needed): the decoder reports how many bytes each frame consumed — exact frame boundaries, the same technique `untrunc` uses internally.
- **Timing and metadata** — frame rate, B-frame reordering pattern (`ctts`), edit lists, and codec configuration are derived from a **healthy reference clip** shot on the same camera with the same settings. The `moov` is built from the reference as a template, so all camera metadata semantics carry over.

The killer feature is `selftest`: before touching your broken file, the tool re-derives the reference clip's *own* index from its raw `mdat` and compares it **byte-for-byte** against the index the camera wrote. If that passes, the method is proven for your exact camera and settings.

## Requirements

- Python 3.8+ (no Python packages needed)
- For audio recovery: **ffmpeg 4.x shared libraries** (`libavcodec` 57 or 58). ffmpeg 5+ removed the API that reports per-frame bytes consumed.
  - Debian/Ubuntu 20.04/22.04: `sudo apt install libavcodec58`
  - macOS: `brew install ffmpeg@4`
  - Or use `--video-only` (no libraries needed at all)

## Usage

```bash
# 1. Prove the method works for your camera/settings (uses a healthy clip)
python3 mdt_repair.py selftest P1448719.MP4

# 2. Repair (safe default: writes a new file, original .MDT untouched)
python3 mdt_repair.py repair P1448720.MDT P1448719.MP4

# Fast path for huge files: repair in place (seconds instead of copying
# the whole file; fully reversible)
python3 mdt_repair.py repair P1448720.MDT P1448719.MP4 --in-place

# Changed your mind? Restore the original .MDT byte-for-byte
python3 mdt_repair.py undo P1448720.MP4.mdt-recovery.json
```

Optionally, `pip install .` from the repo root installs an `mdt-repair` command that works the same way.

The reference clip must come from the **same camera with the same settings** (resolution, frame rate, codec, audio format). The clip recorded immediately before or after the interrupted one is ideal.

Scans are resumable: if interrupted, rerun the same command and it continues from the checkpoint.

## Safety notes

- Default mode never modifies the `.MDT`; it builds a separate repaired file (needs free space equal to the broken file's size).
- `--in-place` modifies only the first 16 bytes (the unfinalized `mdat` header) and appends the new index — nothing in between is touched. The original 16 bytes and file length are saved to a sidecar JSON, and `undo` restores the original exactly.
- Work on a copy of your card. Never write to the card an interrupted recording is on until you're done.

## Limitations

- H.264 (AVCC) video + AAC audio in MP4, chunk-interleaved with AUD-delimited frames — the standard LUMIX MP4 layout. MOV recordings with LPCM audio are not supported.
- Constant frame rate / constant GOP timing (true of LUMIX internal recording).
- Rebuilt duration fields must fit 32-bit MP4 boxes (< ~13 hours at 90 kHz timescale).
- If a stretch of audio is genuinely corrupted, its frame boundaries are estimated and flagged; that region may glitch but the stream stays in sync.

## Verifying results

```bash
ffprobe repaired.MP4                     # streams + duration
ffmpeg -v error -i repaired.MP4 -f null -   # full decode check
```

## License

MIT — see [LICENSE](LICENSE).
