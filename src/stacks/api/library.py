import json
import logging
import os
import stat
import zipfile
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Response, current_app, jsonify, request, stream_with_context

from . import api_bp
from stacks.constants import DOWNLOAD_PATH, PROJECT_ROOT
from stacks.security.auth import require_auth_with_permissions, validate_api_key

logger = logging.getLogger("api")

# Selections posted from the browser can list thousands of files, which is far
# more than Werkzeug's default 500 KB form-field limit allows.
MAX_SELECTION_BYTES = 64 * 1024 * 1024

ZIP_CHUNK_SIZE = 1024 * 1024


def _incomplete_dir(config) -> Path:
    incomplete_folder_path = config.get('downloads', 'incomplete_folder_path', default='/download/incomplete')
    return (PROJECT_ROOT / incomplete_folder_path.lstrip('/')).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def list_library_files(root: Path, incomplete: Path) -> list[dict]:
    """
    Walk the download folder and return every finished, regular file.

    Symlinks, dotfiles/dot-folders and the incomplete folder are skipped, so
    nothing outside the library (or half-downloaded) can ever be listed.
    """
    root = root.resolve()
    files = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith('.')
            and not os.path.islink(current / d)
            and (current / d).resolve() != incomplete
        )

        for name in sorted(filenames):
            if name.startswith('.'):
                continue
            full = current / name
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue

            rel = full.relative_to(root).as_posix()
            files.append({
                'path': rel,
                'name': name,
                'folder': Path(rel).parent.as_posix() if '/' in rel else '',
                'size': st.st_size,
                'modified': int(st.st_mtime),
            })

    return files


def resolve_library_file(rel_path, root: Path, incomplete: Path):
    """
    Map a client-supplied relative path onto a file inside the library.

    Returns the absolute Path, or None if the path is not a plain finished file
    that list_library_files() would also have returned.
    """
    if not isinstance(rel_path, str) or not rel_path or '\x00' in rel_path:
        return None

    parts = rel_path.replace('\\', '/').split('/')
    if rel_path.startswith('/') or any(p in ('', '.', '..') or p.startswith('.') for p in parts):
        return None

    root = root.resolve()
    candidate = root.joinpath(*parts)

    # Reject symlinks anywhere along the path, then confirm containment.
    probe = root
    for part in parts:
        probe = probe / part
        if os.path.islink(probe):
            return None

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None

    if not _is_within(resolved, root) or _is_within(resolved, incomplete):
        return None
    if not stat.S_ISREG(os.lstat(resolved).st_mode):
        return None

    return resolved


class _ZipSink:
    """Write-only, non-seekable file object that collects zip output in memory
    so it can be handed to the HTTP response one chunk at a time."""

    def __init__(self):
        self._chunks = []
        self._offset = 0

    def write(self, data):
        self._chunks.append(bytes(data))
        self._offset += len(data)
        return len(data)

    def tell(self):
        return self._offset

    def flush(self):
        pass

    def drain(self) -> bytes:
        data = b''.join(self._chunks)
        self._chunks.clear()
        return data


def stream_zip(entries):
    """
    Yield a zip archive for `entries` (list of (arcname, Path)) as bytes chunks.

    Files are stored, not compressed: ebooks are already compressed formats and
    this keeps CPU use negligible and memory flat regardless of library size.
    """
    sink = _ZipSink()
    with zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for arcname, path in entries:
            try:
                st = os.stat(path)
                info = zipfile.ZipInfo.from_file(path, arcname, strict_timestamps=False)
                info.compress_type = zipfile.ZIP_STORED
                info.file_size = st.st_size
                with open(path, 'rb') as src, zf.open(info, 'w') as dst:
                    while True:
                        chunk = src.read(ZIP_CHUNK_SIZE)
                        if not chunk:
                            break
                        dst.write(chunk)
                        out = sink.drain()
                        if out:
                            yield out
            except OSError as e:
                # The file vanished or became unreadable after it was selected.
                logger.warning(f"Library archive: skipping {arcname}: {e}")
                continue
            out = sink.drain()
            if out:
                yield out
    out = sink.drain()
    if out:
        yield out


def _is_cross_origin() -> bool:
    """True if a browser is making this request from a different origin."""
    site = request.headers.get('Sec-Fetch-Site')
    if site:
        return site not in ('same-origin', 'none')
    origin = request.headers.get('Origin')
    if not origin:
        return False
    allowed = {request.host, request.headers.get('X-Forwarded-Host')}
    return urlparse(origin).netloc not in allowed


