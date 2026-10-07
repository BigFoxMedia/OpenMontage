#!/usr/bin/env python3
"""Extract embedded video clips from Live-Photo style JPEG containers (MPO).

Some cameras/apps store "Live Photos" as a single .jpg: the main still image
plus one or more MP4/MOV containers appended inside the same file (CIPA MPO
multi-picture object layout). This script finds and carves out every embedded
video container, writes them as standalone .mp4 files, and (optionally)
extracts the first still frame as a plain JPEG.

It then runs ffprobe on each extracted clip and prints a summary report so the
agent can plan the edit from real data (durations, resolutions, codecs, audio).

Usage:
    python scripts/extract_live_photos.py <input_dir_or_files...> <output_dir>
        [--stills]          Also extract the first MPO image as <name>.jpg
        [--json PATH]       Also write the full report as JSON

Exit codes:
    0  success (at least one clip extracted, or inputs had no clips)
    1  usage error
    2  ffprobe missing (clips still extracted; report incomplete)

Notes:
    - Works on plain JPEG MPO Live Photos (the common "live.jpg" case).
      HEIC Live Photos need a different parser (not supported here).
    - Embedded containers are identified by ISO-BMFF `ftyp` boxes with known
      brands. Top-level box walking handles size==1 (64-bit) and size==0
      (to-EOF) mdat boxes, which naive parsers miss.
    - Clips are named <basename>_clip0.mp4, <basename>_clip1.mp4, ... in
      file order. For typical Live Photo JPEGs: clip0 is a short silent
      preview (~0.5s), clip1 is the full live clip with audio (~3s).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ISO-BMFF brands we accept as a real video container start.
_BRANDS = frozenset(
    [
        b"mp42", b"isom", b"mp41", b"qt  ", b"M4V ", b"3gpp", b"avc1",
        b"3g2", b"mmp4", b"msdv",
    ]
)

# Top-level box names in an ISO-BMFF file.
_KNOWN_BOXES = frozenset(
    [b"ftyp", b"moov", b"mdat", b"free", b"uuid", b"moof", b"mfra", b"mvex",
     b"wide", b"skip", b"iods", b"stbl", b"meta", b"pnot", b"wide"]
)


def _walk_top_level_boxes(data: bytes, start: int) -> tuple[list[tuple[str, int, int]], int]:
    """Walk top-level ISO-BMFF boxes from `start`. Returns (boxes, end_offset).

    Handles the two non-obvious size encodings that break naive parsers:
      size == 1  -> the real size is the 64-bit `largesize` field after the
                    box name (common for big mdat boxes)
      size == 0  -> the box extends to the end of the file
    """
    pos = start
    boxes: list[tuple[str, int, int]] = []
    while pos + 16 <= len(data):
        size = int.from_bytes(data[pos:pos + 4], "big")
        name = data[pos + 4:pos + 8]
        if name not in _KNOWN_BOXES:
            break
        if size == 1:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
        elif size == 0:
            size = len(data) - pos
        if size < 8 or pos + size > len(data):
            break
        boxes.append((name.decode("latin1"), pos, size))
        pos += size
    return boxes, pos


def find_embedded_containers(data: bytes) -> list[tuple[int, int]]:
    """Return (start, end) offsets of every embedded ISO-BMFF container."""
    containers: list[tuple[int, int]] = []
    pos = 0
    while True:
        pos = data.find(b"ftyp", pos)
        if pos == -1 or pos < 4:
            break
        brand = data[pos + 4:pos + 8]
        if brand not in _BRANDS:
            pos += 4
            continue
        start = pos - 4  # the 4-byte size field precedes the 'ftyp' name
        boxes, end = _walk_top_level_boxes(data, start)
        if len(boxes) < 2:  # a lone ftyp is not a container
            pos += 4
            continue
        containers.append((start, end))
        pos = max(end, start + 100)
    return containers


def probe_clip(path: Path) -> dict:
    """ffprobe a clip -> {duration, resolution, fps, vcodec, acodec}. Best effort."""
    out = {"duration_seconds": None, "resolution": None, "fps": None,
           "vcodec": None, "acodec": None}
    if shutil.which("ffprobe") is None:
        return out
    try:
        dur = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if dur:
            out["duration_seconds"] = round(float(dur), 3)
        v = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if v:
            parts = [p.strip() for p in v.split(",")]
            out["vcodec"] = parts[0] or None
            if len(parts) >= 3 and parts[1] and parts[2] and parts[1] != "0":
                out["resolution"] = f"{parts[1]}x{parts[2]}"
            if len(parts) >= 4 and parts[3] and "/" in parts[3]:
                num, den = parts[3].split("/")
                try:
                    out["fps"] = round(int(num) / int(den), 2)
                except (ValueError, ZeroDivisionError):
                    pass
        a = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        out["acodec"] = a or None
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        out["error"] = str(exc)
    return out


def extract_still(src: Path, dst: Path) -> Optional[str]:
    """Save the first MPO frame as a plain JPEG. None on failure (caller may
    still use the original .jpg directly)."""
    try:
        from PIL import Image
        with Image.open(src) as img:
            if hasattr(img, "load_frame"):
                img.load_frame(0)
            img.convert("RGB").save(dst, "JPEG", quality=92)
            return f"{img.size[0]}x{img.size[1]}"
    except Exception:
        return None


def process_file(src: Path, out_dir: Path, extract_stills: bool) -> dict:
    data = src.read_bytes()
    containers = find_embedded_containers(data)
    report = {
        "input": str(src),
        "size_bytes": len(data),
        "clips": [],
        "still": None,
        "has_embedded_video": bool(containers),
    }

    for i, (start, end) in enumerate(containers):
        clip_path = out_dir / f"{src.stem}_clip{i}.mp4"
        clip_path.write_bytes(data[start:end])
        report["clips"].append({
            "path": str(clip_path),
            "bytes": end - start,
            "probe": probe_clip(clip_path),
        })

    if extract_stills:
        still_path = out_dir / f"{src.stem}_still.jpg"
        res = extract_still(src, still_path)
        if res:
            report["still"] = {"path": str(still_path), "resolution": res}
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="input dir(s) and/or Live Photo .jpg file(s)")
    parser.add_argument("output_dir", help="directory for extracted clips/stills")
    parser.add_argument("--stills", action="store_true",
                        help="also extract the first MPO image as a plain .jpg")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the full report as JSON here")
    args = parser.parse_args(argv)

    inputs: list[Path] = []
    for raw in args.inputs:
        p = Path(raw)
        if p.is_dir():
            for pat in ("*.jpg", "*.jpeg", "*.JPG", "*.JPEG", "*.heic"):
                inputs.extend(sorted(p.glob(pat)))
        elif p.is_file():
            inputs.append(p)
        else:
            print(f"ERROR: not found: {raw}", file=sys.stderr)
            return 1
    if not inputs:
        print("ERROR: no input files found", file=sys.stderr)
        return 1

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    reports = [process_file(src, out_dir, args.stills) for src in inputs]

    total_clips = sum(len(r["clips"]) for r in reports)
    total_seconds = sum(
        (c["probe"]["duration_seconds"] or 0.0)
        for r in reports for c in r["clips"]
    )

    print(f"Scanned {len(reports)} file(s), extracted {total_clips} clip(s), "
          f"~{total_seconds:.1f}s of motion total")
    for r in reports:
        if not r["has_embedded_video"]:
            print(f"  {Path(r['input']).name}: no embedded video")
            continue
        for c in r["clips"]:
            p = c["probe"]
            print(f"  {Path(r['input']).name} -> {Path(c['path']).name}: "
                  f"{p['duration_seconds']}s {p['resolution']} {p['vcodec']} "
                  f"audio={p['acodec'] or 'none'}")

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(reports, indent=2), encoding="utf-8")

    if shutil.which("ffprobe") is None:
        print("WARNING: ffprobe not found — clips extracted but unverified", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
