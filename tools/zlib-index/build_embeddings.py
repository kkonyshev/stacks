#!/usr/bin/env python3
"""
Pilot embedding generation for the "find similar books" feature.

Reads a deterministic subset of books from the existing zlib_index.sqlite3
(the FTS5 search index), generates embeddings via a local Ollama embedding
model, and stores them in a new, separate sqlite-vec file - kept apart from
the existing search index so the proven FTS5 search feature has zero
exposure to this new code path.

Usage:
    python3 build_embeddings.py \\
        --source zlib_index.sqlite3 \\
        --out zlib_embeddings.sqlite3 \\
        --limit 500000 \\
        --ollama-host http://localhost:11434 \\
        --model bge-m3
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import sqlite_vec


def embed_batch(ollama_host, model, texts):
    """Call Ollama's batch embedding endpoint, return list of vectors."""
    req = urllib.request.Request(
        f"{ollama_host}/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    return body["embeddings"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Path to zlib_index.sqlite3")
    parser.add_argument("--out", required=True, help="Path to write the embeddings sqlite file")
    parser.add_argument("--limit", type=int, default=500000)
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--model", default="bge-m3")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--dims", type=int, default=1024)
    parser.add_argument("--quantize", choices=["float32", "int8"], default="float32",
                         help="Store vectors as float32 (default) or int8 (quantized, ~4x smaller, "
                              "requires embeddings to be unit-normalized)")
    parser.add_argument("--concurrency", type=int, default=1,
                         help="Number of concurrent Ollama batch requests in flight. "
                              "Measured empirically: 2 captures nearly all available speedup "
                              "on this hardware, higher values show no further benefit.")
    args = parser.parse_args()

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    out = sqlite3.connect(args.out)
    out.enable_load_extension(True)
    sqlite_vec.load(out)
    out.enable_load_extension(False)
    out.execute("PRAGMA journal_mode = WAL")
    out.execute("PRAGMA synchronous = NORMAL")
    vec_type = "int8" if args.quantize == "int8" else "float"
    out.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS book_vectors USING vec0(embedding {vec_type}[{args.dims}])")
    out.execute("""
        CREATE TABLE IF NOT EXISTS vector_md5_map (
            rowid INTEGER PRIMARY KEY,
            md5 TEXT UNIQUE NOT NULL
        )
    """)

    rows = src.execute(
        "SELECT rowid, md5, title, author FROM books WHERE md5 IS NOT NULL ORDER BY rowid LIMIT ?",
        (args.limit,),
    ).fetchall()
    total = len(rows)
    print(f"Embedding {total:,} books from {args.source} -> {args.out}", file=sys.stderr)

    start = time.time()
    done = 0
    next_vec_rowid = out.execute("SELECT COALESCE(MAX(rowid), 0) FROM vector_md5_map").fetchone()[0] + 1

    batches = [rows[i:i + args.batch_size] for i in range(0, total, args.batch_size)]

    def fetch_vectors(batch):
        texts = [f"{r['title'] or ''} {r['author'] or ''}".strip() for r in batch]
        try:
            return embed_batch(args.ollama_host, args.model, texts)
        except Exception as e:
            print(f"  batch failed: {e}", file=sys.stderr)
            return None

    # ThreadPoolExecutor.map keeps results in submission order even though
    # the underlying requests run concurrently, so rowid assignment below
    # stays deterministic - all SQLite writes stay on this single thread.
    executor = ThreadPoolExecutor(max_workers=args.concurrency)
    for batch, vectors in zip(batches, executor.map(fetch_vectors, batches)):
        if vectors is None:
            continue

        for row, vec in zip(batch, vectors):
            vec_bytes = sqlite_vec.serialize_float32(vec)
            if args.quantize == "int8":
                # vec_quantize_int8's result must be consumed within the same
                # SQL statement - its type tag doesn't survive a Python
                # round-trip through a separate SELECT + re-INSERT.
                out.execute(
                    "INSERT INTO book_vectors(rowid, embedding) VALUES (?, vec_quantize_int8(?, 'unit'))",
                    (next_vec_rowid, vec_bytes),
                )
            else:
                out.execute(
                    "INSERT INTO book_vectors(rowid, embedding) VALUES (?, ?)",
                    (next_vec_rowid, vec_bytes),
                )
            out.execute(
                "INSERT OR REPLACE INTO vector_md5_map(rowid, md5) VALUES (?, ?)",
                (next_vec_rowid, row["md5"]),
            )
            next_vec_rowid += 1

        done += len(batch)
        if done % 5000 < args.batch_size:
            elapsed = time.time() - start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  {done:,}/{total:,} ({rate:.1f}/s, elapsed {elapsed:.0f}s, ETA {eta:.0f}s)", file=sys.stderr)
            out.commit()

    executor.shutdown()
    out.commit()
    elapsed = time.time() - start
    print(f"Done: {done:,} embedded in {elapsed:.1f}s ({done/elapsed:.1f}/s) -> {args.out}", file=sys.stderr)
    out.close()
    src.close()


if __name__ == "__main__":
    main()
