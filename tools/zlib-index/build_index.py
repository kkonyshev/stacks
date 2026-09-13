#!/usr/bin/env python3
"""
Build a local SQLite search index from Anna's Archive Containers (AAC)
"zlib3_records" metadata dumps (annas_archive_meta__aacid__zlib3_records__*.jsonl.seekable.zst).

Usage:
    python3 build_index.py <path-to-jsonl.zst-file> [<more files>...] [--db zlib_index.sqlite3]

The input file(s) are the raw torrent payload (a JSON-Lines file compressed with
Zstandard). Decompression is streamed through the `zstd` CLI, so the file never
needs to be fully decompressed to disk.
"""
import argparse
import json
import mmap
import shutil
import sqlite3
import subprocess
import sys
import time

# zstd frame magic number (0xFD2FB528), little-endian byte order as it
# appears in the file.
ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    aacid TEXT PRIMARY KEY,
    zlibrary_id TEXT,
    md5 TEXT,
    title TEXT,
    author TEXT,
    publisher TEXT,
    language TEXT,
    extension TEXT,
    year TEXT,
    filesize INTEGER,
    isbns TEXT
);
CREATE INDEX IF NOT EXISTS idx_books_md5 ON books(md5);

CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, author, publisher,
    content='books', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS books_ai AFTER INSERT ON books BEGIN
    INSERT INTO books_fts(rowid, title, author, publisher)
    VALUES (new.rowid, new.title, new.author, new.publisher);
END;
"""


def find_frame_offsets(mm):
    """Locate every zstd frame start in the (possibly gappy) file."""
    offsets = []
    start = 0
    while True:
        idx = mm.find(ZSTD_FRAME_MAGIC, start)
        if idx == -1:
            break
        offsets.append(idx)
        start = idx + 4
    return offsets


def iter_records(path):
    """
    Decompress a (possibly incomplete) AAC .jsonl.seekable.zst file.

    The file is a concatenation of independent zstd frames. A torrent that's
    downloaded but missing some pieces will have zero-filled gaps at those
    byte ranges; any frame overlapping a gap fails to decompress on its own
    and is skipped, without losing the rest of the file the way a single
    whole-file `zstd -dc` pass would (it aborts at the first bad byte).
    """
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            offsets = find_frame_offsets(mm)
            total_frames = len(offsets)
            bad_frames = 0
            for i, start in enumerate(offsets):
                end = offsets[i + 1] if i + 1 < total_frames else len(mm)
                chunk = mm[start:end]
                proc = subprocess.run(
                    ["zstd", "-dc"],
                    input=chunk,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if proc.returncode != 0 or not proc.stdout:
                    bad_frames += 1
                    continue
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        # A frame that overlaps a missing-piece gap can decode
                        # "successfully" but yield a truncated final line.
                        continue
            print(
                f"  frames: {total_frames - bad_frames} decoded, "
                f"{bad_frames} unreadable (gaps/missing pieces)",
                file=sys.stderr,
            )
        finally:
            mm.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", help="zlib3_records .jsonl.zst file(s)")
    parser.add_argument("--db", default="zlib_index.sqlite3")
    parser.add_argument(
        "--min-free-gb", type=float, default=5.0,
        help="Abort cleanly (keeping what's indexed so far) if free space on "
             "the DB's volume drops below this many GB",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA journal_mode = MEMORY")

    count = 0
    start = time.time()
    batch = []
    BATCH_SIZE = 5000
    low_space = False

    def flush():
        nonlocal batch
        if not batch:
            return
        conn.executemany(
            """INSERT OR IGNORE INTO books
               (aacid, zlibrary_id, md5, title, author, publisher, language,
                extension, year, filesize, isbns)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            batch,
        )
        conn.commit()
        batch = []

    for path in args.files:
        if low_space:
            break
        print(f"Indexing {path} ...", file=sys.stderr)
        for rec in iter_records(path):
            meta = rec.get("metadata", {})
            md5 = meta.get("md5_reported") or meta.get("md5")
            batch.append((
                rec.get("aacid"),
                str(meta.get("zlibrary_id", "")),
                md5,
                meta.get("title", ""),
                meta.get("author", ""),
                meta.get("publisher", ""),
                meta.get("language", ""),
                meta.get("extension", ""),
                str(meta.get("year", "")),
                meta.get("filesize_reported"),
                json.dumps(meta.get("isbns", [])),
            ))
            count += 1
            if len(batch) >= BATCH_SIZE:
                flush()
            if count % 500000 == 0:
                elapsed = time.time() - start
                print(f"  {count:,} records in {elapsed:.0f}s", file=sys.stderr)
                free_gb = shutil.disk_usage(".").free / 1e9
                if free_gb < args.min_free_gb:
                    print(
                        f"  free space down to {free_gb:.1f}GB (below "
                        f"--min-free-gb {args.min_free_gb}) - stopping safely, "
                        f"keeping what's indexed so far",
                        file=sys.stderr,
                    )
                    low_space = True
                    break

    flush()
    conn.execute("INSERT INTO books_fts(books_fts) VALUES('optimize')")
    conn.commit()
    conn.close()

    elapsed = time.time() - start
    print(f"Done: {count:,} records indexed in {elapsed:.0f}s -> {args.db}", file=sys.stderr)


if __name__ == "__main__":
    main()
