#!/usr/bin/env python3
"""
Migrate an existing embeddings index into a new file whose vec0 table uses
`language` as a partition key, so language-filtered similarity queries only
scan that language's subset instead of the entire corpus.

Doesn't touch the embedding vectors themselves (no re-embedding) - just
copies each (rowid, md5, embedding) row into the repartitioned schema,
looking up each book's language from the main FTS5 index. The whole copy
happens inside a single SQL statement per batch (source attached to the
output connection) so the vector blob never passes through Python as a
plain bytes object - sqlite-vec's internal type tag doesn't survive that
round-trip (same gotcha as vec_quantize_int8 in build_embeddings.py).

Usage:
    python3 repartition_by_language.py \\
        --source zlib_embeddings_full.sqlite3 \\
        --index zlib_index.sqlite3 \\
        --out zlib_embeddings_full_by_lang.sqlite3
"""
import argparse
import sqlite3
import sys
import time

import sqlite_vec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="Existing embeddings index (not partitioned)")
    parser.add_argument("--index", required=True, help="zlib_index.sqlite3, for language lookup")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dims", type=int, default=768)
    parser.add_argument("--batch-size", type=int, default=50000)
    args = parser.parse_args()

    out = sqlite3.connect(args.out)
    out.enable_load_extension(True)
    sqlite_vec.load(out)
    out.enable_load_extension(False)
    out.execute("PRAGMA journal_mode = WAL")
    out.execute("PRAGMA synchronous = NORMAL")
    out.execute("ATTACH DATABASE ? AS src", (args.source,))
    out.execute("ATTACH DATABASE ? AS idx", (args.index,))
    out.execute(f"CREATE VIRTUAL TABLE book_vectors USING vec0(language TEXT PARTITION KEY, embedding int8[{args.dims}])")
    out.execute("CREATE TABLE vector_md5_map (rowid INTEGER PRIMARY KEY, md5 TEXT UNIQUE NOT NULL)")

    total, max_rowid = out.execute("SELECT COUNT(*), MAX(rowid) FROM src.vector_md5_map").fetchone()
    print(f"Migrating {total:,} vectors (max rowid {max_rowid:,}) -> {args.out}", file=sys.stderr)

    start = time.time()
    done = 0
    batch = args.batch_size
    for range_start in range(0, max_rowid + 1, batch):
        range_end = range_start + batch

        out.execute("""
            INSERT INTO book_vectors(rowid, language, embedding)
            SELECT m.rowid,
                   COALESCE((SELECT b.language FROM idx.books b WHERE b.md5 = m.md5 LIMIT 1), 'unknown'),
                   v.embedding
            FROM src.vector_md5_map m
            JOIN src.book_vectors v ON v.rowid = m.rowid
            WHERE m.rowid > ? AND m.rowid <= ?
        """, (range_start, range_end))
        n = out.execute("""
            INSERT INTO vector_md5_map(rowid, md5)
            SELECT rowid, md5 FROM src.vector_md5_map WHERE rowid > ? AND rowid <= ?
        """, (range_start, range_end)).rowcount
        out.commit()

        done += n
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  {done:,}/{total:,} ({rate:.0f}/s, elapsed {elapsed:.0f}s, ETA {eta:.0f}s)", file=sys.stderr)

    out.close()
    elapsed = time.time() - start
    print(f"Done: {done:,} migrated in {elapsed:.1f}s -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
