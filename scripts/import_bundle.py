#!/usr/bin/env python3
"""Restore a migration bundle into a local MiniMax Code installation.

Run this from inside the extracted bundle folder:

    python3 import_bundle.py --dry-run     # rehearse, write nothing
    python3 import_bundle.py               # perform the import

With no flags it asks one question — where to put the project files, defaulting
to a folder named after the project, on your Desktop. Press Enter to
accept it. The MiniMax Code data
directory is always ``~/.minimax`` for a given user and is never asked about;
it still matters, because it is the prefix the stored attachment paths get
rewritten onto, which is what makes a bundle exported by another machine open
correctly. Pass ``--workspace`` to skip the question, or ``--non-interactive``
to take every default without asking.

It does four things:

  1. Copies the bundled project files into the target workspace directory.
  2. Registers that workspace as a project and writes the migrated sessions,
     their messages, and their search index into the local session database.
  3. Restores bundled attachments under ``v2/assets/`` using their original
     relative layout, so the absolute paths stored in the conversation still
     resolve on this machine.
  4. Prints where each piece landed, so nothing is left looking for.

Attachments deliberately do not go into the project folder: the conversation
stores their absolute path under the data directory, so putting a copy anywhere
else would leave every attachment link broken.

Python 3.9+, standard library only. macOS, Linux, and Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BUNDLE = HERE / "bundle"
DEFAULT_PROJECT_SUBDIR = "project_files"
DEFAULT_WORKSPACE_NAME = "Project"
DATA_DIR_NAME = ".minimax"

DB_REL = Path("v2/sqlite/runtime-state.sqlite")
ASSETS_REL = Path("v2/assets")
RUNTIME_SESSIONS_REL = Path("v2/sessions")
MEMORY_SUBDIR = "memory"
CONTEXT_DIGEST_NAME = "09_context_digest.jsonl"
ATTACHION_INDEX_NAME = "08_attachments.json"

SESSION_COLUMNS = [
    "session_id", "record_json", "updated_at_ms", "columnar_version", "agent_name",
    "runtime", "session_type", "status", "archived", "visibility", "session_kind",
    "purpose", "purpose_kind", "origin_cron_id", "parent_session_id", "workspace_dir",
    "project_workspace_dir", "is_default_workspace", "title", "created_at_ms",
    "error_message", "error_code", "extra_data_json", "project_id",
    "history_relative_dir",
]
MESSAGE_COLUMNS = [
    "id", "session_id", "msg_id", "role", "turn_id", "created_at_ms", "data_json",
    "source", "source_context_json",
]
PROJECT_COLUMNS = [
    "project_id", "project_kind", "workspace_dir", "pinned", "hidden", "order_index",
    "recent_at_ms", "latest_activity_at_ms", "session_count", "extra_data_json",
    "created_at_ms", "updated_at_ms",
]
FTS_COLUMNS = [
    "session_id", "session_id_terms", "agent_name_terms", "title_terms",
    "workspace_dir_terms", "purpose_terms", "status_terms", "session_type_terms",
]

MIN_SCHEMA_VERSION = 40  # bundles from older runtimes are not supported
# The only columnar version this importer has been written and tested against.
# A bundle at any other value would be inserted and then silently never join a
# project, because the application's trigger is keyed on this number.
SUPPORTED_COLUMNAR_VERSION = 3


class Abort(Exception):
    """A condition the user needs to resolve before the import can proceed."""


class Escapes(Abort):
    """A bundle path pointing outside the directory it belongs to."""


def to_terms(text):
    """Reproduce the application's character-level tokenisation.

    The app lowercases the text and emits ``"c" + hex(codepoint)`` for every
    character, space separated. The FTS5 table then indexes each chunk as one
    token, which makes session search character based. Writing raw text here
    would parse fine but search differently from native sessions.
    """
    return " ".join(f"c{ord(ch):x}" for ch in str(text or "").lower())


def load_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_manifest(bundle: Path) -> dict:
    path = bundle / "00_manifest.json"
    if not path.exists():
        raise Abort(f"No 00_manifest.json in {bundle} — this is not a migration bundle.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise Abort(
            f"00_manifest.json in {bundle} is not readable JSON: {error}\n"
            f"      The bundle is damaged or incomplete. Copy it again.")
    if not isinstance(manifest, dict):
        raise Abort(f"00_manifest.json in {bundle} is not a JSON object.")
    return manifest


def clean_title(raw: str) -> str:
    return (raw or "").replace("\n", " ").replace("</title>", "").strip()


def stored_attachment_paths(payload_json: str) -> list:
    """Absolute attachment paths recorded in one stored message payload.

    ``payload_json`` is the raw ``data_json`` column of a message row. The
    files the user uploaded live in ``attachments[].local.filePath``. That is
    the path the app opens, so it is the one that has to resolve.
    """
    try:
        payload = json.loads(payload_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    return [(a.get("local") or {}).get("filePath")
            for a in payload.get("attachments") or []
            if (a.get("local") or {}).get("filePath")]


def restore_memory(bundle: Path, data_dir: Path) -> tuple:
    """Put the bundled agent memory back, keeping whatever was already there.

    The memory file is the user's profile and working preferences, and the
    destination may have its own by now. Overwriting it silently would throw
    away whatever was learned here, so the existing file is copied aside first
    and the backup is named in the output.

    Returns (restored_count, bytes, backups).
    """
    source_dir = bundle / MEMORY_SUBDIR
    if not source_dir.is_dir():
        return 0, 0, []
    dest_dir = data_dir / MEMORY_SUBDIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    restored = total = 0
    backups = []
    for source in sorted(source_dir.rglob("*.md")):
        if not source.is_file():
            continue
        dest = dest_dir / source.relative_to(source_dir)
        if source.is_symlink():
            raise Abort(
                f"Refusing to restore memory file {source.name}: it is a "
                f"symbolic link.\n      Nothing has been written.")
        if dest.exists():
            backup = dest.with_name(f"{dest.name}.{stamp}.bak")
            shutil.copy2(dest, backup)
            backups.append(backup)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        restored += 1
        total += dest.stat().st_size
    return restored, total, backups


def deploy_project(source: Path, dest: Path, dry_run: bool) -> tuple:
    """Copy the bundled project snapshot into the target workspace.

    Walks the whole tree. An export made with ``--project-recursive`` puts
    subdirectories in here, and skipping them would drop those files silently
    while still reporting a plausible file count.

    Returns (files copied, relative names of files that already existed and
    were overwritten). Those are announced: the folder on this machine may
    hold work that is now gone, and the user is the only one who still has a
    copy of it.
    """
    if not source.is_dir():
        print(f"      WARNING: no project snapshot at {source}, skipping")
        return 0, []
    # Inside the guard: mkdir used to run before it, so --dry-run created the
    # target folder on a machine it was supposed to touch. The whole point of
    # a rehearsal is that a suspicious receiver can run it safely.
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    count = 0
    replaced = []
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        relative = item.relative_to(source)
        if any(part.startswith("~$") or part == ".DS_Store"
               for part in relative.parts):
            continue
        if not dry_run:
            target = dest / relative
            if item.is_symlink():
                raise Abort(
                    f"Refusing to restore {relative}: it is a symbolic link, "
                    f"and following it would copy a file from this machine "
                    f"into the project.\n"
                    f"      Nothing has been written.")
            if target.is_file():
                replaced.append(str(relative))
            copy_without_symlink(item, target)
        count += 1
    return count, replaced


class ImportLock:
    """Refuse a second concurrent import into the same data directory.

    The database serialises itself correctly, but the attachment files do
    not: two imports write the same destinations, and a size mismatch between
    them gets reported as a damaged bundle when the bundle is perfectly
    fine. One import at a time is the honest fix, and the loser should be
    told that, not sent to re-transfer 300 MB.

    Released on any exit, including a kill, by writing the owner's process id
    and treating a pid that no longer exists as a stale lock.
    """

    def __init__(self, data_dir: Path):
        self.path = data_dir / "v2/sqlite/.import.lock"
        self.held = False

    def _stale(self) -> bool:
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
        return False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL is the atomic part: exactly one process can create it.
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            self.held = True
            # Released on every normal exit. A kill leaves the file behind,
            # and _stale() clears it by noticing the process is gone.
            import atexit
            atexit.register(self.__exit__)
        except FileExistsError:
            if self._stale():
                try:
                    self.path.unlink()
                except OSError:
                    pass
                return self.__enter__()
            owner = self.path.read_text(encoding="utf-8").strip() or "?"
            raise Abort(
                f"Another import into this data directory is already running "
                f"(process {owner}).\n"
                f"      Nothing has been written. Wait for it to finish and "
                f"run the import again.\n"
                f"      Two imports at once would overwrite each other's "
                f"attachment files, which is not something to debug later.")
        return self

    def __exit__(self, *exc):
        if not self.held:
            return False
        self.held = False
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def preflight_versions(db_path: Path, manifest: dict) -> None:
    """Refuse a version mismatch before the project files are copied.

    These checks used to live in the step-2 writer, which runs *after* step 1
    has already put the project into the target folder. The abort then said
    "Nothing has been written" while files were sitting right there -- and a
    person who believes that sentence will not go looking for them.

    Both checks are read-only, so running them twice is harmless.
    """
    conn = sqlite3.connect(db_path, timeout=1.0)
    try:
        target_schema = conn.execute(
            "SELECT MAX(version) FROM local_runtime_v2_schema_migrations"
        ).fetchone()[0]
        source_schema = manifest.get("schemaVersion")
        if target_schema is not None and source_schema is not None \
                and target_schema < source_schema:
            raise Abort(
                f"Local schema v{target_schema} is older than the bundle's "
                f"v{source_schema}. Update MiniMax Code on this machine first.\n"
                f"      Nothing has been written.")
        if target_schema is not None and target_schema < MIN_SCHEMA_VERSION:
            raise Abort(
                f"Local schema v{target_schema} is too old to import safely.\n"
                f"      Nothing has been written.")
        destination_version = probe_columnar_version(conn)
        if destination_version is not None \
                and destination_version != SUPPORTED_COLUMNAR_VERSION:
            raise Abort(
                f"This machine's MiniMax Code expects columnar version "
                f"{destination_version}, but the importer only knows version "
                f"{SUPPORTED_COLUMNAR_VERSION}.\n"
                f"      The conversation would be written and then never appear "
                f"in the list.\n"
                f"      Update this machine to the same version you exported "
                f"from, then run this again.\n"
                f"      Nothing has been written.")
    finally:
        conn.close()


def probe_fts5(db_path: Path) -> None:
    """Refuse early if this Python cannot write the search index.

    The FTS5 table is written in step 2, but the project files go out in
    step 1. Finding out here meant the abort landed after the files were
    already in the target folder while the message still said "Nothing has
    been changed" -- which is exactly the sentence a person trusts and then
    does not restore the backup over.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
    except sqlite3.Error as error:
        raise Abort(
            f"The session database could not be opened: {error}\n"
            f"      Nothing has been written.")
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS "
                     "local_runtime_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS local_runtime_fts5_probe")
        conn.commit()
    except sqlite3.OperationalError as error:
        raise Abort(
            f"This machine cannot write the search index: {error}\n"
            f"      Nothing has been written. MiniMax Code needs an sqlite3 "
            f"built with FTS5, which this Python's does not.\n"
            f"      Install a Python with FTS5 support, or import on a "
            f"machine where MiniMax Code has been launched.")
    finally:
        conn.close()


