# MiniMax Code session database notes

Reference for the internals `export_bundle.py` and `import_bundle.py` depend on.
Read this before changing either script, or before hand-writing SQL for a case
they do not cover.

## Where things live

| What | Path |
|---|---|
| Data directory | `~/.minimax` (do not assume this when multiple profiles exist — read it from the host config) |
| Session database | `<dataDir>/v2/sqlite/runtime-state.sqlite` |
| Runtime history | `<dataDir>/v2/sessions/<history_relative_dir>` |
| User attachments | `<dataDir>/v2/assets/<YYYY>/<MM>/<DD>/<HH-MM-SS>-asset_<assetId>-<original filename>` |

Read the schema version from `MAX(version)` in `local_runtime_v2_schema_migrations`
(observed: 41). The session column `columnar_version` was 3 when this was written.
`import_bundle.py` refuses anything below v40.

## The five tables an import actually needs

| Table | Purpose | Write notes |
|---|---|---|
| `local_runtime_projects` | Project | Dedupe on `workspace_dir`; `project_id` via `MAX + 1` |
| `local_runtime_sessions` | Session | List column names explicitly rather than `SELECT *` — the target install may have added columns |
| `local_runtime_message_rows` | Message bodies | One row per message; `data_json` holds `msg_id` / `role` / `msg_content` / `turnId`; `id` from `MAX + 1` |
| `local_runtime_sessions_fts` | Session list search (FTS5 virtual table) | Columns in order: `session_id` (UNINDEXED), `session_id_terms`, `agent_name_terms`, `title_terms`, `workspace_dir_terms`, `purpose_terms`, `status_terms`, `session_type_terms` |
| `local_runtime_session_fts_keys` | FTS row → session map | `fts_rowid` **must** come from the INSERT's `lastrowid` |

## Three things that bite

### 1. The search index is character level, and the app pre-tokenises

`local_runtime_sessions_fts` does not hold raw text. The `*_terms` columns hold:

```python
def to_terms(text):
    return " ".join(f"c{ord(ch):x}" for ch in (text or "").lower())
```

For example `"ab cd"` becomes `c61 c62 c20 c63 c64`.

FTS5's `unicode61` tokenizer then treats each space-separated chunk as one token,
so **searching is per-character**. To find `b` you query `MATCH 'c62'`;
`MATCH 'bc'` returns nothing because the word is not split. Writing raw text
here parses without complaint but searches differently from native sessions.
Note that a run of non-Latin characters with no space between them becomes a
single token, so a whole word cannot be found by matching any part of it.

### 2. FTS5 has its own rowid space

`fts_rowid` is an internal FTS5 identifier shared across the whole table,
unrelated to business keys. Deriving it as
`SELECT MAX(fts_rowid) + 1 FROM local_runtime_session_fts_keys` breaks as soon
as the destination already has sessions occupying that number: the index then
points at another session's row, and search silently returns nothing.

```python
cursor = conn.execute("INSERT INTO local_runtime_sessions_fts (...) VALUES (...)")
conn.execute(
    "INSERT INTO local_runtime_session_fts_keys (fts_rowid, session_id) VALUES (?, ?)",
    (cursor.lastrowid, session_id),
)
```

For the same reason, do not copy the source machine's `fts_rowid`, and do not
rely on "enumerate every table with a `session_id` column and copy the rows" —
`local_runtime_sessions_fts_content` has no `session_id` column, so it gets
skipped while the keys row carries a foreign rowid across with it.

### 3. Attachments live in the message, not in the assets table

`local_runtime_session_assets` has `msg_id`, `role`, `asset_index` and looks like
an attachment registry, but it only records files the **agent** produced.
User uploads appear in each message's `data_json` under `attachments`:

```json
{"attachments": [
  {"meta": {"attachmentType": "file", "fileName": "paper.pdf",
            "mimeType": "application/pdf", "sizeBytes": 6781834},
   "local": {"assetId": "asset_...",
             "filePath": "<dataDir>/v2/assets/2026/09/28/01-46-03-697-asset_...-paper.pdf",
             "desktopPath": "where the user originally dragged it from"}}
]}
```

Scraping paths out of `msg_content` is unreliable — a long path can be split
across lines in the stored JSON and files go missing without warning. Always
parse the `attachments` field.

Compute the relative path against the **absolute** assets root:

```python
Path(file_path).relative_to(data_dir / "v2/assets")   # correct
Path(file_path).relative_to(Path("v2/assets"))        # ValueError: absolute vs relative
```

The wrong base raises, and the fallback then dumps everything into `unmapped/`,
so the bundle looks fine but the original absolute paths never resolve on import.
The importer reports any reference that still points at the old machine, which is
what this shows up as.

Restoring to `v2/assets/<relative path>` is what makes the conversation's
recorded paths work again on the other machine.

## The runtime history directory: skip it when truncating

`local_runtime_sessions.history_relative_dir` points at
`v2/sessions/<date>/<time>-session_<base64>`, containing `messages.jsonl`,
`history-catalog.json`, `user-message-locators.jsonl`, `manifest.json`, and
friends.

- `messages.jsonl` has more rows than the database (it includes tool events —
  382 rows against 157 database rows in the observed session);
- `history-catalog.json` pins it with `sha256`, `byteLength`, and `messageCount`;
- `user-message-locators.jsonl` records a `byteOffset` and `artifactRevision`
  per message.

So a hand-trimmed copy always fails its integrity check. When truncating:

1. do not migrate the directory;
2. set `history_relative_dir` to `NULL` in the session row;
3. do not write a `05_history_dir/` entry in the bundle.

Omitting it does not affect display or continuation — `local_runtime_message_rows`
is the authoritative conversation.

## Take snapshots with the backup API, not `cp`

The live database sits next to `-wal` and `-shm` sidecars that can be tens of
megabytes. Copying only the main file loses the index state; the symptom is that
*nothing anywhere* is searchable, which is easy to misread as an importer bug.

```python
src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
dst = sqlite3.connect(tmp)
src.backup(dst)
```

Open read-only with `?mode=ro` to avoid accidental writes. Do not set
`immutable=1` — that ignores the WAL.

## Safe import checklist

1. Replace, do not skip. If a session with the same `session_id` exists, delete
   it and everything derived from it — messages, the search-index rows reached
   through their own `rowid`, the per-session asset rows — and write the bundle
   in. Skipping instead leaves a stale conversation that the user reads without
   knowing it is out of date, and skipping only the session row leaves orphaned
   messages and an index pointing at rows that no longer exist.
2. Back up before writing the database. Use SQLite's backup API, which
   produces **one** consistent file, `v2/sqlite/backups/runtime-state.<stamp>.sqlite`
   — not a file-by-file copy of a live WAL database, which can reassemble into
   something that passes `integrity_check` and is empty. To undo, quit the app,
   put that file back as `runtime-state.sqlite`, and **delete any `-wal` and
   `-shm` beside it**, or SQLite replays the stale log over your restored copy.
3. Abort when the local schema is older than the bundle's — cross-version inserts
   can land in the wrong columns.
4. Wrap writes in `BEGIN IMMEDIATE` and roll back on error.
5. Read the row counts back afterwards and report them.
