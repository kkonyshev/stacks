#!/usr/bin/env python3
"""
Search the local zlib metadata index built by build_index.py.

Usage:
    python3 search.py "dune frank herbert" [--db zlib_index.sqlite3] [--limit 20]

Prints matches as: md5  ext  size  language  year  |  title — author
"""
import argparse
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--db", default="zlib_index.sqlite3")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    fts_query = " ".join(f'"{tok}"' for tok in args.query.split())

    rows = conn.execute(
        """
        SELECT b.md5, b.title, b.author, b.extension, b.filesize, b.language, b.year
        FROM books_fts
        JOIN books b ON b.rowid = books_fts.rowid
        WHERE books_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, args.limit),
    ).fetchall()

    if not rows:
        print("No matches.")
        return

    for r in rows:
        size = f"{r['filesize']/1_000_000:.1f}MB" if r["filesize"] else "?"
        print(f"{r['md5']}  {r['extension'] or '?':6} {size:>9}  {r['language'] or '?':10} {r['year'] or '?':6} | {r['title']} — {r['author']}")


if __name__ == "__main__":
    main()
