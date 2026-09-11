import os
import sqlite3

INDEX_PATH = "/opt/stacks/zlib_index.sqlite3"


def lookup_by_md5(md5):
    """
    Look up title/author/extension for an md5 in the local search index
    (see stacks.api.local_index / tools/zlib-index/).

    Used as a fallback filename source when scraping Anna's Archive's page
    for title/filepath metadata fails (site redesigns, anti-bot challenges),
    since the index's metadata has been verified accurate against the
    actual downloaded files' real formats.

    Returns a dict with 'title', 'author', 'extension', or None if the
    index isn't mounted or the md5 isn't in it.
    """
    if not os.path.exists(INDEX_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT title, author, extension FROM books WHERE md5 = ? LIMIT 1",
            (md5,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
