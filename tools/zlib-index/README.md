# Local Search Index & Semantic Search

Stacks itself has no search UI of its own - you queue downloads by MD5 or
URL. The tools in this folder build an optional, fully offline search layer
on top of Stacks, derived from Anna's Archive's own published metadata,
in three independent tiers. Each tier is additive: skip straight to the one
you want, or stop at any tier and use Stacks exactly as-is.

| Tier | Gives you | Needs |
|---|---|---|
| 1. Nothing | Stacks as published - add by MD5/URL, Tampermonkey script | Nothing extra |
| 2. Local search index | A **Search** tab: title/author/format/language, instant offline results | One metadata torrent |
| 3. + Description embeddings | **Find Similar** and free-text **Search by Description** | Tier 2 + Ollama |

Nothing here is required to use Stacks. If a tier's config is disabled or
its index file is missing, the corresponding UI simply doesn't appear -
there's no broken/degraded state to worry about.

---

## Tier 1 - Stacks without any index

This is just the base setup from the [main README](../README.md#quick-start).
Bring the stack up, log in, queue downloads by MD5/URL or via the
Tampermonkey script. The Search tab will show "Local index not built yet"
and stay that way until you complete Tier 2 - everything else works
normally.

---

## Tier 2 - Local search index (FTS5)

### Step 1: Get the metadata torrent

Anna's Archive publishes periodic metadata dumps in their "AAC" (Anna's
Archive Containers) format as torrents, on their official Torrents page.
Look for a **`zlib3_records`** metadata torrent - a single file named
something like:

```
annas_archive_meta__aacid__zlib3_records__<start>--<end>.jsonl.seekable.zst
```

It's a Zstandard-compressed, newline-delimited JSON file (one Z-Library
book's metadata per line), roughly **20-25GB**. Download it with a torrent
client to wherever you have space - it doesn't need to live inside this
repo.

Budget disk space for the whole pipeline before starting:

| File | Approx. size |
|---|---|
| Source torrent (`.jsonl.seekable.zst`) | ~24GB |
| `zlib_index.sqlite3` (Tier 2 output) | ~20GB |
| `zlib_descriptions_full.sqlite3` (Tier 3) | ~20GB |
| `zlib_embeddings_full_by_lang.sqlite3` (Tier 3) | ~8GB |
| **Total, all tiers** | **~72GB** |

### Step 2: Set up the Python environment

```bash
cd tools/zlib-index
python3 -m venv venv
source venv/bin/activate
pip install sqlite-vec
```

`zstd` must also be available on the host (`which zstd`) - install via your
package manager if not (`dnf install zstd`, `apt install zstd`, `brew
install zstd`).

### Step 3: Build the index

```bash
python3 build_index.py /path/to/annas_archive_meta__aacid__zlib3_records__*.jsonl.seekable.zst \
    --db zlib_index.sqlite3
```

This streams the file frame-by-frame (tolerant of a torrent with a few
missing/corrupt pieces - it just skips unreadable frames rather than
aborting) and builds an FTS5 full-text index over title/author/publisher.
Expect on the order of **30-60 minutes**, depending on disk speed - it
prints progress every 500K records. On a full official dump this lands
around **45 million rows**.

**Low disk space:** pass `--min-free-gb 10` (default `5`) to raise the
safety threshold at which the script stops cleanly instead of filling
the disk.

### Step 4: Validate

```bash
python3 search.py "Dune Frank Herbert" --db zlib_index.sqlite3
```

You should see multiple real editions of Dune. Sanity-check the total
count too (no `sqlite3` CLI needed - not every system has it installed):

```bash
python3 -c "import sqlite3; print(sqlite3.connect('zlib_index.sqlite3').execute('SELECT COUNT(*) FROM books').fetchone()[0])"
```

Tens of millions is expected for a full official dump.

### Step 5: Mount it and enable Search

