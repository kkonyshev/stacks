#!/usr/bin/env python3
"""
Embed book title+author+description using EmbeddingGemma's proper asymmetric
retrieval format. Joins the existing zlib_index.sqlite3 (title/author) with a
zlib_descriptions*.sqlite3 file (md5 -> description) via ATTACH.

Per Google's EmbeddingGemma docs, documents and queries use different prompt
prefixes so both land in the same retrieval-optimized space:
    document: "title: {title|'none'} | text: {content}"
    query:    "task: search result | query: {content}"
This script embeds the DOCUMENT side only - queries are prefixed separately
at search time (see search_similar.py).

For parallel runs, launch N processes with --shard-index/--shard-count (SQL
modulo on rowid, so no precomputation needed) each writing to its own --out
file, then merge with merge_embeddings.py.

Usage:
    python3 build_embeddings_desc.py \\
        --source zlib_index.sqlite3 \\
        --descriptions zlib_descriptions_full.sqlite3 \\
        --out zlib_embeddings_shard0.sqlite3 \\
        --model embeddinggemma --dims 768 --quantize int8 --concurrency 4 \\
        --shard-index 0 --shard-count 8
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import sqlite_vec

# Placeholder/boilerplate values that show up in place of a real description
# in the source metadata (e.g. the literal string "null", not an actual NULL,
# or scraper-added notices) - checked in main() before assembling embed text
# so these books fall back to title+author instead of polluting the vector
# with junk. Discovered by inspecting the most-duplicated description values
# in the extracted corpus (~5-6% of all descriptions matched one of these).
JUNK_DESCRIPTIONS = {"null", "none", "n/a", "", "-", "()", "]]>"}
JUNK_ANYWHERE = ("downloaded from", "z-lib.org", "z-library")
JUNK_PREFIXES = ("includes index", "includes bibliographical reference")


def is_junk_description(desc):
    if not desc:
        return True
    d = desc.strip().lower()
    if len(d) < 20:
        return True
    if d in JUNK_DESCRIPTIONS:
        return True
    if any(s in d for s in JUNK_ANYWHERE):
        return True
    return d.startswith(JUNK_PREFIXES)


def embed_batch(ollama_host, model, texts):
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
    parser.add_argument("--descriptions", required=True, help="Path to zlib_descriptions*.sqlite3")
    parser.add_argument("--out", required=True)
    parser.add_argument("--ollama-host", default="http://localhost:11434")
    parser.add_argument("--model", default="embeddinggemma")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--dims", type=int, default=768)
    parser.add_argument("--desc-chars", type=int, default=500,
                         help="Truncate description to this many chars before embedding")
    parser.add_argument("--quantize", choices=["float32", "int8"], default="int8")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None,
                         help="Cap the number of books embedded per shard (default: no limit)")
    parser.add_argument("--anchor-md5s", default="",
                         help="Comma-separated md5s to guarantee are included in a limited sample")
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.execute("ATTACH DATABASE ? AS descs", (args.descriptions,))
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

    anchors = [m.strip().lower() for m in args.anchor_md5s.split(",") if m.strip()]
    where_extra = ""
    params = []
    if anchors:
        placeholders = ",".join("?" for _ in anchors)
        where_extra = f" OR LOWER(b.md5) IN ({placeholders})"
        params = anchors

    shard_clause = ""
    shard_params = []
    if args.shard_count > 1:
        shard_clause = "AND (b.rowid % ? = ?" + (where_extra if anchors else "") + ")"
        shard_params = [args.shard_count, args.shard_index] + (anchors if anchors else [])

    # With no --limit, everything eventually gets embedded anyway, so plain
    # rowid order is fine. With --limit, an anchor md5 could easily fall
    # outside the first N rows by rowid - order it first so it's always
    # included regardless of shard/limit combination.
    order_sql = "ORDER BY b.rowid"
    order_params = []
    if anchors and args.limit:
        anchor_placeholders = ",".join("?" for _ in anchors)
        order_sql = f"ORDER BY CASE WHEN LOWER(b.md5) IN ({anchor_placeholders}) THEN 0 ELSE 1 END, b.rowid"
        order_params = anchors

    limit_sql = "LIMIT ?" if args.limit else ""
    limit_params = [args.limit] if args.limit else []

    # Resumption: --out's vector_md5_map (created above, empty on a fresh run)
    # already lists every md5 embedded so far - skip those instead of
    # re-fetching and re-embedding everything from scratch after a crash/restart.
    src.execute("ATTACH DATABASE ? AS outdb", (args.out,))
    done_count = out.execute("SELECT COUNT(*) FROM vector_md5_map").fetchone()[0]
    resume_clause = "AND NOT EXISTS (SELECT 1 FROM outdb.vector_md5_map m WHERE m.md5 = b.md5)" if done_count else ""

    rows = src.execute(f"""
        SELECT b.md5, b.title, b.author, d.description
        FROM books b
        JOIN descs.descriptions d ON d.md5 = b.md5
        WHERE b.md5 IS NOT NULL
        {shard_clause}
        {resume_clause}
        GROUP BY b.md5
        {order_sql}
        {limit_sql}
    """, shard_params + order_params + limit_params).fetchall()
    if done_count:
        print(f"[shard {args.shard_index}] Resuming: {done_count:,} already embedded, {len(rows):,} remaining",
              file=sys.stderr)
    total = len(rows)
    print(f"[shard {args.shard_index}/{args.shard_count}] Embedding {total:,} books -> {args.out}", file=sys.stderr)

    start = time.time()
    done = 0
    next_vec_rowid = out.execute("SELECT COALESCE(MAX(rowid), 0) FROM vector_md5_map").fetchone()[0] + 1

    batches = [rows[i:i + args.batch_size] for i in range(0, total, args.batch_size)]

    def fetch_vectors(batch):
        texts = []
        for r in batch:
            raw_desc = r["description"] or ""
            desc = "" if is_junk_description(raw_desc) else raw_desc[:args.desc_chars]
            title = r["title"] or "none"
            content = f"{r['author'] or ''}. {desc}".strip()
            # EmbeddingGemma's document-side asymmetric-retrieval prefix format.
            texts.append(f"title: {title} | text: {content}")
        try:
            return embed_batch(args.ollama_host, args.model, texts)
        except Exception as e:
            print(f"  [shard {args.shard_index}] batch failed: {e}", file=sys.stderr)
            return None

    executor = ThreadPoolExecutor(max_workers=args.concurrency)
    for batch, vectors in zip(batches, executor.map(fetch_vectors, batches)):
        if vectors is None:
            continue

        for row, vec in zip(batch, vectors):
            vec_bytes = sqlite_vec.serialize_float32(vec)
            if args.quantize == "int8":
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
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  [shard {args.shard_index}] {done:,}/{total:,} ({rate:.1f}/s, elapsed {elapsed:.0f}s, ETA {eta:.0f}s)", file=sys.stderr)
        out.commit()

    executor.shutdown()
    out.commit()
    elapsed = time.time() - start
    print(f"Done: [shard {args.shard_index}] {done:,} embedded in {elapsed:.1f}s ({done/elapsed:.1f}/s) -> {args.out}", file=sys.stderr)
    out.close()
    src.close()


if __name__ == "__main__":
    main()