def probe_database_writable(db_path: Path) -> None:
    """Fail here if the database cannot be written, before anything is copied.

    The project files are copied at step 1 and the database is written at step
    2, so a locked database used to abort *after* the files had landed. The
    message said "nothing has been written", and a user who believes that will
    not go looking for the files or restore the backup.

    Taking and immediately releasing a write lock costs nothing and turns the
    most common failure into one that happens before the first file moves.
    """
    # A free lock is not writability. An empty transaction writes no page, so
    # a read-only database file passes it and then fails at step 2 with the
    # project files already on disk.
    if not os.access(db_path, os.W_OK):
        raise Abort(
            f"The session database is not writable: {db_path}\n"
            f"      Nothing has been written. Fix its permissions (or the "
            f"folder holding it), then run the import again.")
    if not os.access(db_path.parent, os.W_OK):
        raise Abort(
            f"The folder holding the session database is not writable: "
            f"{db_path.parent}\n"
            f"      Nothing has been written. SQLite also has to create the "
            f"-wal and -shm files beside it, so fix the folder's permissions "
            f"and run the import again.")
    conn = sqlite3.connect(db_path, timeout=1.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as error:
        raise Abort(
            f"Database is busy or errored: {error}\n"
            f"      Nothing has been written. Quit MiniMax Code completely on "
            f"this machine, then run the import again.")
    finally:
        conn.close()


def backup_database(db_path: Path, data_dir: Path) -> Path:
    """Snapshot the live database to one file, and prove it is readable.

    A plain copy of a WAL database is not safe: the main file, ``-wal`` and
    ``-shm`` are captured at three different moments, so a commit or checkpoint
    between them leaves a set that reassembles into something else — the export
    script refuses to read the live database for exactly this reason. Use the
    SQLite backup API, which produces a single consistent file, and then run an
    integrity check rather than printing a reassuring message about a copy that
    may not be usable.
    """
    backup_dir = data_dir / "v2/sqlite/backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"runtime-state.{stamp}.sqlite"
    # Written under a name that cannot be mistaken for a good backup, then
    # renamed only after it verifies. A kill or a full disk part-way through
    # leaves a *.partial file, so the newest *.sqlite is always a complete
    # database — which matters, because the undo instructions say take the newest.
    partial = dest.with_name(dest.name + ".partial")
    # A path with '#' or '?' truncates a hand-built file: URI, and a Windows
    # drive letter is not a URI path at all. Quote it properly.
    source = sqlite3.connect(
        "file:" + quote(str(db_path)) + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(partial)
        try:
            with target:
                source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            target.close()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    if result != "ok":
        partial.unlink(missing_ok=True)
        raise Abort(
            f"The safety backup of the session database did not verify "
            f"({result}).\n"
            f"      Nothing has been written.")
    partial.replace(dest)
    prune_backups(backup_dir, keep=5)
    return backup_dir


def prune_backups(backup_dir: Path, keep: int = 5) -> int:
    """Keep the newest few verified backups and delete the rest.

    Each backup is a full copy of the session database -- over 100 MB on a
    real install -- and nothing ever removed them, so every import grew the
    data directory without bound. The undo instructions say to take the
    newest, so keeping a handful is enough; anything older than that is
    deleted along with its -wal and -shm siblings, which restore a lone
    .sqlite without them would miss.

    Returns how many files were removed.
    """
    try:
        done = sorted((p for p in backup_dir.glob("*.sqlite")
                       if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for stale in done[keep:]:
        for path in (stale, stale.with_name(stale.name + "-wal"),
                     stale.with_name(stale.name + "-shm")):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


# Fields whose absolute paths have to follow the files onto this machine.
#
# `tool_calls` and `thinking_content` are deliberately excluded. They record
# what was executed and reasoned at the time, and rewriting them would falsify
# that record. `msg_content` and `attachments` are the opposite: they are what
# the user is meant to open. A delivered-file card embeds the path of a file
# that really did move with the project, so leaving the old path there is what
# makes the app report "file does not exist or has been moved".
REPOINTED_FIELDS = ("attachments", "msg_content")


def remap_paths(node, replacements: list):
    """Return ``node`` with any old root in ``replacements`` swapped for its new one.

    ``replacements`` is a list of ``(old_prefix, new_prefix)``, longest first.
    The prefix is replaced wherever it appears, not only at the start of the
    string: a delivered-file reference sits inside markup like
    ``src="/old/path/file.docx"``, so a startswith test would miss it.

    The match must end on a path boundary. A bare substring replace turns
    ``/old/Project-old/notes.md`` into ``/new/Project-old/notes.md`` when
    the project is ``/old/Project`` -- and a ``.minimax-backup`` sibling is
    an entirely plausible real folder. That result is worse than a dead link:
    it is a live link pointing at the wrong file, and nothing reports it.
    """
    if isinstance(node, dict):
        return {k: remap_paths(v, replacements) for k, v in node.items()}
    if isinstance(node, list):
        return [remap_paths(v, replacements) for v in node]
    if isinstance(node, str):
        for old, new in replacements:
            if not old or old not in node:
                continue
            out = []
            at = 0
            while True:
                hit = node.find(old, at)
                if hit < 0:
                    out.append(node[at:])
                    break
                after = node[hit + len(old):hit + len(old) + 1]
                if after and after not in ("/", "\\"):
                    # Part of a longer name, not the path itself. Keep it.
                    out.append(node[at:hit + len(old)])
                    at = hit + len(old)
                    continue
                out.append(node[at:hit])
                out.append(new)
                at = hit + len(old)
            node = "".join(out)
        return node
    return node


def count_stale_paths(message_rows: list, old_data_dirs: list,
                      old_workspace: str | None, data_dir: Path,
                      workspace: Path) -> dict:
    """Count the stored paths that still name the machine we came from.

    Deliberately counts every occurrence anywhere in the row, not only the
    fields the rewriter knows about. A count that only covered the allowlist
    would be zero by construction and would prove nothing.
    """
    needles = [d for d in (old_data_dirs or []) if d and d not in ("/", "//")]
    if old_workspace and len(old_workspace) >= 4:
        needles.append(old_workspace)
    # The new locations are not stale, and one can contain the other.
    keep = {str(data_dir), str(workspace)}
    needles = [n for n in needles if n not in keep]
    if not needles:
        return {"total": 0, "examples": []}
    total, examples = 0, []
    for message in message_rows:
        for column in ("data_json", "source_context_json"):
            blob = message.get(column)
            if not isinstance(blob, str):
                continue
            for needle in needles:
                hits = blob.count(needle)
                if not hits:
                    continue
                # A row that was repointed can still quote the old path in
                # prose; that is not a link. Count the JSON string values only.
                try:
                    parsed = json.loads(blob)
                except (json.JSONDecodeError, TypeError):
                    continue
                for text in iter_string_values(parsed):
                    if needle in text:
                        total += 1
                        if len(examples) < 3:
                            examples.append(needle.rstrip("/").rsplit("/", 1)[-1]
                                            or needle)
                        break
    return {"total": total, "examples": examples}


def iter_string_values(node):
    """Yield every string anywhere in a decoded JSON structure."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from iter_string_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_string_values(value)


def rewrite_paths(message_rows: list, replacements: list) -> int:
    """Repoint stored absolute paths onto this machine. Returns messages changed."""
    replacements = [(old, new) for old, new in replacements
                    if old and new and old != new]
    if not replacements:
        return 0
    changed = 0
    for message in message_rows:
        for column in ("data_json", "source_context_json"):
            blob = message.get(column)
            if not isinstance(blob, str):
                continue
            if not any(old in blob for old, _ in replacements):
                continue
            try:
                parsed = json.loads(blob)
            except (json.JSONDecodeError, TypeError):
                continue
            before = {f: parsed[f] for f in REPOINTED_FIELDS if f in parsed}
            for field in REPOINTED_FIELDS:
                if field in parsed:
                    parsed[field] = remap_paths(parsed[field], replacements)
            if any(parsed[f] != before[f] for f in before):
                message[column] = json.dumps(parsed, ensure_ascii=False)
                changed += 1
    return changed


# Tables that hold state owned by a single conversation. Deleting a session
# means emptying these; anything not listed (cron, workspace indexing, token
# telemetry) belongs to the machine, not the conversation, and is left alone.
SESSION_OWNED_TABLES = [
    "local_runtime_message_rows",
    "local_runtime_messages",
    "local_runtime_pi_history_rows",
    "local_runtime_session_assets",
    "local_runtime_session_asset_index_state",
    "local_runtime_session_agent_state",
    "local_runtime_session_agent_definitions",
    "local_runtime_session_locks",  # cleared before the early return too
    "local_runtime_session_clio_state",
    "local_runtime_session_clio_outbox",
    "local_runtime_thread_goals",
    "local_runtime_query_view_states",
    "local_runtime_turn_diffs",
    "local_runtime_turn_diff_journal",
    "local_runtime_turn_ingress",
    "local_runtime_turn_ingress_client_requests",
    "local_runtime_v2_memory_session_states",
    "local_runtime_queue_items",
    "questionnaire_requests",
    "session_resources",
    "session_turn_resources",
]


def conversation_is_busy(conn, session_id: str) -> bool:
    """Is the app holding this conversation, or part-way through a turn?

    A lock row or an unfinished thread goal means the live app owns the
    conversation right now. Overwriting it underneath is how a second turn gets
    to start on the same rows.
    """
    try:
        locked = conn.execute(
            "SELECT COUNT(*) FROM local_runtime_session_locks WHERE session_id = ?",
            (session_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        locked = 0
    if locked:
        return True
    try:
        goal = conn.execute(
            "SELECT COUNT(*) FROM local_runtime_thread_goals WHERE session_id = ?",
            (session_id,)).fetchone()[0]
    except sqlite3.OperationalError:
        goal = 0
    return bool(goal)


def purge_session(conn, session_id: str) -> int:
    """Remove a conversation and everything derived from it.

    The search index has to go first and through its own rowid: the keys table
    is the only thing that maps a session to the FTS rows, so the mapping has
    to be read before it is dropped.

    Returns 1 if a session row was found and removed.
    """
    # Clear the lock first, and do it whether or not a session row exists. An
    # interrupted earlier import can leave a lock behind with no session to own
    # it, and a leftover lock makes the app treat the conversation as running.
    try:
        conn.execute("DELETE FROM local_runtime_session_locks WHERE session_id = ?",
                     (session_id,))
    except sqlite3.OperationalError:
        pass  # table absent on an older install

    present = conn.execute(
        "SELECT COUNT(*) FROM local_runtime_sessions WHERE session_id = ?",
        (session_id,)).fetchone()[0]
    if not present:
        return 0

    rowids = [r[0] for r in conn.execute(
        "SELECT fts_rowid FROM local_runtime_session_fts_keys WHERE session_id = ?",
        (session_id,))]
    for rowid in rowids:
        conn.execute("DELETE FROM local_runtime_sessions_fts WHERE rowid = ?",
                     (rowid,))
    conn.execute("DELETE FROM local_runtime_session_fts_keys WHERE session_id = ?",
                 (session_id,))

    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in SESSION_OWNED_TABLES:
        if table in existing:
            conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))

    conn.execute("DELETE FROM local_runtime_sessions WHERE session_id = ?",
                 (session_id,))
    return present


def probe_columnar_version(conn) -> int | None:
    """Which columnar version this machine's runtime actually acts on.

    Read from the trigger that attaches a session to its project, because that
    is the rule the destination will apply to whatever gets written. Returns
    None when the schema is unfamiliar, in which case the constant is the best
    guess available.
    """
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'trg_local_runtime_project_session%'").fetchall()
    except sqlite3.OperationalError:
        return None
    import re as _re
    for (sql,) in rows:
        match = _re.search(r"columnar_version\s*=\s*(\d+)", sql or "")
        if match:
            return int(match.group(1))
    return None


def import_sessions(db_path: Path, manifest: dict, session_rows: list,
                    message_rows: list, workspace: Path) -> tuple:
    """Write every session in the bundle, replacing any that already exist.

    Returns (written, replaced, messages, written_session_ids).
    """
    by_session: dict = {}
    for message in message_rows:
        by_session.setdefault(message.get("session_id"), []).append(message)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        target_schema = conn.execute(
            "SELECT MAX(version) FROM local_runtime_v2_schema_migrations").fetchone()[0]
        source_schema = manifest.get("schemaVersion")
        print(f"      schema: bundle v{source_schema} / local v{target_schema}")
        if target_schema is not None and source_schema is not None \
                and target_schema < source_schema:
            raise Abort(
                f"Local schema v{target_schema} is older than the bundle's "
                f"v{source_schema}. Update MiniMax Code on this machine first.")
        if target_schema is not None and target_schema < MIN_SCHEMA_VERSION:
            raise Abort(f"Local schema v{target_schema} is too old to import safely.")

        # The application's own bookkeeping — the trigger that assigns a
        # project to a session and recomputes the project's count — only fires
        # for one columnar version. A session written at another one is accepted
        # and then never joins a project, so it is absent from the list with
        # nothing to indicate why.
        #
        # Ask the destination what it actually wants, rather than assuming this
        # machine's version: checking the bundle alone says nothing about a
        # destination that has been updated since the export.
        destination_version = probe_columnar_version(conn)
        if destination_version is not None \
                and destination_version != SUPPORTED_COLUMNAR_VERSION:
            raise Abort(
                f"This machine's MiniMax Code expects columnar version "
                f"{destination_version}, but the importer only knows version "
                f"{SUPPORTED_COLUMNAR_VERSION}.\n"
                f"      The conversation would be written and then never appear "
                f"in the list.\n"
                f"      Update this machine to the same version you exported "
                f"from, then run this again.\n"
                f"      Nothing has been written.")
        # Check the value that will actually be written, not the one the
        # manifest advertises: the guard used to read the manifest and then
        # write the row, so a row at an unknown version sailed through.
        row_versions = {row.get("columnar_version")
                        for row in session_rows}
        row_versions.discard(None)
        if row_versions - {SUPPORTED_COLUMNAR_VERSION}:
            raise Abort(
                f"This bundle's conversation rows carry columnar version "
                f"{sorted(row_versions)}, but only "
                f"{SUPPORTED_COLUMNAR_VERSION} is supported.\n"
                f"      Sessions at an unknown version never join a project, so "
                f"they would not appear in the list.\n"
                f"      Nothing has been written.")
        bundle_version = manifest.get("columnarVersion")
        if bundle_version is not None and bundle_version != SUPPORTED_COLUMNAR_VERSION:
            raise Abort(
                f"This bundle was written with columnar version "
                f"{bundle_version}, but this importer only knows version "
                f"{SUPPORTED_COLUMNAR_VERSION}.\n"
                f"      Sessions at an unknown version are not attached to a "
                f"project and would not appear in the list.\n"
                f"      Nothing has been written.")

        backup_dir = backup_database(db_path, db_path.parent.parent.parent)
        print(f"      backed up the existing database to {backup_dir}")

        conn.execute("BEGIN IMMEDIATE")
        now_ms = int(datetime.now().timestamp() * 1000)
        project_id = None

        written = present = message_total = 0
        written_ids: list = []
        for session_row in session_rows:
            session_id = session_row.get("session_id")
            if not session_id:
                continue
            # Re-importing replaces rather than skips. A bundle is a snapshot
            # of the conversation as of the export, so the older copy in the
            # database is the stale one and keeping it would leave the user
            # reading whatever happened to be there first.
            # Checked before purging: the purge removes the very rows that
            # would say the conversation is in use.
            in_use = conversation_is_busy(conn, session_id)
            if purge_session(conn, session_id):
                present += 1
                print(f"      replaced existing conversation {session_id}"
                      + ("  [IN USE]" if in_use else ""))
                if in_use:
                    print(f"        WARNING: this conversation is in use on "
                          f"this machine. Replacing it drops its lock and any "
                          f"turn in progress.")
                    print(f"        A backup of the database as it was just now "
                          f"is in {backup_dir}; restore from it if this was not "
                          f"intended.")
            if project_id is None:
                row = conn.execute(
                    "SELECT project_id FROM local_runtime_projects WHERE workspace_dir = ?",
                    (str(workspace),)).fetchone()
                if row:
                    project_id = row[0]
                    print(f"      reusing project, project_id={project_id}")
                else:
                    project_id = conn.execute(
                        "SELECT COALESCE(MAX(project_id), 0) + 1 FROM "
                        "local_runtime_projects").fetchone()[0]
                    order_index = conn.execute(
                        "SELECT COALESCE(MAX(order_index), 0) + 1 FROM "
                        "local_runtime_projects").fetchone()[0]
                    record = {"projectId": project_id, "projectKind": "workspace",
                              "workspaceDir": str(workspace), "createdAtMs": now_ms,
                              "updatedAtMs": now_ms}
                    values = {
                        "project_id": project_id, "project_kind": "workspace",
                        "workspace_dir": str(workspace), "pinned": 0, "hidden": 0,
                        "order_index": order_index, "recent_at_ms": now_ms,
                        "latest_activity_at_ms": now_ms, "session_count": 0,
                        "extra_data_json": json.dumps(record, ensure_ascii=False),
                        "created_at_ms": now_ms, "updated_at_ms": now_ms}
                    conn.execute(
                        f"INSERT INTO local_runtime_projects "
                        f"({','.join(PROJECT_COLUMNS)}) "
                        f"VALUES ({','.join('?' * len(PROJECT_COLUMNS))})",
                        [values[c] for c in PROJECT_COLUMNS])
                    print(f"      created project {workspace}, project_id={project_id}")

            # Only carry across columns the bundle actually has. Writing an
            # explicit NULL for one the bundle omits defeats a NOT NULL DEFAULT
            # and crashes the insert.
            values = {c: session_row.get(c) for c in SESSION_COLUMNS
                      if c in session_row and session_row.get(c) is not None}
            values["session_id"] = session_id
            values["workspace_dir"] = str(workspace)
            values["project_workspace_dir"] = str(workspace)
            values["project_id"] = project_id
            # A session the app flagged as belonging to its own default
            # workspace would be matched by a trigger looking for
            # project_kind='default', land in a project with no folder, and
            # leave an empty one behind. This import always means "the folder
            # the user named", so that flag is cleared.
            values["is_default_workspace"] = 0
            values["status"] = "idle"
            values["archived"] = 0
            values["visibility"] = "visible"
            try:
                record = json.loads(session_row.get("record_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                record = {}
            record.update({"sessionId": session_id, "workspaceDir": str(workspace),
                           "archived": False, "status": "idle",
                           "visibility": "visible", "projectId": project_id})
            values["record_json"] = json.dumps(record, ensure_ascii=False)
            # Bind only the columns that have a value. Listing the full set and
            # passing None for the rest sends an explicit NULL, which defeats a
            # NOT NULL DEFAULT and aborts the insert.
            columns = [c for c in SESSION_COLUMNS if c in values]
            conn.execute(
                f"INSERT INTO local_runtime_sessions ({','.join(columns)}) "
                f"VALUES ({','.join('?' * len(columns))})",
                [values[c] for c in columns])

            rows = by_session.get(session_id, [])
            if rows:
                next_id = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM "
                    "local_runtime_message_rows").fetchone()[0]
                payload = []
                for offset, message in enumerate(rows):
                    item = {c: message.get(c) for c in MESSAGE_COLUMNS}
                    item["id"] = next_id + offset
                    item["session_id"] = session_id
                    payload.append([item.get(c) for c in MESSAGE_COLUMNS])
                conn.executemany(
                    f"INSERT INTO local_runtime_message_rows "
                    f"({','.join(MESSAGE_COLUMNS)}) "
                    f"VALUES ({','.join('?' * len(MESSAGE_COLUMNS))})", payload)
                message_total += len(payload)

            fts_values = {
                "session_id": session_id,
                "session_id_terms": to_terms(session_id),
                "agent_name_terms": to_terms(values.get("agent_name")),
                "title_terms": to_terms(values.get("title")),
                "workspace_dir_terms": to_terms(workspace),
                "purpose_terms": to_terms(values.get("purpose")),
                "status_terms": to_terms("idle"),
                "session_type_terms": to_terms(values.get("session_type")),
            }
            cursor = conn.execute(
                f"INSERT INTO local_runtime_sessions_fts ({','.join(FTS_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(FTS_COLUMNS))})",
                [fts_values[c] for c in FTS_COLUMNS])
            conn.execute(
                "INSERT INTO local_runtime_session_fts_keys (fts_rowid, session_id) "
                "VALUES (?, ?)", (cursor.lastrowid, session_id))
            written += 1
            written_ids.append(session_id)

        # The application recomputes the project's session count from its own
        # rules inside a database trigger on every session insert. That rule is
        # not part of any documented schema, so recompute it here as well rather
        # than incrementing blindly: an increment drifts the moment a session is
        # replaced, or the project already held conversations of its own.
        if project_id is not None:
            conn.execute(
                "UPDATE local_runtime_projects SET session_count = ("
                "  SELECT COUNT(*) FROM local_runtime_sessions s"
                "  WHERE s.project_id = local_runtime_projects.project_id"
                f"    AND s.columnar_version = {SUPPORTED_COLUMNAR_VERSION}"
                "    AND s.archived = 0"
                "    AND s.visibility <> 'hidden'"
                "    AND s.session_kind NOT IN ('peek', 'channel', 'cron')"
                "    AND s.parent_session_id IS NULL)"
                " WHERE project_id = ?", (project_id,))

        conn.commit()
        return written, present, message_total, written_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def contained_by(base: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``base``, or return None if it escapes.

    A bundle travels between machines and is not necessarily friendly. A
    ``relativePath`` of ``../evil`` would otherwise land outside the attachments
    directory, so resolve and require the result to stay inside.
    """
    if not relative or Path(relative).is_absolute():
        return None
    # A link on the *base* is the one that slips through a leaf-only test:
    # the child is an ordinary path, both sides resolve into the same real
    # directory, and containment passes. Check the base as well.
    if base.is_symlink():
        raise Escapes(
            f"{base.name} is a symbolic link, and following it would copy a "
            f"file from this machine into the install.\n"
            f"      Nothing has been written. Repack the bundle without links.")
    raw = base / relative
    # Checked before resolving: resolve() follows the link, so afterwards
    # is_symlink() is always false and a bundled link would slip through.
    if raw.is_symlink():
        raise Abort(
            f"{raw.name} is a symbolic link, and following it would copy a "
            f"file from this machine into the install.\n"
            f"      Nothing has been written. Repack the bundle without links.")
    candidate = raw.resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        # Refuse rather than returning None: a caller reading None would count
        # this as a missing file and carry on, which is how a hostile bundle
        # walks past the check.
        raise Escapes(
            f"{relative!r} points outside {base}.\n"
            f"      Nothing has been written. This bundle is not a plain "
            f"migration bundle.")
    return candidate


def copy_without_symlink(source: Path, dest: Path) -> None:
    """Copy a regular file, refusing symlinks. Never leaves a partial file.

    ``shutil.copy2`` follows a symlink, so a bundle could carry a link named
    like an attachment and quietly copy a file from this machine into the data
    directory — where the next export would ship it onward.

    The copy is written to a temporary name and renamed into place, because
    ``copy2`` is not atomic. A process killed mid-copy otherwise leaves a
    half-written file at the real destination name: it exists, it opens, and
    the next import overwrites it without ever mentioning that the earlier one
    was interrupted. A rename either happens or does not, so a reader sees
    the old complete file or the new complete file, never a splice of two.
    """
    if source.is_symlink():
        raise Abort(
            f"Refusing to import {source.name}: it is a symbolic link, and "
            f"following it would copy a file from this machine into the "
            f"install.\n"
            f"      Nothing has been written. Repack the bundle without links.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    scratch = dest.with_name(dest.name + ".incoming")
    try:
        shutil.copy2(source, scratch)
        os.replace(scratch, dest)
    except BaseException:
        # Includes KeyboardInterrupt and SystemExit: a half-written scratch
        # file is litter, and a failure must not leave one behind.
        try:
            scratch.unlink()
        except OSError:
            pass
        raise


def restore_attachments(bundle: Path, data_dir: Path):
    """Copy bundled attachment files into the local data directory.

    Returns (restored, missing, total_bytes, parked). ``parked`` lists the
    files that had no relative path on the sending machine: the exporter
    still ships their bytes, and they are saved under `unmapped/` here so
    the content is not thrown away, but the conversation's own links to
    them cannot be repaired and the report has to say so.
    """
    index_path = bundle / "08_attachments.json"
    root = bundle / "attachments"
    if not index_path.exists() or not root.is_dir():
        return 0, 0, 0, []
    parsed = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise Abort(
            f"08_attachments.json is a {type(parsed).__name__}, not an object.\n"
            f"      Nothing has been written. The bundle is malformed.")
    index = parsed
    assets_root = data_dir / ASSETS_REL
    restored = missing = total = 0
    parked = []
    for meta in index.values():
        relative = meta.get("relativePath")
        if relative:
            source = contained_by(root, relative)
            if source is None or not source.exists():
                missing += 1
                continue
            dest = contained_by(assets_root, relative)
        else:
            # Shipped by the exporter under its bare name. Previously this
            # was counted as "missing" and the user was told to re-copy the
            # bundle, which can never help: the bytes are already here, they
            # just have nowhere in the assets tree to go.
            name = Path(str(meta.get("fileName") or "")).name
            if not name:
                missing += 1
                continue
            source = contained_by(root, f"unmapped/{name}")
            if source is None or not source.exists():
                missing += 1
                continue
            dest = contained_by(assets_root, f"unmapped/{name}")
            parked.append((name, dest))
        copy_without_symlink(source, dest)
        total += dest.stat().st_size
        restored += 1
    return restored, missing, total, parked


def default_desktop(home: Path) -> Path:
    """A sensible place to put things, on whichever platform this is.

    ``~/Desktop`` does not exist on Windows, and many Linux setups put it in a
    different language or not at all. Prefer whatever is actually there, and
    fall back to a folder we create ourselves rather than assuming one.
    """
    for name in ("Desktop", "桌面", "Escritorio"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home


def ask(label: str, default: Path) -> Path:
    """Ask for a directory, showing the default and taking it on empty input.

    Only asks when a person is actually there. When stdin is piped or
    redirected the default is used, so scripted and unattended runs keep
    working exactly as before.

    The answer is cleaned up rather than trusted, because all three of these
    are things people actually type and each one lands the files somewhere
    surprising:

    - **shell quotes** -- a quoted path pasted from a terminal keeps its
      quotes, and the folder is then *named* `"` something.
    - **a bare name** -- resolved against the current directory, which is the
      bundle folder they are standing in.
    - **``~``** -- expands to the home directory, so the whole project lands
      loose in it.
    """
    if not sys.stdin.isatty():
        return default
    try:
        answer = input(f"  {label}\n    [{default}] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    # Pasting a terminal command is the common case; take the path out of it
    # rather than creating a folder named after the quotes.
    if len(answer) > 1 and answer[0] == answer[-1] and answer[0] in ("'", '"'):
        answer = answer[1:-1].strip()
    if not answer:
        return default
    return Path(answer).expanduser()


MEDIA_SRC = re.compile(r'<media[^>]*\bsrc="([^"]+)"')


def restore_history_dir(bundle: Path, data_dir: Path) -> int:
    """Restore the bundled runtime history directory, if there is one.

    The exporter copies it, so leaving the session rows pointing at a directory
    that never arrives gives the destination a dangling reference. Restoring it
    keeps the bundle and the rows consistent.
    """
    source = bundle / "05_history_dir"
    if not source.is_dir():
        return 0
    dest = data_dir / RUNTIME_SESSIONS_REL
    if not dest.is_dir():
        return 0
    # Every member, not just subdirectories. The runtime history carries real
    # files at its top level (messages.jsonl, manifest.json, llm-call.json --
    # about 1.3 MB) and they were never restored at all, on any import.
    copied_dirs = copied_files = 0
    for rel_dir in sorted(source.iterdir()):
        # copytree follows symlinks by default, so a bundled link would pull a
        # local directory into the install and the next export would ship it.
        if rel_dir.is_symlink():
            raise Escapes(
                f"The history directory contains a symbolic link "
                f"({rel_dir.name}).\n"
                f"      Nothing has been written. Repack the bundle without "
                f"links.")
        target = dest / rel_dir.name
        if any(part.startswith("..") for part in rel_dir.relative_to(source).parts):
            continue
        if rel_dir.is_file():
            copy_without_symlink(rel_dir, target)
            copied_files += 1
            continue
        # Copy into place even when the target exists. Skipping it meant a
        # half-copied directory from an interrupted run was accepted for ever
        # and never repaired.
        for item in rel_dir.rglob("*"):
            if item.is_symlink():
                raise Escapes(
                    f"The history directory contains a symbolic link "
                    f"({item.name}).\n"
                    f"      Nothing has been written. Repack the bundle "
                    f"without links.")
        # copytree refuses a non-empty target, so the old copy is moved aside
        # first rather than silently kept.
        if target.exists():
            doomed = target.with_name(target.name + ".superseded")
            if doomed.exists():
                shutil.rmtree(doomed, ignore_errors=True)
            os.replace(target, doomed)
            shutil.rmtree(doomed, ignore_errors=True)
        shutil.copytree(rel_dir, target, symlinks=False)
        copied_dirs += 1
    return copied_dirs + copied_files


def restore_context_digest(bundle: Path, workspace: Path) -> Path | None:
    """Place the runtime's compaction summary next to the project files.

    The summary is the runtime's own compressed view of the conversation. It is
    not a message row, so it cannot be read back out of the database — it has
    to travel as a file. Writing it into the project folder is what makes it
    reachable: the agent reads the project directory when asked to pick the
    conversation up.
    """
    source = bundle / CONTEXT_DIGEST_NAME
    if not source.is_file():
        return None
    if source.is_symlink():
        raise Escapes(
            f"{CONTEXT_DIGEST_NAME} is a symbolic link, and following it would "
            f"copy a file from this machine into the project.\n"
            f"      Nothing has been written.")
    dest = workspace / CONTEXT_DIGEST_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


def audit_file_cards(db_path: Path, session_ids: list) -> tuple:
    """Check the delivered-file links the conversation renders as cards.

    The project snapshot only carries the workspace's own files. A card that
    points somewhere else — a file produced earlier and kept outside the
    project — cannot be brought along, and the app reports it as missing only
    when the card is clicked. This counts them up front.

    Returns (cards, broken, the_broken_paths).
    """
    if not session_ids:
        # Nothing was written, so there are no cards to check. Refusing here
        # would be a lie: the project files and memory are already on disk by
        # this point, and claiming otherwise hides a real side effect.
        return 0, 0, []
    placeholders = ",".join("?" * len(session_ids))
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT data_json FROM local_runtime_message_rows "
            f"WHERE session_id IN ({placeholders})", session_ids).fetchall()
    finally:
        conn.close()

    cards = set()
    for (blob,) in rows:
        if not blob:
            continue
        try:
            text = json.loads(blob).get("msg_content") or ""
        except (json.JSONDecodeError, TypeError):
            continue
        cards.update(MEDIA_SRC.findall(text))
    broken = sorted(p for p in cards if not Path(p).exists())
    return len(cards), len(broken), broken


def preflight_bundle(bundle: Path, project_subdir: str) -> int:
    """Check everything the bundle can make us do, before writing anything.

    Without this the refusal happens at step [3/4], by which point the
    conversation is already in the database and "nothing has been written" is a
    lie. It also makes --dry-run mean something on a hostile bundle.
    """
    problems = 0
    index_path = bundle / "08_attachments.json"
    if index_path.is_file() and not index_path.is_symlink():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise Abort(f"08_attachments.json is not readable JSON: {error}\n"
                        f"      Nothing has been written.")
        root = bundle / "attachments"
        if root.is_dir():
            unparked = 0
            for meta in (index or {}).values():
                relative = meta.get("relativePath")
                if not relative:
                    # Not an error any more: the exporter ships these under
                    # attachments/unmapped/ and the import now saves them
                    # there. They used to be counted, dropped, and blamed on
                    # a failed copy.
                    unparked += 1
                    continue
                try:
                    contained_by(root, relative)
                except Escapes as error:
                    raise Abort(f"{error}\n      Nothing has been written.")
            problems += unparked
    # The project snapshot too: a link in here was previously only noticed
    # while copying, by which time earlier files were already in the workspace
    # and "nothing has been written" was untrue.
    # Every member the importer reads or writes from, not just the ones we
    # remembered to list.
    for name in ("attachments", ATTACHION_INDEX_NAME, "00_manifest.json",
                 "01_messages.jsonl", "02_sessions.json",
                 CONTEXT_DIGEST_NAME, "05_history_dir", MEMORY_SUBDIR,
                 project_subdir):
        path = bundle / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise Escapes(
                f"{name} is a symbolic link, and following it would copy a file "
                f"from this machine into the install.\n"
                f"      Nothing has been written. Repack the bundle without "
                f"links.")
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_symlink():
                    raise Escapes(
                        f"{name} contains a symbolic link ({item.name}).\n"
                        f"      Nothing has been written. Repack the bundle "
                        f"without links.")
    return problems


def preflight_destination(bundle: Path, data_dir: Path, dry_run: bool) -> int:
    """Refuse a link already sitting where an attachment would be written.

    A link planted at the target path would make the copy write through it,
    and a link on the assets root would move every attachment somewhere else
    on this machine. Both are refused -- but only useful if it happens
    *before* the conversation is written, which is why this runs next to
    preflight_bundle rather than inside the copy step.

    Returns the number of attachment paths inspected. With --dry-run nothing
    is written, so the check is skipped and the count is still reported.
    """
    index_path = bundle / "08_attachments.json"
    if not index_path.is_file():
        return 0
    try:
        parsed = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0  # preflight_bundle already refused a damaged index
    if not isinstance(parsed, dict):
        return 0
    root = data_dir / ASSETS_REL
    if not dry_run and root.is_symlink():
        raise Escapes(
            f"{root} is a symbolic link, and every attachment would be "
            f"written through it.\n"
            f"      Nothing has been written. Remove the link, or point "
            f"--data-dir somewhere else.")
    seen = 0
    for meta in parsed.values():
        relative = meta.get("relativePath") if isinstance(meta, dict) else None
        if not relative:
            # Parked files land under unmapped/ and were skipped here
            # entirely, which left the one path the unmapped fix writes to
            # unguarded.
            name = Path(str(meta.get("fileName") or "")).name
            relative = f"unmapped/{name}" if name else None
        if not relative:
            continue
        seen += 1
        if dry_run:
            continue
        target = root / relative
        # Every component, not just the leaf. A link on v2/assets/2026 sends
        # the file somewhere else entirely, and the leaf check sees an
        # ordinary path.
        walk = root
        for component in Path(relative).parts:
            walk = walk / component
            if walk.is_symlink():
                raise Escapes(
                    f"{walk} is a symbolic link, and the copy would write "
                    f"through it.\n"
                    f"      Nothing has been written. Remove the link, then "
                    f"run the import again.")
    return seen


def read_expected_sizes(bundle: Path, data_dir: Path) -> tuple:
    """Load the bundle's recorded attachment sizes, keyed by this machine's path.

    Returns (expected_sizes, note). `note` is non-empty when the size check
    could not be armed -- the caller prints it, because a size check that
    silently stops checking is worse than one that was never there.

    Keyed on the bundle's own copy rather than the source path: the stored
    paths have already been repointed at this machine, so the old absolute
    path would never match.
    """
    index_path = bundle / "08_attachments.json"
    if not index_path.is_file():
        return {}, "no attachment index in the bundle, so sizes were not checked"
    try:
        raw = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"attachment index unreadable ({exc}), sizes were not checked"
    if not isinstance(raw, dict):
        return {}, ("attachment index is not a mapping, "
                    "sizes were not checked")
    expected = {}
    skipped = 0
    for meta in raw.values():
        if not isinstance(meta, dict):
            skipped += 1
            continue
        declared = meta.get("actualSizeBytes")
        name = meta.get("relativePath")
        if not name:
            # A parked file has no relativePath; the exporter still recorded
            # its size, and it is restored under this key. Skipping it meant
            # the only attachments the unmapped fix rescues were the only
            # ones never verified complete.
            name = Path(str(meta.get("fileName") or "")).name
            name = f"unmapped/{name}" if name else None
        # A zero-length attachment is legal and must be checked like any
        # other, so the test is on type rather than truthiness. `0` used to
        # be dropped here and then also skipped at the comparison, which
        # meant the file was never checked and nothing said so. A negative
        # size is not a size at all, and accepting one would report a
        # perfect file as damaged and send the user to re-copy 300 MB.
        if (name and isinstance(declared, int)
                and not isinstance(declared, bool) and declared >= 0):
            expected[(data_dir / ASSETS_REL / name).resolve()] = declared
        else:
            skipped += 1
    if not expected:
        # Reachable with an empty `{}` index. Silence here would let the
        # caller certify files it never compared.
        return {}, "the bundle records no attachment sizes, so none were checked"
    note = (f"{skipped} attachment record(s) carried no size, "
            "so those were not checked") if skipped else ""
    return expected, note


def audit_attachments(db_path: Path, session_ids: list, bundle: Path,
                     data_dir: Path) -> tuple:
    """Re-read the written messages and check every attachment path opens.

    The restore step reports what it copied, but the conversation is what the
    app actually follows. This reads the paths back out of the database the
    way the app will, and stats them, so a mismatch between "copied" and
    "openable" cannot pass silently.

    Returns (paths_referenced, missing, mismatched, size_checked, note).

    The two failure kinds are kept apart on purpose. "Missing" tells the
    user to go looking for a file that is not there; a wrong-size file is
    sitting right there, and sending them to fetch it again is the only
    useful advice. Calling both "missing" sends them hunting for a file
    that already exists.

    `size_checked` is how many files were actually compared against a
    recorded size, which is not always every file. The caller reports that
    number rather than a bare ratio, because a check that ran zero times
    must not be printed as though everything passed it.
    """
    placeholders = ",".join("?" * len(session_ids))
    conn = sqlite3.connect(db_path)
    try:
        # source_context_json is read as well: the rewriter repoints paths in
        # both columns, so a path that lives only in that one was moved onto
        # this machine and then never looked at.
        rows = conn.execute(
            f"SELECT data_json, source_context_json "
            f"FROM local_runtime_message_rows "
            f"WHERE session_id IN ({placeholders})", session_ids).fetchall()
    finally:
        conn.close()

    paths = set()
    for row in rows:
        for blob in row:
            paths.update(stored_attachment_paths(blob or ""))
    # Existence is not enough: a copy interrupted part-way leaves a file that
    # is present, openable, and half the size it should be. The bundle records
    # the original size, so compare rather than assume.
    expected, note = read_expected_sizes(bundle, data_dir)
    missing, mismatched, checked = [], [], 0
    for path in paths:
        target = Path(path)
        if not target.exists():
            missing.append(path)
            continue
        want = expected.get(target.resolve())
        if want is None:
            continue
        checked += 1
        if target.stat().st_size != want:
            mismatched.append((path, target.stat().st_size, want))
    return len(paths), missing, mismatched, checked, note


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a migration bundle into a local MiniMax Code installation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE),
                        help="bundle directory (default: ./bundle next to this script)")
    parser.add_argument("--project-subdir", default=DEFAULT_PROJECT_SUBDIR,
                        help="project snapshot subfolder inside the bundle")
    parser.add_argument("--data-dir", default=None,
                        help=f"MiniMax Code data directory "
                             f"(default: ~/{DATA_DIR_NAME}; it is always the same "
                             "for a given user, so it is never asked about)")
    parser.add_argument("--home", default=None,
                        help="user home directory (default: system home)")
    parser.add_argument("--workspace", default=None,
                        help="project folder to create on this machine "
                             "(default: the project's own name, inside the first of "
                             "~/Desktop, ~/桌面 or ~/Escritorio that exists)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would happen without writing anything")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never ask anything, take every default")
    args = parser.parse_args()

    if args.non_interactive:
        sys.stdin = open(os.devnull)

    bundle = Path(args.bundle).expanduser()
    home = Path(args.home).expanduser() if args.home else Path.home()

    manifest = read_manifest(bundle)
    sessions_path = (bundle / "02_sessions.json"
                     if (bundle / "02_sessions.json").exists()
                     else bundle / "02_session_row.json")
    if not sessions_path.exists():
        raise Abort(f"No session record at {sessions_path}. The bundle is incomplete.")
    try:
        payload = json.loads(sessions_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise Abort(
            f"{sessions_path.name} is not readable JSON: {error}\n"
            f"      The bundle is damaged or incomplete. Copy it again.")
    session_rows = payload if isinstance(payload, list) else [payload]
    try:
        message_rows = load_jsonl(bundle / "01_messages.jsonl")
    except FileNotFoundError:
        raise Abort(
            f"No 01_messages.jsonl in {bundle}. The bundle is incomplete.")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise Abort(
            f"01_messages.jsonl is not readable JSON: {error}\n"
            f"      The bundle is damaged or incomplete. Copy it again.")

    # A half-copied bundle imports without complaint, and the user then reads a
    # conversation that quietly stops partway through. The manifest says how
    # many rows there should be; refuse before writing anything if the file does
    # not hold that many.
    declared = manifest.get("messageRowCount")
    if declared is not None and declared != len(message_rows):
        raise Abort(
            f"The bundle holds {len(message_rows)} messages but its manifest "
            f"says {declared}.\n"
            f"      It was not copied completely. Nothing has been written.\n"
            f"      Copy the folder again — do not import a partial bundle, it "
            f"would import as a conversation that stops partway through.")
    declared_sessions = manifest.get("sessionCount")
    if declared_sessions is not None and declared_sessions != len(session_rows):
        raise Abort(
            f"The bundle holds {len(session_rows)} conversation records but its "
            f"manifest says {declared_sessions}.\n"
            f"      It was not copied completely. Nothing has been written.")

    # Default to the project's own name so pressing Enter lands the files in
    # something recognisable, not a folder called "Project".
    workspace_name = (manifest.get("sourceWorkspaceName")
                      or DEFAULT_WORKSPACE_NAME)
    default_workspace = default_desktop(home) / workspace_name
    # Falling back to the home directory because this machine has no Desktop
    # is fine, but dropping a project folder into the home root without
    # saying so leaves the user hunting for it later.
    fell_back_to_home = False  # set once the real workspace is known

    will_ask = sys.stdin.isatty() and not args.workspace
    print("=" * 70)
    print("  MiniMax Code session restore")
    print("=" * 70)
    if will_ask:
        print(f"  {manifest.get('sessionCount', len(session_rows))} conversation(s), "
              f"{manifest.get('messageRowCount', 0)} messages, "
              f"{manifest.get('attachmentCount', 0)} attachments")
        print("  Press Enter to accept the default.")
        print()

    workspace = (Path(args.workspace).expanduser() if args.workspace
                 else ask("Where should the project files go?", default_workspace))
    # The data directory is always <home>/.minimax for a given user, so it is
    # never asked about. It still matters: it is the prefix the stored
    # attachment paths get rewritten onto.
    data_dir = (Path(args.data_dir).expanduser() if args.data_dir
                else home / DATA_DIR_NAME)

    # Both roots move: attachments under the data directory, and the project's
    # own files, which the delivered-file cards in the conversation point at.
    # A bundle can record more than one data directory when the attachment files
    # were not under the one that was exported, and every one of them has to be
    # swapped for the local one or those links stay dead.
    for value in (manifest.get("sourceAssetsDirs") or []):
        if not isinstance(value, str) or value in ("/", "//") or len(value) < 4:
            raise Abort(
                f"The manifest's sourceAssetsDirs contains {value!r}, which "
                f"would rewrite every stored path.\n"
                f"      Nothing has been written. The bundle is malformed or "
                f"hostile.")
    source_data_dirs = [d for d in
                        [manifest.get("sourceDataDir")]
                        + list(manifest.get("sourceAssetsDirs") or [])
                        if d and d != str(data_dir)]
    # sourceAssetsDirs feeds the same replacements list, so it needs the same
    # guard: "/" there rewrites every slash in every stored path.
    for key in ("sourceDataDir", "sourceWorkspaceDir"):
        value = manifest.get(key) or ""
        if value in ("/", "//") or len(value) < 4:
            raise Abort(
                f"The bundle's {key} is {value!r}, which would rewrite every "
                f"stored path.\n"
                f"      Nothing has been written. The bundle is malformed or "
                f"hostile.")
    replacements = sorted(
        [(d, str(data_dir)) for d in source_data_dirs]
        + [(manifest.get("sourceWorkspaceDir"), str(workspace))],
        key=lambda pair: -len(pair[0] or ""))
    remapped = rewrite_paths(message_rows, replacements)
    moved_workspace = manifest.get("sourceWorkspaceDir") not in (None, str(workspace))
    # What is still pointing at the old machine? Nothing else reports this,
    # and it is the only way a dead link ever becomes visible: the rewriter
    # works on a field allowlist, so a path in any other field survives and
    # the user clicks a dead card with no error anywhere.
    stale = count_stale_paths(message_rows, source_data_dirs,
                              manifest.get("sourceWorkspaceDir"), data_dir,
                              workspace)

    print(f"  Sessions: {len(session_rows)}")
    for row in session_rows[:6]:
        print(f"            {clean_title(row.get('title'))[:56]}")
    if len(session_rows) > 6:
        print(f"            ... and {len(session_rows) - 6} more")
    print(f"  Messages: {len(message_rows)}"
          + (f"  (truncated at id={manifest.get('truncatedToMessageId')})"
             if manifest.get("truncated") else ""))
    print(f"  Attach  : {manifest.get('attachmentCount', 0)}")
    if remapped:
        print(f"            {remapped} message(s) repointed from "
              + (f"{manifest.get('sourceDataDir')}" if manifest.get("sourceDataDir")
                 != str(data_dir) else "the source data directory")
              + (f" and {manifest.get('sourceWorkspaceDir')}" if moved_workspace
                 else ""))
    print(f"  Project : {workspace}")
    # A bare name resolves against the bundle folder the user is standing in,
    # and "~" resolves to the home root, so the project can end up loose in
    # either. Both are things a person types without thinking.
    if Path(workspace).resolve() == Path(home).resolve():
        print("            that is your home folder itself. The project files "
              "would be scattered in it")
        print("            rather than in a folder of their own — type a path "
              "with a folder name in it.")
    elif len(Path(workspace).parts) <= len(Path(home).resolve().parts) + 1:
        print("            (this is close to your home folder, which usually "
              "means the machine has")
        print("             no Desktop — check this is where you meant it)")
    if moved_workspace:
        print(f"            (was {manifest.get('sourceWorkspaceDir')})")
    if stale["total"]:
        # Not fatal -- the conversation is usable and most such references are
        # prose -- but silence here is why dead links ship unnoticed.
        print(f"            {stale['total']} reference(s) still point at the old "
              f"machine and will not open here.")
        for name in stale["examples"]:
            print(f"              e.g. something under {name}")
        print("            They live in fields this importer does not rewrite, or "
              "in files")
        print("            that were never in the bundle. Nothing else is "
              "affected.")
    print(f"  Data dir: {data_dir}")
    print()

    db_path = data_dir / DB_REL
    if not db_path.exists():
        raise Abort(
            f"No MiniMax Code session database at {db_path}.\n"
            f"      Install and launch MiniMax Code on this machine first, "
            f"or pass --data-dir."
        )

    # The manifest says which folder holds the project snapshot; trusting
    # --project-subdir instead would install something the verifier never saw.
    project_subdir = (manifest.get("projectSnapshotDir")
                      or args.project_subdir)
    # The manifest is bundle input, so this value is not trusted to be a
    # relative name: an absolute path or "../" would make the importer read
    # arbitrary directories off this machine into the project folder.
    if (Path(project_subdir).is_absolute()
            or any(part == ".." for part in Path(project_subdir).parts)):
        raise Abort(
            f"The manifest's projectSnapshotDir is {project_subdir!r}, which "
            f"points outside the bundle.\n"
            f"      Nothing has been written. This bundle is malformed or "
            f"hostile.")
    unparked = preflight_bundle(bundle, project_subdir)
    # The destination gets the same link check as the bundle, and it has to
    # run here: contained_by() refuses a link at the target path, but that is
    # reached in step 3, after the database was already committed in step 2.
    # Aborting there while claiming "nothing has been written" is a lie, and
    # the user trusts it enough to not restore the backup.
    preflight_destination(bundle, data_dir, args.dry_run)
    if not args.dry_run:
        # Before the project files move, not after: see the docstring.
        probe_database_writable(db_path)
        probe_fts5(db_path)
        preflight_versions(db_path, manifest)
        # One import at a time. The database serialises itself; the
        # attachment files do not, and a mismatch between two concurrent
        # copies is otherwise reported as a damaged bundle when the bundle
        # is perfectly fine. Released on every normal exit, and a killed run
        # leaves a pid that the next run recognises as dead.
        ImportLock(data_dir).__enter__()

    copied, replaced_files = deploy_project(
        bundle / project_subdir, workspace, args.dry_run)
    print(f"[1/4] Project files -> {workspace}  ({copied} files)")
    if replaced_files:
        # A project folder on the new machine may already hold work. Memory
        # files are backed up; these were not, and the user is the only one
        # who can still have the only copy.
        print(f"      WARNING: {len(replaced_files)} file(s) already existed in "
              f"that folder and were")
        print(f"      overwritten by the bundle's copy. Anything you had edited "
              f"locally is gone:")
        for name in replaced_files[:5]:
            print(f"        {name}")
        if len(replaced_files) > 5:
            print(f"        ... and {len(replaced_files) - 5} more")
    # The receiver never sees the sender's terminal, so the manifest is the
    # only place this can reach them. It is the difference between "my
    # project's source is on the other machine" and a belief.
    left = manifest.get("projectSubdirectoriesLeftBehind") or []
    if left:
        print(f"      WARNING: {len(left)} project subdirector"
              f"{'y was' if len(left) == 1 else 'ies were'} left out of this "
              f"bundle and are NOT on this machine: "
              + ", ".join(str(n) for n in left[:5])
              + (f" (+{len(left) - 5} more)" if len(left) > 5 else ""))
        print("      Ask the sender for those folders, or re-export with "
              "--project-recursive.")
    print("      (the conversation itself is written next)")

    if args.dry_run:
        print("[2/4] Session + project write  (skipped, --dry-run)")
        print("[3/4] Attachments               (skipped, --dry-run)")
        if unparked:
            # The real run says this at [3/4], which a rehearsal skips.
            print(f"      note: {unparked} attachment(s) came from outside the "
                  f"old data directory, so their links cannot be repaired")
        print("[4/4] Verification             (skipped, --dry-run)")
        print()
        print("Rehearsal complete. Drop --dry-run to perform the import.")
        return 0

    # Decided before the write, not after. import_sessions() commits, so
    # clearing these afterwards only mutated a dict nobody read again: the
    # conversation landed pointing at a directory that was never bundled.
    # With several conversations only the one whose directory actually
    # travelled keeps its reference; the rest are cleared here.
    # The manifest is a claim, not evidence. A partially copied bundle can
    # say historyIncluded and not carry the directory, which produces exactly
    # the dangling reference this exists to prevent, so look for the folder.
    history_present = bool(manifest.get("historyIncluded")
                           and (bundle / "05_history_dir").is_dir())
    if manifest.get("historyIncluded") and not history_present:
        print("      runtime history: the manifest says it was bundled, but "
              f"{bundle / '05_history_dir'}")
        print("      is not in this bundle. References cleared rather than "
              "left dangling.")
    bundled_history = (manifest.get("historyRelativeDir")
                       if history_present else None)
    dangling = 0
    for row in session_rows:
        reference = row.get("history_relative_dir")
        if reference is not None and reference != bundled_history:
            row["history_relative_dir"] = None
            dangling += 1

    print("[2/4] Writing session and project")
    written, replaced, rows_written, written_ids = import_sessions(
        db_path, manifest, session_rows, message_rows, workspace)
    print(f"      {written} conversation(s) written, {rows_written} message(s), "
          f"{written} search index entr(ies)"
          + (f"; {replaced} existing one(s) replaced" if replaced else ""))

    history_dirs = 0
    if manifest.get("historyIncluded"):
        history_dirs = restore_history_dir(bundle, data_dir)
        if history_dirs:
            print(f"      runtime history: {history_dirs} item(s) "
                  f"restored under {data_dir / RUNTIME_SESSIONS_REL}")
    if dangling:
        # Already cleared on the rows above, before they were written.
        print(f"      runtime history: not bundled for {dangling} of the "
              f"conversation(s), so their reference was cleared")

    print("[3/4] Restoring attachments")
    index_path = bundle / "08_attachments.json"
    if index_path.is_file():
        try:
            if not isinstance(json.loads(index_path.read_text(encoding="utf-8")), dict):
                raise ValueError("not a JSON object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            # Checked before anything is written: a damaged index used to crash
            # at this point, leaving the database already updated.
            raise Abort(
                f"08_attachments.json is not readable JSON: {error}\n"
                f"      Nothing has been written. Copy the bundle again.")
    restored, missing, total, parked = restore_attachments(bundle, data_dir)
    print(f"      {restored} restored ({total / 1e6:.1f} MB) -> {data_dir / ASSETS_REL}")
    if parked:
        print(f"      {len(parked)} of those came from outside the old data "
              f"directory, so the conversation's own links to them still point "
              f"nowhere:")
        for name, dest in parked[:3]:
            print(f"        {name} -> {dest}")
        if len(parked) > 3:
            print(f"        ... and {len(parked) - 3} more")
        print(f"      The content is under "
              f"{data_dir / ASSETS_REL / 'unmapped'}")
        print("      Open those from that folder. Re-copying the bundle will "
              "not change this.")
    if missing:
        print(f"      {missing} could not be restored (missing or unmapped)")

    mem_files, mem_bytes, mem_backups = restore_memory(bundle, data_dir)
    if mem_files:
        print(f"      memory: {mem_files} file(s) restored "
              f"({mem_bytes / 1000:.1f} KB) -> {data_dir / MEMORY_SUBDIR}")
        for backup in mem_backups:
            print(f"        the version already here was kept as "
                  f"{backup.name}")
        if manifest.get("truncated"):
            # The sender's memory is their profile as it stands today, not as
            # it stood at the cut. Saying so here is the difference between
            # "this is the state at that point" and "this is what they know".
            print("        note: the conversation was cut, but memory is the "
                  "sender's profile as of")
            print("        today, so it may describe work that happened after "
                  "the cut point.")
    elif manifest.get("memoryIncluded"):
        print("      memory: the manifest says it was bundled, but "
              f"{bundle / MEMORY_SUBDIR} is missing")

    digest_dest = restore_context_digest(bundle, workspace)
    if digest_dest:
        print(f"      context: compaction summary placed at {digest_dest}")
        print("        Read it if the conversation seems to be missing early "
              "context.")
    elif manifest.get("contextDigestIncluded"):
        print("      context: the manifest says a summary was bundled, but "
              f"{bundle / CONTEXT_DIGEST_NAME} is missing")

    print("[4/4] Checking the attachments actually open")
    # Audit what was actually written, not what the manifest claims: a shifted
    # sessionIds would otherwise make this pass on a broken bundle.
    total_paths, missing_paths, wrong_size, size_checked, size_note = (
        audit_attachments(db_path, written_ids or [], bundle, data_dir))
    if size_note and total_paths:
        print(f"      [NOTE] {size_note}")
    broken_count = len(missing_paths) + len(wrong_size)
    if not total_paths:
        print("      no attachments referenced")
    elif broken_count:
        if missing_paths:
            print(f"      [PROBLEM] {len(missing_paths)}/{total_paths} attachment "
                  f"files are not on this machine")
            for path in missing_paths[:3]:
                print(f"        absent: {path}")
            if len(missing_paths) > 3:
                print(f"        ... and {len(missing_paths) - 3} more")
        if wrong_size:
            # "Incomplete" would be wrong for a file larger than recorded, and
            # a rounded MB figure hides a small difference behind two
            # identical numbers. State the exact gap.
            print(f"      [PROBLEM] {len(wrong_size)}/{total_paths} attachment "
                  f"files do not match the size the bundle recorded")
            for path, got, want in wrong_size[:3]:
                target = Path(path)
                print(f"        {target.parent.name}/{target.name}: "
                      f"{got:,} bytes here, {want:,} expected"
                      f" ({abs(want - got):,} off)")
            if len(wrong_size) > 3:
                print(f"        ... and {len(wrong_size) - 3} more")
        print()
        print("  The conversation was imported, but those files are damaged or")
        print("  were not copied whole, so some of them may fail to open.")
        print("  Re-copy the whole bundle folder from the other machine, then")
        print("  run the import again. An interrupted copy is the usual cause.")
    else:
        if size_checked:
            print(f"      {size_checked}/{total_paths} attachment files present "
                  f"and the right size")
        else:
            print(f"      {total_paths}/{total_paths} attachment files are "
                  f"present, but nothing verified they are complete")
    # [3/4] counts what the bundle carried, [4/4] counts what this
    # conversation actually points at. After a truncation those differ, and
    # an unexplained gap reads as a failure.
    if total_paths and restored != total_paths:
        print(f"      (the bundle carried {restored} attachment file(s); this "
              f"conversation references {total_paths})")

    cards, cards_broken_count, card_examples = audit_file_cards(
        db_path, written_ids or [])
    if cards:
        print(f"      {cards - cards_broken_count}/{cards} "
              f"delivered-file links resolve")
        if cards_broken_count:
            print(f"      [NOTE] {cards_broken_count} file(s) in the conversation "
                  f"were produced outside the project and are not in the bundle:")
            for path in card_examples[:3]:
                print(f"        missing: {path}")
            if cards_broken_count > 3:
                print(f"        ... and {cards_broken_count - 3} more")
            print("      Copy them over by hand, or click through them on the old")
            print("      machine and re-save. Nothing else is affected.")

    print()
    print("=" * 70)
    # A check that could not run is not the same as a check that passed.
    # "Import complete" plus exit 0 would tell a calling script the
    # attachments were verified when nothing compared them.
    if broken_count:
        banner = "  Import complete  (with problems)"
    elif size_note and total_paths and not size_checked:
        banner = "  Import complete  (attachment sizes not verified)"
    else:
        banner = "  Import complete"
    print(banner)
    print("=" * 70)
    print()
    print("  Where things landed:")
    print(f"    project files  -> {workspace}")
    print(f"    conversations  -> session list in MiniMax Code "
          f"({written} added"
          + (f", {replaced} replaced)" if replaced else ")"))
    if manifest.get("attachmentCount", 0):
        print(f"    attachments    -> {data_dir / ASSETS_REL} "
              f"({restored} files, {total / 1e6:.0f} MB)")
    if mem_files:
        print(f"    memory         -> {data_dir / MEMORY_SUBDIR} "
              f"({mem_files} file(s), profile and preferences)")
    if digest_dest:
        print(f"    context digest -> {digest_dest} "
              f"(the runtime's own summary of the conversation)")
        print()
        print("  Attachments are deliberately not copied into the project folder:")
        print("  the conversation stores their absolute path under the data")
        print("  directory, so restoring them anywhere else breaks every link.")
    print()
    print("  Next: launch MiniMax Code, refresh the session list, and open the chat.")
    print()
    # Exit 0 has to mean the files were verified, not merely that nobody
    # checked. A bundle that records no sizes gets a non-zero exit so a
    # wrapper script cannot read silence as a clean bill of health.
    unverified = bool(size_note) and total_paths and not size_checked
    return 1 if (broken_count or unverified) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as error:
        print(f"\n[ABORTED] {error}")
        sys.exit(1)
    except sqlite3.OperationalError as error:
        # A schema difference and a locked file both surface as
        # OperationalError, but they need opposite advice: the first means
        # "your MiniMax Code is a different version", the second means
        # "quit the app". Telling someone to quit a closed app wastes their
        # time and hides the real cause, so separate them.
        text = str(error).lower()
        if any(marker in text for marker in
               ("readonly database", "disk i/o error", "no space left",
                "permission denied", "unable to open database file")):
            print(f"\n[ABORTED] The session database could not be written: "
                  f"{error}")
            print("        Nothing has been written. This is a permissions or "
                  "disk-space")
            print("        problem, not a running app — quitting MiniMax Code "
                  "will not")
            print("        help. Check free space and that you can write to "
                  "the data")
            print("        directory, then run this again.")
        elif any(marker in text for marker in
               ("no such table", "no column named", "has no column named",
                "no such column", "no such module")):
            print(f"\n[ABORTED] This machine does not match the bundle: {error}")
            if "no such module" in text:
                print("        This Python's sqlite3 has no FTS5, which the "
                      "import needs")
                print("        for the conversation list to be searchable. Try a "
                      "different")
                print("        interpreter, for example `python3.12`, or install "
                      "one that")
                print("        ships FTS5. Nothing has been changed.")
            else:
                print("        The bundle was written for a different version of "
                  "the app.")
                print("        Update MiniMax Code on this machine to the same "
                      "version you")
                print("        exported from, then run this again. Nothing has "
                      "been changed.")
        else:
            print(f"\n[ABORTED] Database is busy or errored: {error}")
            print("        Quit MiniMax Code completely (including background/tray "
                  "processes) and retry.")
        sys.exit(1)