Add a bind mount in `docker-compose.override.yml` (create the file if you
don't have one yet):

```yaml
services:
  stacks:
    volumes:
      - "./tools/zlib-index/zlib_index.sqlite3:/opt/stacks/zlib_index.sqlite3:ro"
```

Then rebuild and restart:

```bash
docker compose up -d --build stacks
```

Open the **Search** tab - the status line should read "Index ready -
N books (X GB)". No further config is needed; Tier 2 has no `enabled`
toggle since it's the base search feature itself, not an optional add-on.

---

## Tier 3 - Find Similar + Search by Description

Builds on Tier 2 - complete that first (you need `zlib_index.sqlite3`).

This tier adds:
- **Find Similar**: a button next to each search result that finds
  books with a similar description.
- **Search by Description**: type what you're looking for in plain
  language ("winter and dragons") and get results ranked by meaning
  rather than exact keywords.

Both are genuinely useful but have a real limitation worth knowing before
you invest hours of compute: the embedding model is much better at
**book-to-book** similarity (Find Similar) than **free-text concept**
queries - vague phrasing can miss books that are actually relevant. Try
the small pilot in Step 3 before committing to the full run.

### Step 1: Extract descriptions

Only a fraction of the catalog (roughly 20-25%) has a description at all
in the source metadata - this step pulls out just those, deduplicated by
book (the same book can appear multiple times in the source across
repeated scrapes).

```bash
python3 extract_descriptions.py /path/to/the/same/seekable_file.jsonl.seekable.zst \
    --db zlib_descriptions_full.sqlite3
```

This re-scans the same source file from Step 1 of Tier 2 (keep it around
until you're done with Tier 3). A full pass takes **45-90 minutes**
single-threaded. To go faster, run it sharded across several CPU cores in
parallel - first precompute the frame index once (a few minutes, avoids
every shard repeating the same full-file scan), then launch N workers:

```bash
python3 extract_descriptions.py /path/to/file.jsonl.seekable.zst --dump-offsets frame_offsets.txt

for i in 0 1 2 3; do
  python3 extract_descriptions.py /path/to/file.jsonl.seekable.zst \
      --offsets-file frame_offsets.txt --shard-index $i --shard-count 4 \
      --db zlib_descriptions_shard$i.sqlite3 &
done
wait
```

Then merge the shards into one file (dedup happens automatically via
`INSERT OR IGNORE`):

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('zlib_descriptions_full.sqlite3')
conn.execute('CREATE TABLE IF NOT EXISTS descriptions (md5 TEXT PRIMARY KEY, description TEXT NOT NULL)')
for i in range(4):
    conn.execute(f\"ATTACH DATABASE 'zlib_descriptions_shard{i}.sqlite3' AS s{i}\")
    conn.execute(f'INSERT OR IGNORE INTO descriptions SELECT * FROM s{i}.descriptions')
    conn.execute(f'DETACH DATABASE s{i}')
conn.commit()
"
rm zlib_descriptions_shard*.sqlite3
```

On 4 shards this typically finishes in under 10 minutes.

### Step 2: Bring up Ollama and pull the embedding model

The stack includes a containerized Ollama service for exactly this. It
needs no separate install:

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull embeddinggemma
```

By default this container is **CPU-only**, which is genuinely usable
(measured ~37 embeddings/sec on a 16-core host - a full ~8.8M-book run
takes roughly 2-3 days). If you have an NVIDIA GPU, enabling passthrough
gets you the same throughput as bare metal (~280/s, ~9 hours for the
full corpus) - see [GPU acceleration](#gpu-acceleration-for-ollama-optional)
below. Either way, the script talks to Ollama the same way; only the
speed differs.

Confirm the port is reachable from the host (used by the scripts in this
folder, which run outside Docker):

```bash
curl http://localhost:11435/api/tags
```

### Step 3: Pilot run - validate before committing hours of compute

Embed a small sample first, including a book you know well as an anchor
so you can sanity-check results:

```bash
python3 build_embeddings_desc.py \
    --source zlib_index.sqlite3 \
    --descriptions zlib_descriptions_full.sqlite3 \
    --out zlib_embeddings_pilot.sqlite3 \
    --ollama-host http://localhost:11435 \
    --model embeddinggemma --dims 768 --quantize int8 --concurrency 4 \
    --limit 20000 --anchor-md5s <md5-of-a-book-you-know>
```

Then check the neighbors make sense:

```bash
python3 -c "
import sqlite3, sqlite_vec
db = sqlite3.connect('zlib_embeddings_pilot.sqlite3')
db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)
idx = sqlite3.connect('file:zlib_index.sqlite3?mode=ro', uri=True)

md5 = '<md5-of-a-book-you-know>'
vrow = db.execute('SELECT rowid FROM vector_md5_map WHERE md5 = ?', (md5,)).fetchone()
rows = db.execute('''SELECT rowid, distance FROM book_vectors
    WHERE embedding MATCH (SELECT embedding FROM book_vectors WHERE rowid = ?) AND k = 8
    ORDER BY distance''', (vrow[0],)).fetchall()
for r_rowid, dist in rows:
    m = db.execute('SELECT md5 FROM vector_md5_map WHERE rowid = ?', (r_rowid,)).fetchone()
    book = idx.execute('SELECT title, author FROM books WHERE md5 = ? LIMIT 1', (m[0],)).fetchone()
    print(f'{dist:.1f}  {book}')
"
```

The nearest neighbor (distance ~0) should be the book itself or an
identical edition; the next few should be genuinely related books
(sequels, same author/series, or close themes). If that looks right,
delete the pilot file and move on:

```bash
rm zlib_embeddings_pilot.sqlite3
```

### Step 4: Full run

```bash
python3 build_embeddings_desc.py \
    --source zlib_index.sqlite3 \
    --descriptions zlib_descriptions_full.sqlite3 \
    --out zlib_embeddings_full.sqlite3 \
    --ollama-host http://localhost:11435 \
    --model embeddinggemma --dims 768 --quantize int8 --concurrency 4
```

No `--limit` embeds everything that has a description (typically ~8-9
million books out of the full catalog). This is **safe to interrupt and
resume** - re-running the exact same command skips whatever's already in
`--out` and picks up where it left off, rather than starting over.

### Step 5 (recommended): Repartition by language

Speeds up language-filtered Find Similar/Search-by-Description queries
by 10-30x (a plain unfiltered query still does a full brute-force scan
regardless - `sqlite-vec` has no approximate-nearest-neighbor indexing,
just this kind of partition-key pre-filtering). Takes a few minutes - it
only copies already-computed vectors into a repartitioned schema, no
re-embedding involved:

```bash
python3 repartition_by_language.py \
    --source zlib_embeddings_full.sqlite3 \
    --index zlib_index.sqlite3 \
    --out zlib_embeddings_full_by_lang.sqlite3
```

### Step 6: Validate

Same KNN sanity check as Step 3, against `zlib_embeddings_full_by_lang.sqlite3`
this time, plus a free-text query to check semantic search quality:

```bash
python3 -c "
import sqlite3, sqlite_vec, json, urllib.request

def embed(text):
    req = urllib.request.Request('http://localhost:11435/api/embed',
        data=json.dumps({'model': 'embeddinggemma', 'input': [f'task: search result | query: {text}']}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())['embeddings'][0]

db = sqlite3.connect('zlib_embeddings_full_by_lang.sqlite3')
db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)
idx = sqlite3.connect('file:zlib_index.sqlite3?mode=ro', uri=True)

vec = sqlite_vec.serialize_float32(embed('winter and dragons'))
rows = db.execute('''SELECT rowid, distance FROM book_vectors
    WHERE embedding MATCH vec_quantize_int8(?, \"unit\") AND k = 8 ORDER BY distance''', (vec,)).fetchall()
for r_rowid, dist in rows:
    m = db.execute('SELECT md5 FROM vector_md5_map WHERE rowid = ?', (r_rowid,)).fetchone()
    book = idx.execute('SELECT title, author FROM books WHERE md5 = ? LIMIT 1', (m[0],)).fetchone()
    print(f'{dist:.1f}  {book}')
"
```

Expect genuinely dragon/winter-themed titles, not just books containing
those words literally.

### Step 7: Mount and enable

Add both new files to `docker-compose.override.yml`:

```yaml
services:
  stacks:
    volumes:
      - "./tools/zlib-index/zlib_index.sqlite3:/opt/stacks/zlib_index.sqlite3:ro"
      - "./tools/zlib-index/zlib_embeddings_full_by_lang.sqlite3:/opt/stacks/zlib_embeddings_full_by_lang.sqlite3:ro"
      - "./tools/zlib-index/zlib_descriptions_full.sqlite3:/opt/stacks/zlib_descriptions_full.sqlite3:ro"
```

```bash
docker compose up -d --build stacks
```

Then in the Stacks UI, **Settings**:
- **Similar Search**: enable, set index path to `/opt/stacks/zlib_embeddings_full_by_lang.sqlite3`
- **Descriptions**: enable, set index path to `/opt/stacks/zlib_descriptions_full.sqlite3`

Save, then go to the **Search** tab - "Find Similar" buttons and the
"Search by Description" field should now appear. Both config sections
also accept `ollama_url` (default `http://ollama:11434`, the container's
internal address - only relevant if you renamed the service or moved
Ollama elsewhere) and `model` (default `embeddinggemma`, must match
whatever you actually embedded with).

---

## GPU acceleration for Ollama (optional)

Linux only (NVIDIA). Not available in Docker Desktop on macOS regardless
of chip (no GPU passthrough to containers on Mac, ever) - only a native,
non-Docker Ollama install gets GPU/Metal acceleration there. Windows can
do this via Docker Desktop's WSL2 backend with NVIDIA's WSL-aware drivers,
same NVIDIA Container Toolkit approach as below.

1. Install the toolkit:
   ```bash
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
   sudo dnf install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
   (Debian/Ubuntu: swap the `dnf`/`yum` repo+install lines for the `apt` equivalent from [NVIDIA's install docs](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) - the `nvidia-ctk`/`systemctl` steps are the same.)

2. Verify passthrough works at all:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi
   ```

3. Add a GPU reservation for the `ollama` service in
   `docker-compose.override.yml` (deliberately kept out of the main
   `docker-compose.yml` so the project stays usable out of the box on
   machines without a GPU):
   ```yaml
   services:
     ollama:
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: 1
                 capabilities: [gpu]
   ```

4. Recreate the container: `docker compose up -d ollama`

Verify with `docker exec ollama nvidia-smi` - the GPU should show up
inside the container. First embedding request after a restart is slow
(model load) - judge throughput on a run of at least a few thousand
items, not the first few seconds.

---

## Troubleshooting

- **"Local index not built yet"** after mounting: check the container
  can actually read the file - `docker exec stacks ls -l /opt/stacks/zlib_index.sqlite3`.
  On SELinux systems (Fedora, RHEL) add `,z` to the mount (`:ro,z`).
- **Find Similar returns nothing for a book you can see in Search**: only
  a fraction of editions have a description, so not every book/md5 is in
  the embeddings index - the API automatically falls back to another
  edition of the same book if one is indexed, and says so in the
  response. If truly no edition of that book has a description, there's
  nothing to fall back to.
- **Semantic search results look like keyword matching, not meaning**:
  confirm your query actually evokes a clear genre/theme - this is a
  known model limitation for vague phrasing, not a bug (see the note at
  the top of Tier 3).
- **`sqlite3.OperationalError: no such module: fts5`**: your `sqlite3`
  CLI wasn't built with FTS5 support. Use a container image that has it
  (e.g. `nouchka/sqlite3`) rather than the bare `sqlite3` package on some
  distros.