def same_origin_or_admin_key(f):
    """
    Refuse cross-origin browser requests unless they carry a valid admin API key.

    The app reflects any Origin in its CORS headers with credentials allowed, so a
    third-party web page could otherwise drive these endpoints using the user's
    logged-in session cookie. Same-origin UI use and plain API-key clients
    (curl, scripts) are unaffected.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _is_cross_origin():
            provided = request.headers.get('X-API-Key') or request.args.get('api_key')
            is_valid, key_type = validate_api_key(provided)
            if not (is_valid and key_type == 'admin'):
                return jsonify({'success': False, 'error': 'Cross-origin requests require an admin API key'}), 403
        return f(*args, **kwargs)
    return wrapper


@api_bp.route('/api/library/files', methods=['GET'])
@require_auth_with_permissions(allow_downloader=False)
@same_origin_or_admin_key
def api_library_files():
    """List finished files in the download folder."""
    config = current_app.stacks_config
    files = list_library_files(DOWNLOAD_PATH, _incomplete_dir(config))
    return jsonify({
        'success': True,
        'files': files,
        'count': len(files),
        'total_size': sum(f['size'] for f in files),
    })


@api_bp.route('/api/library/archive', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
@same_origin_or_admin_key
def api_library_archive():
    """
    Stream selected (or all) library files as a single zip archive.

    Accepts a form or JSON body with either `all` = true or `selection`, a JSON
    array of paths relative to the download folder (as returned by
    /api/library/files).
    """
    request.max_form_memory_size = MAX_SELECTION_BYTES

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        select_all = bool(payload.get('all'))
        selection = payload.get('selection')
    else:
        select_all = request.form.get('all') in ('1', 'true', 'on')
        raw = request.form.get('selection')
        try:
            selection = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return jsonify({'success': False, 'error': 'Invalid selection'}), 400

    config = current_app.stacks_config
    root = DOWNLOAD_PATH.resolve()
    incomplete = _incomplete_dir(config)

    if select_all:
        entries = [
            (f['path'], root / f['path'])
            for f in list_library_files(root, incomplete)
        ]
    else:
        if not isinstance(selection, list) or not selection:
            return jsonify({'success': False, 'error': 'No files selected'}), 400
        entries = []
        seen = set()
        for rel in selection:
            resolved = resolve_library_file(rel, root, incomplete)
            if resolved is None:
                return jsonify({'success': False, 'error': f'Invalid file: {rel}'}), 400
            arcname = resolved.relative_to(root).as_posix()
            if arcname not in seen:
                seen.add(arcname)
                entries.append((arcname, resolved))

    if not entries:
        return jsonify({'success': False, 'error': 'No files to archive'}), 404

    filename = f"stacks-library-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    logger.info(f"Library archive: streaming {len(entries)} file(s) as {filename}")

    return Response(
        stream_with_context(stream_zip(entries)),
        mimetype='application/zip',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        },
    )


@api_bp.route('/api/library/delete', methods=['POST'])
@require_auth_with_permissions(allow_downloader=False)
@same_origin_or_admin_key
def api_library_delete():
    """
    Permanently delete the selected finished files from the download folder.

    Body: {"selection": [paths as returned by /api/library/files]}. Every path is
    validated before anything is removed, so one invalid path rejects the whole
    request. Directories are never removed, only the files themselves.
    """
    payload = request.get_json(silent=True) or {}
    selection = payload.get('selection')
    if not isinstance(selection, list) or not selection:
        return jsonify({'success': False, 'error': 'No files selected'}), 400

    root = DOWNLOAD_PATH.resolve()
    incomplete = _incomplete_dir(current_app.stacks_config)

    targets = {}
    for rel in selection:
        resolved = resolve_library_file(rel, root, incomplete)
        if resolved is None:
            return jsonify({'success': False, 'error': f'Invalid file: {rel}'}), 400
        targets[resolved.relative_to(root).as_posix()] = resolved

    deleted, failed, freed = [], [], 0
    for rel, path in targets.items():
        try:
            size = os.lstat(path).st_size
            os.unlink(path)
            deleted.append(rel)
            freed += size
        except OSError as e:
            logger.warning(f"Library delete: could not remove {rel}: {e}")
            failed.append({'path': rel, 'error': e.strerror or str(e)})

    logger.info(f"Library delete: removed {len(deleted)} file(s) ({freed} bytes), {len(failed)} failed")
    return jsonify({
        'success': not failed,
        'deleted': deleted,
        'freed_bytes': freed,
        'failed': failed,
    })
