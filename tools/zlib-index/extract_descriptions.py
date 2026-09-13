#!/usr/bin/env python3
"""
Extract md5 + description from the raw AAC zlib3_records source into a small,
separate SQLite table - kept apart from zlib_index.sqlite3 so it can be
joined in later (by md5) without rewriting the existing index.

By default processes the entire source file. Pass --limit to stop early
after collecting a fixed number of non-empty descriptions (useful for
small pilots without waiting on a full pass).

For parallel runs, use --dump-offsets once to precompute the (expensive,
whole-file) zstd frame index, then launch N workers with --offsets-file,
--shard-index and --shard-count so each only decodes its own slice of
frames - avoiding paying the full-file scan cost once per worker.

Usage:
    # single process, full file
    python3 extract_descriptions.py <source.jsonl.zst> --db out.sqlite3

    # parallel: precompute offsets once, then 4 workers
    python3 extract_descriptions.py <source.jsonl.zst> --dump-offsets offsets.txt
    python3 extract_descriptions.py <source.jsonl.zst> --offsets-file offsets.txt \\
        --shard-index 0 --shard-count 4 --db shard0.sqlite3
"""
import argparse
import json
import mmap
import shutil
import sqlite3
import subprocess
import sys
import time

ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"

SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptions (
    md5 TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
"""


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


def iter_records_for_offsets(path, offsets, all_offsets_len_hint=None):
    """Decode only the given frame start-offsets (a subset assigned to this
    shard). Needs the *full* sorted offsets list to know each frame's end
    boundary, so callers pass the complete list plus which indices to decode."""
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for start, end in offsets:
                chunk = mm[start:end]
                proc = subprocess.run(
                    ["zstd", "-dc"],
                    input=chunk,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if proc.returncode != 0 or not proc.stdout:
                    continue
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        finally:
            mm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="zlib3_records .jsonl.zst source file")
    parser.add_argument("--db", default="zlib_descriptions.sqlite3")
    parser.add_argument("--limit", type=int, default=None,
                         help="Stop after collecting this many non-empty descriptions (default: no limit, process the whole file)")
    parser.add_argument("--min-free-gb", type=float, default=5.0,
                         help="Abort cleanly (keeping what's extracted so far) if free space on "
                              "the DB's volume drops below this many GB")
    parser.add_argument("--dump-offsets", default=None,
                         help="Compute the full-file zstd frame offset index and write it to this "
                              "path (one integer per line), then exit without extracting anything")
    parser.add_argument("--offsets-file", default=None,
                         help="Load precomputed frame offsets from this file instead of rescanning "
                              "the whole file (produced by --dump-offsets)")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()

    if args.dump_offsets:
        with open(args.file, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            offsets = find_frame_offsets(mm)
            mm.close()
        with open(args.dump_offsets, "w") as out:
            out.write("\n".join(str(o) for o in offsets))
        print(f"Dumped {len(offsets):,} frame offsets -> {args.dump_offsets}", file=sys.stderr)
        return

    if args.offsets_file:
        with open(args.offsets_file) as f:
            all_offsets = [int(line) for line in f if line.strip()]
    else:
        with open(args.file, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            all_offsets = find_frame_offsets(mm)
            mm.close()

    import os
    file_size = os.path.getsize(args.file)
    total_frames = len(all_offsets)
    # Pair each start offset with its end offset (next frame's start, or EOF),
    # computed against the *full* list, then keep only this shard's frames -
    # round-robin so each shard gets an even mix of early/late frames.
    bounded = [
        (all_offsets[i], all_offsets[i + 1] if i + 1 < total_frames else file_size)
        for i in range(total_frames)
    ]
    my_frames = bounded[args.shard_index::args.shard_count]
    print(f"Shard {args.shard_index}/{args.shard_count}: {len(my_frames):,}/{total_frames:,} frames",
          file=sys.stderr)

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    start = time.time()
    count = 0
    scanned = 0
    low_space = False
    for rec in iter_records_for_offsets(args.file, my_frames):
        scanned += 1
        meta = rec.get("metadata", {})
        md5 = meta.get("md5_reported") or meta.get("md5")
        desc = meta.get("description")
        if md5 and desc:
            conn.execute(
                "INSERT OR IGNORE INTO descriptions(md5, description) VALUES (?, ?)",
                (md5, desc),
            )
            count += 1
            if count % 10000 == 0:
                conn.commit()
                elapsed = time.time() - start
                limit_str = f"{args.limit:,}" if args.limit else "?"
                print(f"  [shard {args.shard_index}] {count:,}/{limit_str} descriptions ({scanned:,} scanned, {elapsed:.0f}s)",
                      file=sys.stderr)
                free_gb = shutil.disk_usage(".").free / 1e9
                if free_gb < args.min_free_gb:
                    print(f"  [shard {args.shard_index}] free space down to {free_gb:.1f}GB (below "
                          f"--min-free-gb {args.min_free_gb}) - stopping safely, keeping what's extracted so far",
                          file=sys.stderr)
                    low_space = True
                    break
            if args.limit and count >= args.limit:
                break

    conn.commit()
    conn.close()
    elapsed = time.time() - start
    print(f"Done: [shard {args.shard_index}] {count:,} descriptions from {scanned:,} scanned records in {elapsed:.0f}s -> {args.db}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
