import json
import logging
import os
import sqlite3
import urllib.request

import sqlite_vec
from flask import jsonify, request, current_app

from . import api_bp
from stacks.security.auth import require_auth_with_permissions

logger = logging.getLogger("api")

INDEX_PATH = "/opt/stacks/zlib_index.sqlite3"


def get_index_connection():
    """Open the local Z-Library metadata index read-only."""
    if not os.path.exists(INDEX_PATH):
        return None
    conn = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_similar_search_config():
    """
    Read the similar_search config section. Returns None if the feature is
    disabled or its index file isn't present, so callers can fall back to
    the normal search flow - "find similar" is an optional, pluggable
    addition on top of the existing FTS5 search, never a dependency of it.
    """
    config = current_app.stacks_config
    if not config.get('similar_search', 'enabled', default=False):
        return None
    index_path = config.get('similar_search', 'index_path', default=None)
    if not index_path or not os.path.exists(index_path):
        return None
    return {
        'index_path': index_path,
        'ollama_url': config.get('similar_search', 'ollama_url', default='http://ollama:11434'),
        'model': config.get('similar_search', 'model', default='embeddinggemma'),
    }


def embed_query_text(ollama_url, model, text):
    """
    Embed free-text search input using EmbeddingGemma's asymmetric-retrieval
    query prefix, matching how the index's documents were embedded (see
    build_embeddings_desc.py) so both land in the same space.
    """
    req = urllib.request.Request(
        f"{ollama_url}/api/embed",
        data=json.dumps({"model": model, "input": [f"task: search result | query: {text}"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    return body["embeddings"][0]


def get_similar_connection(index_path):
    """Open the similarity vector index read-only, with sqlite-vec loaded."""
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def get_description_index_config():
    """
    Read the description_index config section. Returns None if the feature
    is disabled or its index file isn't present - descriptions are a purely
    optional enrichment on search/similar results, never a dependency of
    them, so callers just omit the field when this returns None.
    """
    config = current_app.stacks_config
    if not config.get('description_index', 'enabled', default=False):
        return None
    index_path = config.get('description_index', 'index_path', default=None)
    if not index_path or not os.path.exists(index_path):
        return None
    return {'index_path': index_path}


def attach_descriptions(results, description_config):
    """
    Batch-fetch and merge in a `description` field for each result dict in
    place (only for md5s that actually have one - many won't, since only a
    fraction of the catalog has description metadata at all).
    """
    if not description_config or not results:
        return
    md5s = [r['md5'] for r in results if r.get('md5')]
    if not md5s:
        return
    try:
        conn = sqlite3.connect(f"file:{description_config['index_path']}?mode=ro", uri=True)
        placeholders = ",".join("?" for _ in md5s)
        rows = conn.execute(
            f"SELECT md5, description FROM descriptions WHERE md5 IN ({placeholders})",
            md5s,
        ).fetchall()
        conn.close()
        by_md5 = {row[0]: row[1] for row in rows}
        for r in results:
            desc = by_md5.get(r.get('md5'))
            if desc:
                r['description'] = desc
    except sqlite3.OperationalError as e:
        logger.error(f"Failed to fetch descriptions: {e}")


@api_bp.route('/api/local_index/status', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
def api_local_index_status():
    """Report whether the local search index is available, and its size."""
    if not os.path.exists(INDEX_PATH):
        return jsonify({'available': False})

    try:
        conn = get_index_connection()
        count = conn.execute("SELECT COUNT(*) AS c FROM books").fetchone()['c']
        conn.close()
        return jsonify({
            'available': True,
            'record_count': count,
            'file_size': os.path.getsize(INDEX_PATH),
            'similar_search_available': get_similar_search_config() is not None,
            'description_index_available': get_description_index_config() is not None,
        })
    except Exception as e:
        logger.error(f"Failed to read local index status: {e}")
        return jsonify({'available': False, 'error': str(e)})


@api_bp.route('/api/local_index/search', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
def api_local_index_search():
    """
    Search the local Z-Library metadata index.

    Query params:
        title, author: free-text terms, matched via FTS5 against title/author/publisher
        language, extension: exact (case-insensitive) filters
        limit: max results (default 50, capped at 1000)
    """
    conn = get_index_connection()
    if conn is None:
        return jsonify({
            'success': False,
            'error': 'Local index not built yet. See tools/zlib-index/build_index.py.',
        }), 404

    title = (request.args.get('title') or '').strip()
    author = (request.args.get('author') or '').strip()
    language = (request.args.get('language') or '').strip()
    extension = (request.args.get('extension') or '').strip()
    unique_titles = (request.args.get('unique_titles') or '').lower() in ('1', 'true', 'yes')
    try:
        limit = min(int(request.args.get('limit', 50)), 1000)
    except ValueError:
        limit = 50

    fts_terms = []
    for term in (title + " " + author).split():
        # Escape double quotes for FTS5 phrase syntax
        fts_terms.append('"' + term.replace('"', '""') + '"')
    fts_query = " ".join(fts_terms)

    where_clauses = []
    params = []

    if language:
        where_clauses.append("LOWER(b.language) = LOWER(?)")
        params.append(language)
    if extension:
        where_clauses.append("LOWER(b.extension) = LOWER(?)")
        params.append(extension.lstrip('.'))

    where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""

    # Default dedup key is md5 (collapses re-affirmed copies of the exact
    # same file - see comment below). When unique_titles is requested, group
    # by title instead, additionally collapsing different editions/formats
    # of the same titled book down to one representative row.
    group_col = "title" if unique_titles else "md5"

    # When collapsing to one row per title, prefer epub > fb2 > pdf > other
    # as the representative edition. Folded into the same MIN()-aggregate
    # trick as the rank/rowid tiebreak below, weighted well above it so
    # extension preference always wins ties.
    ext_priority = "CASE LOWER(b.extension) WHEN 'epub' THEN 0 WHEN 'fb2' THEN 1 WHEN 'pdf' THEN 2 ELSE 3 END"

    try:
        if fts_query:
            # The source dataset is an append-only log of scrape events, so
            # the same book (same md5) can appear multiple times under
            # different aacids (re-affirmed on a later scrape pass). Group
            # by md5 (or title) and keep the best-ranked match per group -
            # SQLite's MIN()/MAX() aggregate extension pulls the rest of
            # that row's columns from whichever source row had the min rank.
            sql = f"""
                SELECT md5, title, author, extension, filesize,
                       language, year, publisher, MIN(sort_key) AS best_rank
                FROM (
                    SELECT b.md5, b.title, b.author, b.extension, b.filesize,
                           b.language, b.year, b.publisher,
                           ({ext_priority}) * 1000000.0 + books_fts.rank AS sort_key
                    FROM books_fts
                    JOIN books b ON b.rowid = books_fts.rowid
                    WHERE books_fts MATCH ? {where_sql}
                )
                GROUP BY {group_col}
                ORDER BY best_rank
                LIMIT ?
            """
            rows = conn.execute(sql, [fts_query] + params + [limit]).fetchall()
        else:
            # No text query - just filter (e.g. browse by language/format).
            # Same duplicate issue applies here; dedupe the same way.
            sql = f"""
                SELECT md5, title, author, extension, filesize,
                       language, year, publisher, MIN(sort_key) AS _rowid
                FROM (
                    SELECT b.*, ({ext_priority}) * 1000000000 + b.rowid AS sort_key
                    FROM books b
                    WHERE 1=1 {where_sql}
                )
                GROUP BY {group_col}
                LIMIT ?
            """
            rows = conn.execute(sql, params + [limit]).fetchall()

        keep = ('md5', 'title', 'author', 'extension', 'filesize', 'language', 'year', 'publisher')
        results = [{k: row[k] for k in keep} for row in rows]
        attach_descriptions(results, get_description_index_config())
        return jsonify({'success': True, 'results': results, 'count': len(results)})
    except sqlite3.OperationalError as e:
        logger.error(f"Local index search failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    finally:
        conn.close()


@api_bp.route('/api/local_index/lookup', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
def api_local_index_lookup():
    """
    Batch-resolve md5s to display metadata (title/author), for enriching
    queue/history rows that only carry a filename or md5. Duplicate md5
    rows are collapsed the same way search does (best/first match wins).

    POST body: {"md5s": [...]}. POST rather than a query string because a
    large queue can carry hundreds of md5s at once, well past what fits in
    a URL.
    """
    conn = get_index_connection()
    if conn is None:
        return jsonify({'success': False, 'error': 'Local index not built yet.'}), 404

    data = request.get_json(silent=True) or {}
    md5s = [str(m).strip().lower() for m in (data.get('md5s') or []) if str(m).strip()]
    if not md5s:
        return jsonify({'success': True, 'results': {}})

    try:
        placeholders = ",".join("?" for _ in md5s)
        rows = conn.execute(
            f"""
            SELECT md5, title, author FROM books
            WHERE LOWER(md5) IN ({placeholders})
            GROUP BY md5
            """,
            md5s,
        ).fetchall()
        results = {row['md5'].lower(): {'title': row['title'], 'author': row['author']} for row in rows}
        return jsonify({'success': True, 'results': results})
    except sqlite3.OperationalError as e:
        logger.error(f"Local index lookup failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    finally:
        conn.close()


def _find_indexed_vector_rowid(vec_conn, idx_conn, md5):
    """
    Look up md5's vector rowid. If it isn't in the similarity index (common -
    only a fraction of md5s/editions have a description to embed), fall back
    to another edition of the same book (same title+author) that is, so
    "Find Similar" still works from whichever copy the user happened to
    click. Returns (rowid, actual_md5_used) or (None, md5) if no edition of
    this book is indexed at all.
    """
    vrow = vec_conn.execute("SELECT rowid FROM vector_md5_map WHERE md5 = ?", (md5,)).fetchone()
    if vrow:
        return vrow[0], md5

    book = idx_conn.execute("SELECT title, author FROM books WHERE md5 = ? LIMIT 1", (md5,)).fetchone()
    if not book or not book['title']:
        return None, md5

    siblings = idx_conn.execute(
        "SELECT DISTINCT md5 FROM books WHERE title = ? AND author = ?",
        (book['title'], book['author']),
    ).fetchall()
    for sib in siblings:
        sib_md5 = sib['md5'].lower()
        if sib_md5 == md5:
            continue
        vrow = vec_conn.execute("SELECT rowid FROM vector_md5_map WHERE md5 = ?", (sib_md5,)).fetchone()
        if vrow:
            return vrow[0], sib_md5

    return None, md5


@api_bp.route('/api/local_index/similar', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
def api_local_index_similar():
    """
    Find books similar to a given book, using a pre-built embeddings index
    (see tools/zlib-index/build_embeddings_desc.py). Purely additive on top
    of the regular FTS5 search - controlled entirely by the similar_search
    config section, which points at whichever index file is currently
    plugged in (title+author-only, description-based, or a future one).

    Query params:
        md5: the book to find neighbors for (required)
        limit: max results (default 10, capped at 50)
        language: optional - restrict results to this language, e.g. when
            the caller's original search had a language filter set
    """
    similar_config = get_similar_search_config()
    if similar_config is None:
        return jsonify({'success': False, 'error': 'Similar-search is not enabled or configured.'}), 404

    md5 = (request.args.get('md5') or '').strip().lower()
    if not md5:
        return jsonify({'success': False, 'error': 'md5 is required'}), 400
    language = (request.args.get('language') or '').strip()
    try:
        limit = min(int(request.args.get('limit', 10)), 50)
    except ValueError:
        limit = 10

    idx_conn = get_index_connection()
    if idx_conn is None:
        return jsonify({'success': False, 'error': 'Local search index not built yet.'}), 404

    try:
        vec_conn = get_similar_connection(similar_config['index_path'])
    except Exception as e:
        idx_conn.close()
        logger.error(f"Failed to open similar-search index: {e}")
        return jsonify({'success': False, 'error': 'Could not open similarity index.'}), 500

    try:
        anchor_rowid, used_md5 = _find_indexed_vector_rowid(vec_conn, idx_conn, md5)
        if anchor_rowid is None:
            return jsonify({'success': True, 'results': [], 'note': 'This book is not in the similarity index.'})

        # If the index has `language` as a vec0 partition key (see
        # repartition_by_language.py), filtering on it directly in the query
        # only scans that language's subset - orders of magnitude faster
        # than brute-force over the whole corpus. Older/unpartitioned index
        # files (no `language` column) fall back to over-fetching and
        # filtering afterward, so any index file plugged into index_path
        # still works, just faster if it happens to be partitioned.
        has_language_partition = any(
            col[1] == "language" for col in vec_conn.execute("PRAGMA table_info(book_vectors)").fetchall()
        )

        if language and has_language_partition:
            rows = vec_conn.execute("""
                SELECT rowid, distance FROM book_vectors
                WHERE embedding MATCH (SELECT embedding FROM book_vectors WHERE rowid = ?)
                AND language = ?
                AND k = ?
                ORDER BY distance
            """, (anchor_rowid, language.lower(), limit + 1)).fetchall()
        else:
            # Over-fetch when filtering by language without partition
            # support, since some neighbors will get dropped after the
            # fact - vec0's KNN doesn't support combining an arbitrary
            # WHERE predicate with the MATCH/k clause otherwise.
            fetch_k = min(limit * 5, 200) if language else limit + 1
            rows = vec_conn.execute("""
                SELECT rowid, distance FROM book_vectors
                WHERE embedding MATCH (SELECT embedding FROM book_vectors WHERE rowid = ?)
                AND k = ?
                ORDER BY distance
            """, (anchor_rowid, fetch_k)).fetchall()

        results = []
        for r_rowid, distance in rows:
            m = vec_conn.execute("SELECT md5 FROM vector_md5_map WHERE rowid = ?", (r_rowid,)).fetchone()
            if not m or m[0].lower() == used_md5:
                continue
            book = idx_conn.execute(
                "SELECT md5, title, author, extension, filesize, language, year, publisher "
                "FROM books WHERE md5 = ? LIMIT 1",
                (m[0],),
            ).fetchone()
            if not book:
                continue
            if language and (book['language'] or '').lower() != language.lower():
                continue
            results.append({**dict(book), 'distance': distance})
            if len(results) >= limit:
                break

        attach_descriptions(results, get_description_index_config())

        note = None
        if used_md5 != md5:
            note = 'This edition was not in the similarity index; showing results for another edition of the same book.'
        return jsonify({'success': True, 'results': results, 'count': len(results), 'note': note})
    except sqlite3.OperationalError as e:
        logger.error(f"Similar-search query failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    finally:
        idx_conn.close()
        vec_conn.close()


@api_bp.route('/api/local_index/semantic_search', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
def api_local_index_semantic_search():
    """
    Free-text semantic search: embed the query text live via Ollama and rank
    the catalog by similarity, instead of matching an existing book's vector.
    Uses the same similar_search config/index as "Find Similar" - it's the
    same underlying capability, just with a typed description as the query
    instead of a book you already found.

    Quality note: this works well for queries that evoke a clear genre/theme
    ("winter and dragons"), but is less reliable for vaguer phrasing - a
    known limitation of the embedding model, not a bug.

    Query params:
        query: free text describing what you're looking for (required)
        limit: max results (default 10, capped at 50)
        language: optional - restrict results to this language
        extension: optional - restrict results to this file format
        title: optional - title must contain this text (case-insensitive)
        author: optional - author must contain this text (case-insensitive)
    """
    similar_config = get_similar_search_config()
    if similar_config is None:
        return jsonify({'success': False, 'error': 'Similar-search is not enabled or configured.'}), 404

    query_text = (request.args.get('query') or '').strip()
    if not query_text:
        return jsonify({'success': False, 'error': 'query is required'}), 400
    language = (request.args.get('language') or '').strip()
    extension = (request.args.get('extension') or '').strip().lstrip('.')
    title_filter = (request.args.get('title') or '').strip().lower()
    author_filter = (request.args.get('author') or '').strip().lower()
    try:
        limit = min(int(request.args.get('limit', 10)), 50)
    except ValueError:
        limit = 10

    try:
        query_vec = embed_query_text(similar_config['ollama_url'], similar_config['model'], query_text)
    except Exception as e:
        logger.error(f"Failed to embed search query via Ollama ({similar_config['ollama_url']}): {e}")
        return jsonify({'success': False, 'error': 'Could not reach the embedding service.'}), 502

    idx_conn = get_index_connection()
    if idx_conn is None:
        return jsonify({'success': False, 'error': 'Local search index not built yet.'}), 404

    try:
        vec_conn = get_similar_connection(similar_config['index_path'])
    except Exception as e:
        idx_conn.close()
        logger.error(f"Failed to open similar-search index: {e}")
        return jsonify({'success': False, 'error': 'Could not open similarity index.'}), 500

    try:
        query_bytes = sqlite_vec.serialize_float32(query_vec)
        has_language_partition = any(
            col[1] == "language" for col in vec_conn.execute("PRAGMA table_info(book_vectors)").fetchall()
        )

        # extension/title/author aren't partition keys, so they're always
        # post-filtered (over-fetching to compensate); language uses the
        # partition key directly when available, same as Find Similar.
        other_filters = bool(extension or title_filter or author_filter)
        if language and has_language_partition:
            fetch_k = min(limit * 5, 200) if other_filters else limit
            rows = vec_conn.execute("""
                SELECT rowid, distance FROM book_vectors
                WHERE embedding MATCH vec_quantize_int8(?, 'unit')
                AND language = ?
                AND k = ?
                ORDER BY distance
            """, (query_bytes, language.lower(), fetch_k)).fetchall()
        else:
            fetch_k = min(limit * 5, 200) if (language or other_filters) else limit
            rows = vec_conn.execute("""
                SELECT rowid, distance FROM book_vectors
                WHERE embedding MATCH vec_quantize_int8(?, 'unit')
                AND k = ?
                ORDER BY distance
            """, (query_bytes, fetch_k)).fetchall()

        results = []
        for r_rowid, distance in rows:
            m = vec_conn.execute("SELECT md5 FROM vector_md5_map WHERE rowid = ?", (r_rowid,)).fetchone()
            if not m:
                continue
            book = idx_conn.execute(
                "SELECT md5, title, author, extension, filesize, language, year, publisher "
                "FROM books WHERE md5 = ? LIMIT 1",
                (m[0],),
            ).fetchone()
            if not book:
                continue
            if language and (book['language'] or '').lower() != language.lower():
                continue
            if extension and (book['extension'] or '').lower() != extension.lower():
                continue
            if title_filter and title_filter not in (book['title'] or '').lower():
                continue
            if author_filter and author_filter not in (book['author'] or '').lower():
                continue
            results.append({**dict(book), 'distance': distance})
            if len(results) >= limit:
                break

        attach_descriptions(results, get_description_index_config())
        return jsonify({'success': True, 'results': results, 'count': len(results)})
    except sqlite3.OperationalError as e:
        logger.error(f"Semantic search query failed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 400
    finally:
        idx_conn.close()
        vec_conn.close()
