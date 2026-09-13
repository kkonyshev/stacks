#!/usr/bin/env python3
"""
Cut a prefix of an AAC .jsonl.seekable.zst file into a smaller, independently
valid file of the same format - for quickly testing the build pipeline (see
README.md) without downloading/processing a full multi-GB metadata torrent.

Works because the format is just a concatenation of independent zstd frames
(the same property build_index.py/extract_descriptions.py rely on for
tolerating a torrent with missing pieces) - a prefix of whole frames is
itself a valid file of the same format, just shorter.

Usage:
    # Take roughly 1/20th of the file
    python3 make_test_slice.py source.jsonl.seekable.zst --fraction 20 --out zlib_01.jsonl.seekable.zst

    # Take an exact frame count instead
    python3 make_test_slice.py source.jsonl.seekable.zst --frames 762 --out zlib_01.jsonl.seekable.zst
"""
import argparse
import mmap
import sys

ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"


def find_frame_offsets(mm):
    offsets = []
    start = 0
    while True:
        idx = mm.find(ZSTD_FRAME_MAGIC, start)
        if idx == -1:
            break
        offsets.append(idx)
        start = idx + 4
    return offsets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Source .jsonl.seekable.zst file")
    parser.add_argument("--out", required=True)
    parser.add_argument("--fraction", type=int, help="Take 1/N of the frames, e.g. 20 for 1/20th")
    parser.add_argument("--frames", type=int, help="Take exactly this many frames instead of --fraction")
    args = parser.parse_args()

    if not args.frames and not args.fraction:
        parser.error("specify --fraction or --frames")

    with open(args.source, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            offsets = find_frame_offsets(mm)
            total = len(offsets)
            n = args.frames if args.frames else max(1, total // args.fraction)
            n = min(n, total)
            end_byte = offsets[n] if n < total else len(mm)
            print(f"Taking {n:,}/{total:,} frames ({end_byte/1e9:.2f}GB of {len(mm)/1e9:.2f}GB)", file=sys.stderr)
            with open(args.out, "wb") as out:
                out.write(mm[0:end_byte])
        finally:
            mm.close()
    print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
