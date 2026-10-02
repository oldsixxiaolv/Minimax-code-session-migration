#!/usr/bin/env python3
"""Export a MiniMax Code session into a portable, one-command migration bundle.

Typical use — pick the most recently updated session in a workspace:

    python3 export_bundle.py --workspace my-project

The bundle it writes can be copied to another machine, where

    python3 import_bundle.py

restores the project files, the session, its messages, its search index, and
all of its attachments.

Optional truncation keeps only the conversation up to a chosen message, which
is useful when you want to move the earlier part of a long session but not the
recent back-and-forth:

    python3 export_bundle.py --workspace my-project --keep-through-text "some phrase"

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

DEFAULT_DATA_DIR = Path.home() / ".minimax"

# How many trailing messages a truncation preview shows. Enough to contain the
# exchange that ends the export, not so much that the point is lost in it.
TAIL_MESSAGES = 6
DB_REL = Path("v2/sqlite/runtime-state.sqlite")
ASSETS_REL = Path("v2/assets")
RUNTIME_SESSIONS_REL = Path("v2/sessions")

SESSION_TABLE = "local_runtime_sessions"
MESSAGE_TABLE = "local_runtime_message_rows"
FTS_KEYS_TABLE = "local_runtime_session_fts_keys"
SCHEMA_TABLE = "local_runtime_v2_schema_migrations"

BUNDLE_SUBDIR = "bundle"
PROJECT_SUBDIR = "project_files"
ATTACHMENT_SUBDIR = "attachments"
MEMORY_SUBDIR = "memory"
CONTEXT_DIGEST_NAME = "09_context_digest.jsonl"

SKIP_PROJECT_NAMES = {
    ".venv", "venv", ".git", ".DS_Store", "__pycache__", "node_modules",
}

# Output folders this tool produces. A previous migration bundle sitting inside
# the project would otherwise be swept into the new one — including that
# bundle's own memory/user.md, which the manifest would then not account for.
SKIP_PROJECT_SUFFIXES = ("_migration", "_迁移包", "-migration")


def skip_project_dir(name: str) -> bool:
    return name in SKIP_PROJECT_NAMES or name.endswith(SKIP_PROJECT_SUFFIXES)


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def copy_db_snapshot(src: Path, dest: Path) -> None:
    """Take a consistent snapshot with SQLite's backup API.

    The destination is removed first: SQLite's backup writes *into* an existing
    file rather than replacing it, so a leftover snapshot from a killed or
    out-of-space run would otherwise make every later export into this
    directory fail for good, with an error that never mentions the cause.
    """
    dest.unlink(missing_ok=True)
    # Read-only, always. This runs against the database the app is using, and
    # a plain connect() hands SQLite a read-write handle: on a WAL database
    # that can run recovery, which is a write. Reading somebody's live
    # database must never be able to modify it.
    source = sqlite3.connect("file:" + quote(str(src)) + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dest)
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def dump_rows(conn, table: str, where_sql: str, params: tuple) -> list:
    cursor = conn.execute(f"SELECT * FROM {table} WHERE {where_sql}", params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def message_text(data_json: str) -> str:
    try:
        return json.loads(data_json).get("msg_content", "") or ""
    except (json.JSONDecodeError, TypeError):
        return ""


def default_desktop(home: Path) -> Path:
    """A sensible place to write the bundle, on whichever platform this is.

    ``~/Desktop`` does not exist on Windows, and many Linux setups use another
    name or none at all. Prefer a directory that is actually there.
    """
    for name in ("Desktop", "桌面", "Escritorio"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home


def drop_empty_out(args) -> None:
    """Remove the output directory if this run created it and left it empty.

    The snapshot needs a directory to live in, so a failed cut used to leave
    an empty folder on the user's Desktop that looks like a finished bundle.
    The preview path already did this; the error paths returned before
    reaching it. Never touches a directory that already held something -- that
    could be a complete bundle from an earlier run.
    """
    if not args.out:
        return
    out_path = Path(args.out).expanduser()
    try:
        if out_path.is_dir() and not any(out_path.iterdir()):
            out_path.rmdir()
    except OSError:
        pass


def discard_snapshot(work_dir: Path) -> None:
    """Drop the database snapshot taken for reading.

    It is a full copy of the session database, roughly 65 MB. Leaving it behind
    after a failed or previewed export puts a copy in the data directory (or the
    output directory) for no reason, and on a failed run nobody is looking for
    it.
    """
    shutil.rmtree(work_dir, ignore_errors=True)


def normalise_for_match(text: str) -> str:
    """Strip decoration so a copied passage still matches the original.

    Quoting a sentence back to the assistant drops the markdown it was
    written with — backticks, bold markers, quote characters — and may
    collapse its whitespace. Comparing the raw strings then silently moves
    the cut point to the quote instead of the original, so drop all of that
    before matching.
    """
    for char in "`*_“”‘’\"'「」『』":
        text = text.replace(char, "")
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
def assets_root_for(path: str, data_dir: Path):
    """The ``v2/assets`` root an absolute attachment path belongs to.

    Normally that is simply the data directory being exported. But
    ``--data-dir`` can point at a copy of the install while the attachment
    files still live in the original one, and taking the difference against the
    wrong root silently marks every file unmapped. Fall back to reading the
    root out of the path itself so a bundle is still correct.
    """
    default = data_dir / ASSETS_REL
    try:
        Path(path).relative_to(default)
        return default
    except ValueError:
        pass
    parts = Path(path).parts
    for i in range(len(parts) - 1):
        if parts[i] == "v2" and parts[i + 1] == "assets":
            return Path(*parts[:i + 2])
    return None


def collect_attachments(message_rows: list, data_dir: Path) -> dict:
    """Collect user attachments from each message's ``attachments`` field.

    Do not scrape paths out of ``msg_content``: a long path can be split across
    lines in the stored JSON and the regex silently misses files. And do not
    rely on ``local_runtime_session_assets`` — that table only records files the
    agent produced, not the ones the user uploaded.
    """
    found: dict = {}
    for row in message_rows:
        try:
            payload = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        for item in payload.get("attachments") or []:
            local = item.get("local") or {}
            path = local.get("filePath")
            if not path or path in found:
                continue
            meta = item.get("meta") or {}
            # Relative to the root the file actually lives under. Using a
            # relative base here raises ValueError and every file ends up
            # unmapped.
            root = assets_root_for(path, data_dir)
            try:
                relative = str(Path(path).relative_to(root)) if root else None
            except ValueError:
                relative = None
            if not Path(path).exists():
                # The conversation points at a file this machine no longer has.
                # Record it so the export can say so, rather than shipping a
                # bundle that quietly omits an attachment.
                found[path] = {
                    "messageRowId": row["id"],
                    "fileName": meta.get("fileName") or Path(path).name,
                    "mimeType": None,
                    "declaredSizeBytes": None,
                    "originalPath": path,
                    "relativePath": None,
                    "missingOnDisk": True,
                }
                continue
            found[path] = {
                "messageRowId": row["id"],
                "fileName": meta.get("fileName") or Path(path).name,
                "mimeType": meta.get("mimeType"),
                "declaredSizeBytes": meta.get("sizeBytes"),
                "originalPath": path,
                "relativePath": relative,
            }
    return found


def package_attachments(bundle_dir: Path, attachments: dict) -> dict:
    root = bundle_dir / ATTACHMENT_SUBDIR
    if root.exists():
        shutil.rmtree(root)
    packed, missing, skipped, total = [], [], [], 0
    for path, meta in attachments.items():
        source = Path(path)
        if not source.exists():
            missing.append(meta["fileName"])
            continue
        if meta["relativePath"]:
            dest = root / meta["relativePath"]
        else:
            # No relative path: the file lives outside the assets tree. Park it
            # under its bare name. Path().name alone would not be enough — a
            # fileName of "../../x" would still be joined onto root and write
            # outside the output directory while the export reported success.
            dest = root / "unmapped" / Path(meta["fileName"]).name
        try:
            dest.resolve().relative_to(root.resolve())
        except ValueError:
            skipped.append(meta["fileName"])
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        total += dest.stat().st_size
        meta["actualSizeBytes"] = dest.stat().st_size
        meta["bundledPath"] = str(dest.relative_to(root.parent))
        packed.append(meta["fileName"])
    return {
        "packed": len(packed),
        "missing": len(missing),
        "missingNames": missing,
        "totalBytes": total,
    }


# --------------------------------------------------------------------------- #
# Project snapshot
# --------------------------------------------------------------------------- #
def snapshot_project(project_dir: Path, bundle_dir: Path, recursive: bool = False) -> dict:
    """Copy the project into the bundle.

    By default only top-level files are taken. A project whose content lives in
    subdirectories comes out empty, which is a confusing way to learn this, so
    pass ``--project-recursive`` when the tree is small enough to copy whole.
    """
    target = bundle_dir / PROJECT_SUBDIR
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    copied, total = 0, 0
    linked: list = []
    skipped: list = []
    unreadable: list = []
    if recursive:
        for root, dirs, files in os.walk(project_dir, onerror=unreadable.append):
            dirs[:] = [d for d in dirs if not skip_project_dir(d)]
            rel_root = Path(root).relative_to(project_dir)
            for name in sorted(files):
                if name.startswith("~$") or name == ".DS_Store":
                    continue
                source = Path(root) / name
                dest = target / rel_root / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                copied += 1
                total += dest.stat().st_size
        # os.walk does not follow directory symlinks and says nothing when it
        # cannot read a directory, so both lose files in silence. "Recursive"
        # reads as the safe option; it has to be the truthful one.
        linked, skipped = [], []
        for path in sorted(project_dir.rglob("*")):
            rel = path.relative_to(project_dir)
            if any(skip_project_dir(part) for part in rel.parts[:-1]):
                continue
            if path.is_symlink() and path.is_dir():
                linked.append(str(rel))
            elif skip_project_dir(path.name) and path.is_dir():
                skipped.append(str(rel))
    else:
        for item in sorted(project_dir.iterdir()):
            if skip_project_dir(item.name) or item.name.startswith("~$"):
                continue
            if item.is_dir():
                continue
            shutil.copy2(item, target / item.name)
            copied += 1
            total += (target / item.name).stat().st_size
    # Count what was left behind. Silently dropping a project's subdirectories
    # and then reporting a clean export is how someone hands over a bundle
    # that quietly has no source code in it.
    left_behind = []
    if not recursive and project_dir.is_dir():
        for item in sorted(project_dir.iterdir()):
            if skip_project_dir(item.name) or item.name.startswith("~$"):
                continue
            if item.is_dir():
                left_behind.append(item.name)
    return {"files": copied, "bytes": total, "recursive": recursive,
            "subdirectories_left_behind": left_behind,
            "symlinked_subdirectories": linked if recursive else [],
            "skipped_subdirectories": skipped if recursive else [],
            "unreadable_paths": [str(p) for p in unreadable if recursive]}


# --------------------------------------------------------------------------- #
# Transcript (human readable fallback)
# --------------------------------------------------------------------------- #
def build_transcript(session_rows: list, workspace: str, message_rows: list) -> str:
    lines = [
        "# Conversation transcript (readable copy)",
        "",
        f"- Sessions: {len(session_rows)}",
        f"- Original workspace: `{workspace}`",
        f"- Messages: {len(message_rows)}",
        "",
        "This file is for reading and manual recovery only. To continue the",
        "conversation inside MiniMax Code, use `import_bundle.py`.",
        "",
        "---",
        "",
    ]
    current = None
    for row in message_rows:
        role = row.get("role") or "system"
        stamp = datetime.fromtimestamp(row["created_at_ms"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        content = message_text(row.get("data_json") or "").strip()
        label = {"user": "User", "assistant": "Assistant"}.get(role, role)
        if row.get("session_id") != current:
            current = row.get("session_id")
            lines.extend(["---", "", f"### session {current}", ""])
        lines.append(f"## {label} - {stamp}")
        lines.append("")
        lines.append(content if content else "_(empty)_")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Bundle scaffolding
# --------------------------------------------------------------------------- #
def pack_memory(bundle_dir: Path, data_dir: Path) -> dict:
    """Copy the agent's memory files into the bundle.

    The memory store is plain files under ``<data dir>/memory/`` — currently a
    single ``user.md`` holding the profile and the working preferences. It is
    included by default, so the destination keeps the same profile; pass
    ``--no-memory`` to leave it out when a bundle is going somewhere the user's
    profile should not follow it. Only ``.md`` files are taken;
    ``tracking/`` is per-session bookkeeping about whether memory was written and
    means nothing on the destination.

    Returns a summary for the manifest.
    """
    source_dir = data_dir / MEMORY_SUBDIR
    dest_dir = bundle_dir / MEMORY_SUBDIR
    # Clear first either way. Re-exporting into a directory that already has a
    # memory folder, with memory now switched off, would otherwise leave the
    # previous run's files on disk contradicting the manifest.
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    if not source_dir.is_dir():
        return {"included": False, "files": [], "bytes": 0}
    files, total = [], 0
    for item in sorted(source_dir.rglob("*.md")):
        if not item.is_file():
            continue
        dest = dest_dir / item.relative_to(source_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        files.append(str(item.relative_to(source_dir)))
        total += dest.stat().st_size
    if not files:
        shutil.rmtree(dest_dir, ignore_errors=True)
    return {"included": bool(files), "files": files, "bytes": total}


def collect_context_digest(bundle_dir: Path, data_dir: Path,
                            history_rels: list) -> dict:
    """Save the runtime's own compaction summaries alongside the messages.

    When a conversation grows long the runtime compresses older material into a
    ``compactionSummary``. Those summaries are not message rows, so they live
    only in the checksum-locked history directory — which a truncated export
    has to skip entirely. Skipping it is correct for the raw messages but drops
    this: the summary frequently covers research context from *before* the cut,
    because the compaction happened after it.

    So lift just the summaries out into a plain file the importer can restore.
    They are a digest, not a copy: they overlap the messages and may also
    mention material from after the cut, which is why they are labelled rather
    than merged in.

    Returns a summary for the manifest.
    """
    sessions_dir = data_dir / RUNTIME_SESSIONS_REL
    dest = bundle_dir / CONTEXT_DIGEST_NAME
    # Same reasoning as the memory folder: a re-export that finds no summary
    # must not leave the previous run's file behind.
    if dest.exists():
        dest.unlink()
    found = []
    for rel in history_rels:
        source = sessions_dir / rel / "messages.jsonl"
        if not source.is_file():
            continue
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = entry.get("message") or {}
            if message.get("role") != "compactionSummary":
                continue
            found.append({"turnId": entry.get("turn_id"),
                          "summary": message.get("summary", "")})
    if not found:
        return {"included": False, "count": 0, "bytes": 0}
    dest.write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in found) + "\n",
        encoding="utf-8")
    return {"included": True, "count": len(found),
            "bytes": dest.stat().st_size}


def stage_runtime_files(bundle_dir: Path) -> None:
    """Copy the standalone importer and the bundle README into the bundle.

    The importer is the only file the recipient runs. Anything left at the top
    level by an earlier export is removed first, so the folder never advertises
    a script this version stopped shipping.
    """
    here = Path(__file__).resolve().parent
    for stale in sorted(bundle_dir.iterdir()) if bundle_dir.is_dir() else []:
        if stale.is_file():
            stale.unlink()
    shutil.copy2(here / "import_bundle.py", bundle_dir / "import_bundle.py")
    readme = here.parent / "assets" / "BUNDLE_README.md"
    if readme.exists():
        shutil.copy2(readme, bundle_dir / "README.md")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def select_sessions(conn, session_id: str | None, workspace: str | None,
                    show_all: bool = False):
    """Decide which sessions go into the bundle.

    --workspace alone means "every session in that project"; adding --session-id
    narrows it to one. Neither given is ambiguous enough to guess at, so we list
    what is available and stop rather than silently bundling someone's entire
    history.
    """
    if session_id:
        row = conn.execute(
            f"SELECT session_id, workspace_dir, title FROM {SESSION_TABLE} "
            "WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            print(f"[error] No session with id {session_id}.", file=sys.stderr)
            return None
        if workspace and workspace not in (row["workspace_dir"] or ""):
            print(f"      note: that session lives in {row['workspace_dir']}, "
                  f"which does not contain '{workspace}' — using it anyway")
        return [session_id]

    if not workspace:
        print("[error] Pass --workspace <project name> to bundle a whole project, "
              "or --session-id <ID> for a single conversation.\n"
              "        (nothing given would mean every session on this machine)",
              file=sys.stderr)
        # Group by project and print the flag to copy, because the first thing
        # a newcomer needs is the exact argument, not a list of home paths.
        rows = conn.execute(
            f"SELECT session_id, title, workspace_dir FROM {SESSION_TABLE} "
            f"ORDER BY updated_at_ms DESC").fetchall()
        by_project: dict = {}
        for row in rows:
            by_project.setdefault(row["workspace_dir"], []).append(row)

        print("\n  Projects on this machine — pass one with --workspace, "
              "or a single id with --session-id:", file=sys.stderr)
        for workspace, items in list(by_project.items())[:12]:
            name = Path(workspace).name or workspace
            newest = items[0]
            shown = (newest["title"] or "(untitled)").replace("\n", " ")
            shown = shown.replace("</title>", "").strip()
            print(f"\n    --workspace {name!r}    "
                  f"({len(items)} conversation"
                  f"{'s' if len(items) != 1 else ''})", file=sys.stderr)
            per_project = 100 if show_all else 3
            for row in items[:per_project]:
                single = (row["title"] or "(untitled)").replace("\n", " ")
                print(f"        {row['session_id']}  {single.replace('</title>', '')[:44]}",
                      file=sys.stderr)
            if len(items) > per_project:
                # Only the three most recent are shown, and a project with
                # dozens of conversations hides the one you want. Say how to
                # see the rest rather than leaving the user to grep the
                # database themselves.
                print(f"        ... and {len(items) - per_project} more "
                      f"(add --list to see every conversation)",
                      file=sys.stderr)
        if len(by_project) > 12:
            print(f"\n    ... and {len(by_project) - 12} more projects",
                  file=sys.stderr)
        return None

    candidates = conn.execute(
        f"SELECT session_id, workspace_dir FROM {SESSION_TABLE} "
        "WHERE workspace_dir LIKE ? OR project_workspace_dir LIKE ?",
        (f"%{workspace}%", f"%{workspace}%")).fetchall()
    if not candidates:
        print(f"[error] No session under a workspace containing '{workspace}'.",
              file=sys.stderr)
        return None

    # A LIKE match can catch neighbouring folders (my-project2). Keep only the
    # sessions that actually share the most common exact workspace path.
    tally: dict = {}
    for row in candidates:
        tally[row["workspace_dir"]] = tally.get(row["workspace_dir"], 0) + 1
    exact = max(tally, key=lambda k: (tally[k], k))
    ids = [r["session_id"] for r in candidates if r["workspace_dir"] == exact]
    if len(tally) > 1:
        print(f"      note: '{workspace}' also matched {len(tally) - 1} other "
              f"workspace(s); using {exact}")
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a MiniMax Code session as a portable migration bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", dest="list_all", action="store_true",
                        help="list every conversation on this machine, not "
                             "just the three most recent per project, and exit")
    parser.add_argument("--session-id", default=None,
                        help="a single conversation by id; without it, "
                             "--workspace alone exports every conversation it "
                             "matches")
    parser.add_argument("--workspace", default=None,
                        help="the project to bundle, matched as a substring of the "
                             "workspace folder path; takes every conversation in it")
    parser.add_argument("--project-dir", default=None,
                        help="directory to snapshot as the project "
                             "(default: the session's own workspace_dir)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help="MiniMax Code data directory (default: ~/.minimax)")
    parser.add_argument("--project-recursive", action="store_true",
                        help="also include subfolders. Without it only top-level "
                             "files are bundled, so a project whose content "
                             "lives in directories loses it")
    parser.add_argument("--out", default=None,
                        help="bundle output directory (default: the "
                             "workspace name plus _migration, on your Desktop "
                             "or equivalent)")
    parser.add_argument("--keep-through-id", type=int, default=None,
                        help="keep messages up to and including this message row id")
    parser.add_argument("--keep-through-text", default=None,
                        help="keep the conversation up to and including the first "
                             "message containing this text. Pass a whole, "
                             "unambiguous paragraph — a short phrase can match an "
                             "earlier message and cut the conversation short")
    parser.add_argument("--keep-through-time", default=None,
                        help="keep messages up to and including this local time, "
                             "e.g. '2026-09-28 10:00'. Applies to every "
                             "conversation in the bundle")
    parser.add_argument("--yes", action="store_true",
                        help="actually write a truncated bundle; without it the "
                             "exporter shows the last message that would be kept "
                             "and writes nothing")
    parser.add_argument("--no-memory", action="store_true",
                        help="leave the agent's memory files "
                             "(~/.minimax/memory/*.md) out of the bundle. They "
                             "are included by default so the destination keeps "
                             "the same profile and preferences; pass this only "
                             "if the bundle is going somewhere you would not "
                             "want the user's profile in")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    live_db = data_dir / DB_REL
    if not live_db.exists():
        print(f"[error] No session database at {live_db}", file=sys.stderr)
        return 1

    work_dir = Path(args.out).expanduser() / "_work" if args.out else \
        data_dir / "v2/sqlite/_export_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot = work_dir / "snapshot.sqlite"

    # Refuse before copying the database: with no arguments the run always
    # stops, and making a full snapshot first wasted 70 MB and printed a
    # progress line that looked like the export had started.
    if not args.session_id and not args.workspace:
        # Quoted for the same reason the importer quotes its path: an
        # unescaped '#' or '?' in the path truncates the URI, and the truncated
        # path can be a *different, valid* database — which this would then
        # export, silently, with exit 0.
        conn = sqlite3.connect(
            "file:" + quote(str(live_db)) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        select_sessions(conn, None, None, show_all=args.list_all)
        conn.close()
        discard_snapshot(work_dir)
        return 1

    # Everything from here on runs under a finally: an unhandled error
    # must not leave a full copy of the session database in the output
    # directory the user was told to copy to another machine.
    try:
        print(f"[1/6] Snapshotting session database "
              f"({live_db.stat().st_size / 1e6:.1f} MB)")
        copy_db_snapshot(live_db, snapshot)
        conn = sqlite3.connect(snapshot)
        conn.row_factory = sqlite3.Row

        print("[2/6] Selecting conversations")
        session_ids = select_sessions(conn, args.session_id, args.workspace,
                                       show_all=args.list_all)
        if session_ids is None:
            conn.close()
            discard_snapshot(work_dir)
            return 1

        session_rows = dump_rows(
            conn, SESSION_TABLE,
            f"session_id IN ({','.join('?' * len(session_ids))})", tuple(session_ids))
        order = {sid: i for i, sid in enumerate(session_ids)}
        session_rows.sort(key=lambda r: order.get(r["session_id"], 0))

        workspace_dir = session_rows[0]["workspace_dir"]
        if len(session_rows) > 1:
            others = {r["workspace_dir"] for r in session_rows}
            if len(others) > 1:
                print(f"      WARNING: sessions span {len(others)} workspaces; keeping only "
                      f"those in {workspace_dir}")
                session_rows = [r for r in session_rows if r["workspace_dir"] == workspace_dir]
                session_ids = [r["session_id"] for r in session_rows]
        # Captured before any truncation clears the field, because it is the only
        # pointer to the runtime history directory, and that directory is where the
        # compaction summaries live.
        original_history_rels = [r["history_relative_dir"] for r in session_rows
                                 if r.get("history_relative_dir")]

        def label(raw):
            return (raw or "(untitled)").replace("\n", " ").replace("</title>", "").strip()

        title = label(session_rows[0]["title"])
        print(f"      selected {len(session_rows)} session(s) in {workspace_dir}")
        for row in session_rows:
            stamp = datetime.fromtimestamp(row["updated_at_ms"] / 1000)
            print(f"        {row['session_id'][:20]}…  {stamp:%Y-%m-%d}  "
                  f"{label(row['title'])[:46]}")

        if args.out:
            out_dir = Path(args.out).expanduser()
        else:
            stem = Path(workspace_dir or "project").name or "project"
            out_dir = default_desktop(Path.home()) / f"{stem}_migration"

        placeholders = ",".join("?" * len(session_ids))
        message_rows = dump_rows(
            conn, MESSAGE_TABLE, f"session_id IN ({placeholders})", tuple(session_ids))
        message_rows.sort(key=lambda r: r["id"])
        total_before = len(message_rows)
        truncated = False

        if (args.keep_through_id is not None or args.keep_through_text
                or args.keep_through_time):
            anchor_row = None
            if args.keep_through_time:
                when = args.keep_through_time.replace("T", " ")
                cutoff_ms = None
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        cutoff_ms = int(datetime.strptime(when, fmt).timestamp() * 1000)
                        break
                    except ValueError:
                        continue
                if cutoff_ms is None:
                    print(f"[error] Cannot read the time: {args.keep_through_time}. "
                          f"Use 'YYYY-MM-DD HH:MM'.", file=sys.stderr)
                    conn.close()
                    discard_snapshot(work_dir)
                    drop_empty_out(args)
                    return 1
                # "Through 08:22" reads as the whole of 08:22, so the
                # minute the user named is included rather than cut at :00.
                if len(when) == 16 and when.endswith(("00", "05", "10", "15",
                                                      "20", "25", "30", "35",
                                                      "40", "45", "50", "55")) \
                        and when.count(":") == 1:
                    cutoff_ms += 59_999
                at_or_before = [r["id"] for r in message_rows
                                if r["created_at_ms"] <= cutoff_ms]
                anchor_row = None
                if not at_or_before:
                    print(f"[error] No message at or before {when} (local time).",
                          file=sys.stderr)
                    conn.close()
                    discard_snapshot(work_dir)
                    drop_empty_out(args)
                    return 1
                stamp = datetime.fromtimestamp(cutoff_ms / 1000)
                print(f"      cut at {stamp:%Y-%m-%d %H:%M} local; "
                      f"{len(at_or_before)} message(s) at or before it")
            elif args.keep_through_id is not None:
                anchor = next((r for r in message_rows
                               if r["id"] == args.keep_through_id), None)
                if anchor is None:
                    print(f"[error] No message with id={args.keep_through_id} in "
                          f"this bundle.", file=sys.stderr)
                    conn.close()
                    discard_snapshot(work_dir)
                    drop_empty_out(args)
                    return 1
                cutoff_ms = anchor["created_at_ms"]
                anchor_row = anchor
                stamp = datetime.fromtimestamp(cutoff_ms / 1000)
                print(f"      id={anchor['id']} is {stamp:%Y-%m-%d %H:%M} local; "
                      f"cutting there")
            else:
                needle = args.keep_through_text
                wanted = normalise_for_match(needle)
                hits = [r for r in message_rows
                        if wanted in normalise_for_match(message_text(r["data_json"]))]
                if not hits:
                    print(f"[error] No message contains: {needle}", file=sys.stderr)
                    conn.close()
                    discard_snapshot(work_dir)
                    drop_empty_out(args)
                    return 1
                # A passage quoted back later often loses its backticks or emphasis,
                # which would otherwise move the cut silently. Matching ignores
                # that decoration, and every candidate is listed with a timestamp
                # so a wrong cut is visible instead of silent.
                hits.sort(key=lambda r: (r["created_at_ms"], r["id"]))
                cutoff_ms = hits[0]["created_at_ms"]
                anchor_row = hits[0]
                if len(hits) > 1:
                    print(f"      that text matches {len(hits)} messages:")
                    for r in hits[:8]:
                        stamp = datetime.fromtimestamp(r["created_at_ms"] / 1000)
                        print(f"        id={r['id']:<6} {stamp:%Y-%m-%d %H:%M} "
                              f"({r['role']})")
                    if len(hits) > 8:
                        print(f"        ... and {len(hits) - 8} more")
                    first = hits[0]
                    stamp = datetime.fromtimestamp(first["created_at_ms"] / 1000)
                    print(f"      keeping up to the earliest, id={first['id']} "
                          f"({stamp:%Y-%m-%d %H:%M}). Pass --keep-through-id to "
                          f"choose a different one.")
            # Filter by time, never by id: ids are only monotonic inside one
            # conversation, and a project can hold several branches whose id ranges
            # interleave. Cutting on an id therefore keeps rows from a later point
            # in time, in a different branch, with nothing to indicate it.
            kept = [r for r in message_rows if r["created_at_ms"] <= cutoff_ms]
            if anchor_row is not None:
                # The filter is by time so that a project's branches, whose id
                # ranges interleave, are cut at the same instant. But two
                # messages in the same conversation can share a millisecond,
                # and one of them can be *after* the one the user named --
                # the live session has such a pair, and the extra message
                # travelled into the bundle with no way to notice.
                kept = [r for r in kept
                        if r["session_id"] != anchor_row["session_id"]
                        or r["id"] <= anchor_row["id"]]
            cutoff = max((r["id"] for r in kept), default=0)
            # A conversation with no message at or before the cut simply vanishes.
            # Reporting only a row count would hide that loss.
            kept_sessions = {r["session_id"] for r in kept}
            dropped_sessions = [s for s in session_ids if s not in kept_sessions]
            if dropped_sessions:
                print(f"      WARNING: {len(dropped_sessions)} of "
                      f"{len(session_ids)} conversation(s) have nothing at or "
                      f"before the cut and are not in the bundle.")
                for session_id in dropped_sessions[:4]:
                    print(f"        dropped: {session_id}")
                if len(dropped_sessions) > 4:
                    print(f"        ... and {len(dropped_sessions) - 4} more")
                print("        To move one conversation only, export it with "
                      "--session-id.")
            if not kept:
                print(f"[error] Nothing to keep at cutoff id={cutoff}.", file=sys.stderr)
                conn.close()
                discard_snapshot(work_dir)
                return 1
            last = kept[-1]
            last_stamp = datetime.fromtimestamp(last["created_at_ms"] / 1000)
            print(f"      truncated at id={cutoff} ({last['role']}, {last_stamp:%Y-%m-%d %H:%M:%S})")
            print(f"      keeping {len(kept)}, dropping {total_before - len(kept)}")

            # Truncation is the one decision here that can silently throw away the
            # wrong end of a conversation, so it never happens unattended. Show the
            # tail that would survive — enough to see the question it answers and
            # the reply that ends the export — and stop. The exporter only writes
            # once that has been read and --yes is passed.
            if not args.yes:
                tail = [r for r in kept
                        if message_text(r["data_json"]).strip()][-TAIL_MESSAGES:]
                first_stamp = datetime.fromtimestamp(kept[0]["created_at_ms"] / 1000)
                print()
                print("  " + "=" * 68)
                print(f"  THE LAST {len(tail)} MESSAGES THAT WOULD BE KEPT")
                print(f"  bundle starts {first_stamp:%Y-%m-%d %H:%M}, "
                      f"ends {last_stamp:%Y-%m-%d %H:%M}")
                print("  " + "=" * 68)
                for row in tail:
                    stamp = datetime.fromtimestamp(row["created_at_ms"] / 1000)
                    who = "YOU  " if row["role"] == "user" else "AGENT"
                    tail_mark = "   <-- LAST KEPT" if row["id"] == last["id"] else ""
                    print(f"\n  #{row['id']}  {stamp:%m-%d %H:%M}  {who}{tail_mark}")
                    print("  " + "-" * 68)
                    text = message_text(row["data_json"]).strip()
                    for line in (text.splitlines() or ["(no text)"]):
                        print(f"  | {line}")
                print("\n  " + "=" * 68)
                print(f"  Nothing has been written. Keeping {len(kept)} of "
                      f"{total_before} messages, dropping "
                      f"{total_before - len(kept)}.")
                print("  Read the exchange above. If the cut is where you want it,")
                print("  re-run the same command with --yes. To move it, use")
                print("  --keep-through-id <id> or --keep-through-text <other text>.")
                conn.close()
                # The snapshot above was only needed to read the database. Remove
                # that snapshot and nothing else: work_dir.parent is the --out
                # directory, which may already hold a complete bundle from an
                # earlier run, and deleting it here would destroy it while claiming
                # nothing was written.
                discard_snapshot(work_dir)
                drop_empty_out(args)
                return 0

            message_rows = kept
            session_ids = sorted({r["session_id"] for r in kept}, key=order.get)
            session_rows = [r for r in session_rows if r["session_id"] in set(session_ids)]
            truncated = True
            # The runtime history directory is checksum locked and its message file
            # does not map 1:1 onto the database rows, so a hand-trimmed copy would
            # fail its integrity check. Skip it entirely and drop the reference.
            history_rel = None
            history_present = False
            print("      runtime history directories: skipped (checksums would not match)")
        else:
            history_rels = [r["history_relative_dir"] for r in session_rows
                            if r["history_relative_dir"]]
            history_rel = history_rels[0] if history_rels else None
            history_src = data_dir / RUNTIME_SESSIONS_REL / history_rel if history_rel else None
            history_present = bool(history_src and history_src.exists())

        roles: dict = {}
        for row in message_rows:
            key = row["role"] or "(empty)"
            roles[key] = roles.get(key, 0) + 1
        # Spelled out rather than repr()'d: a raw dict of role names is
        # an internal detail, and "(empty)" is a bucket, not a speaker.
        spoken = {k: v for k, v in roles.items() if v and k != "(empty)"}
        breakdown = ", ".join(f"{v} {k}" for k, v in spoken.items())
        print(f"[3/6] Messages: {len(message_rows)} across "
              f"{len(session_ids)} conversation(s)"
              + (f"  ({breakdown})" if breakdown else ""))

        # Recomputed: truncation can shorten session_ids since `placeholders` was
        # built, and reusing the old string binds fewer values than the statement
        # declares.
        kept_placeholders = ",".join("?" * len(session_ids))
        fts_key_rows = dump_rows(
            conn, FTS_KEYS_TABLE, f"session_id IN ({kept_placeholders})",
            tuple(session_ids))

        if history_rel:
            history_src = data_dir / RUNTIME_SESSIONS_REL / history_rel
            history_present = history_src.exists()
            if history_present:
                size_mb = sum(f.stat().st_size for f in history_src.rglob("*") if f.is_file()) / 1e6
                print(f"[4/6] Runtime history directory: {history_rel} ({size_mb:.1f} MB)")
            else:
                print("[4/6] Runtime history directory not found (optional)")
                # A session row that still names a directory the bundle does not
                # carry is an inconsistent bundle: the destination would point at
                # something that is not there. Drop the reference so the rows say
                # what is actually true, exactly as a truncated export does.
                for row in session_rows:
                    row["history_relative_dir"] = None
                history_rel = None
        elif truncated:
            print("[4/6] Runtime history directory: not included (truncated export)")
        else:
            print("[4/6] Runtime history directory: none recorded for this conversation")

        print(f"[5/6] Writing bundle to {out_dir}")
        bundle_dir = out_dir / BUNDLE_SUBDIR
        bundle_dir.mkdir(parents=True, exist_ok=True)

        (bundle_dir / "01_messages.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in message_rows) + "\n",
            encoding="utf-8",
        )
        (bundle_dir / "06_transcript.md").write_text(
            build_transcript(session_rows, workspace_dir, message_rows), encoding="utf-8"
        )

        history_src = None
        if history_present and history_rel:
            history_src = data_dir / RUNTIME_SESSIONS_REL / history_rel
            dest_hist = bundle_dir / "05_history_dir"
            if dest_hist.exists():
                shutil.rmtree(dest_hist)
            shutil.copytree(history_src, dest_hist)
        elif (bundle_dir / "05_history_dir").exists():
            shutil.rmtree(bundle_dir / "05_history_dir")

        if truncated:
            for row in session_rows:
                row["history_relative_dir"] = None

        (bundle_dir / "02_sessions.json").write_text(
            json.dumps(session_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (bundle_dir / "04_fts_rows.json").write_text(
            json.dumps(fts_key_rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        attachments = collect_attachments(message_rows, data_dir)
        # Where the attachment files actually live, which is not always the data
        # directory that was passed in. The importer needs this to know what to
        # swap, and the verifier needs it to tell a real problem from a copy.
        assets_dirs = sorted({
            str(assets_root_for(meta["originalPath"], data_dir).parents[1])
            for meta in attachments.values()
            if assets_root_for(meta["originalPath"], data_dir)})
        attach_summary = package_attachments(bundle_dir, attachments)
        (bundle_dir / "08_attachments.json").write_text(
            json.dumps(attachments, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"      attachments: {attach_summary['packed']} bundled "
              f"({attach_summary['totalBytes'] / 1e6:.1f} MB)"
              + (f", {attach_summary['missing']} missing"
                 if attach_summary["missing"] else ""))

        # The compaction summary is written across a wider period than the cut, so
        # bundling it in a truncated export is exactly the later material the cut
        # is meant to remove. Same reasoning as the memory file.
        if truncated:
            # Clearing matters as much as skipping: a digest left behind by an
            # earlier run would contradict the manifest and smuggle post-cut
            # material back in.
            stale_digest = bundle_dir / CONTEXT_DIGEST_NAME
            if stale_digest.exists():
                stale_digest.unlink()
        digest_summary = ({"included": False, "count": 0, "bytes": 0} if truncated
                          else collect_context_digest(bundle_dir, data_dir,
                                                      original_history_rels))
        if digest_summary["included"]:
            print(f"      context: {digest_summary['count']} compaction summary(ies) "
                  f"kept ({digest_summary['bytes'] / 1000:.1f} KB) -> "
                  f"{CONTEXT_DIGEST_NAME}")
            print("      NOTE: these summarise the runtime's own view of the whole "
                  "conversation. They")
            print("      overlap the messages and may mention material from after the "
                  "cut.")

        # Memory travels with a truncated bundle. It is a live snapshot and
        # cannot be cut at a point in time, so it may describe work that
        # happened after the cut -- said plainly on both the export and the
        # import rather than left for the receiver to discover. Leaving it out
        # was defensible, but the point of moving a conversation to another
        # machine is to arrive able to keep working, and a bare transcript
        # with no memory is not that.
        memory_suppressed = False
        if args.no_memory:
            stale = bundle_dir / MEMORY_SUBDIR
            if stale.exists():
                shutil.rmtree(stale)
            memory_summary = {"included": False, "files": [], "bytes": 0}
        else:
            memory_summary = pack_memory(bundle_dir, data_dir)
        if memory_summary["included"]:
            print(f"      memory: {len(memory_summary['files'])} file(s) bundled "
                  f"({', '.join(memory_summary['files'])})")
            print("      NOTE: the bundle now contains your profile. It is a private "
                  "handover — do not publish it. Use --no-memory to leave it out.")
            if truncated:
                print("      NOTE: memory is the profile as it stands NOW, not as it "
                      "stood at the cut — it is a")
                print("      live file that cannot be time-travelled, so it may "
                      "describe work done after that point.")
        elif args.no_memory:
            print("      memory: left out (--no-memory)")
        else:
            print("      memory: nothing to bundle")

        # The project to restore is the session's own workspace, not the directory
        # this script happens to live in - inside a skill install those differ.
        project_dir = Path(args.project_dir).expanduser() if args.project_dir \
            else Path(workspace_dir)
        if project_dir.is_dir():
            print(f"      project snapshot from: {project_dir}")
            project_summary = snapshot_project(project_dir, bundle_dir, args.project_recursive)
        else:
            # A missing project directory is not a warning to print and move
            # on from: the whole project is silently absent from the bundle,
            # the export exits 0, and the receiver has no way to tell.
            print(f"      ERROR: project directory does not exist: {project_dir}")
            print("      Nothing will be bundled for the project. Re-run with "
                  "--project-dir")
            print("      pointing at the right folder, or drop it if this "
                  "conversation has no")
            print("      project files worth carrying.")
            return 2
        if truncated:
            print("      NOTE: the project snapshot is the folder as it is NOW, not "
                  "as it was")
            print("      at the cut; files changed after the cut come along as they "
                  "are today.")
        print(f"      project snapshot: {project_summary['files']} files "
              f"({project_summary['bytes'] / 1e3:.0f} KB)"
              + (" [recursive]" if args.project_recursive else " [top-level files only]"))
        left = project_summary.get("subdirectories_left_behind") or []
        if left:
            shown = ", ".join(left[:6]) + (f" (+{len(left) - 6} more)"
                                           if len(left) > 6 else "")
            print(f"      WARNING: {len(left)} subdirector"
                  f"{'y' if len(left) == 1 else 'ies'} did NOT go into the "
                  f"bundle: {shown}")
            print("      Their contents are not on the other machine. Re-run with")
            print("      --project-recursive to include them, or copy those folders")
            print("      across by hand.")
        # Recursive mode is not automatically complete. os.walk skips linked
        # directories and swallows read errors, and the skip list matches on
        # name suffix, so a real folder called `db_migration` is dropped by a
        # rule meant for this tool's own leftovers. Say so every time.
        linked_dirs = project_summary.get("symlinked_subdirectories") or []
        if linked_dirs:
            print(f"      WARNING: {len(linked_dirs)} director"
                  f"{'y is' if len(linked_dirs) == 1 else 'ies are'} a symbolic "
                  f"link and was NOT followed:")
            for name in linked_dirs[:4]:
                print(f"        {name}")
            if len(linked_dirs) > 4:
                print(f"        ... and {len(linked_dirs) - 4} more")
            print("      Copy whatever they point at across by hand.")
        skipped_dirs = project_summary.get("skipped_subdirectories") or []
        if skipped_dirs:
            print(f"      WARNING: {len(skipped_dirs)} director"
                  f"{'y was' if len(skipped_dirs) == 1 else 'ies were'} skipped "
                  f"by name (dependency folders, or names ending in "
                  f"_migration/-migration):")
            for name in skipped_dirs[:4]:
                print(f"        {name}")
            if len(skipped_dirs) > 4:
                print(f"        ... and {len(skipped_dirs) - 4} more")
            print("      If any of those are real work rather than leftovers, "
                  "copy them over yourself.")
        unread = project_summary.get("unreadable_paths") or []
        if unread:
            print(f"      WARNING: {len(unread)} path(s) could not be read and "
                  f"are NOT in the bundle:")
            for name in unread[:4]:
                print(f"        {name}")
            if len(unread) > 4:
                print(f"        ... and {len(unread) - 4} more")

        stage_runtime_files(out_dir)

        schema_version = conn.execute(f"SELECT MAX(version) FROM {SCHEMA_TABLE}").fetchone()[0]
        columnar_versions = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT columnar_version FROM {SESSION_TABLE}").fetchall()
        ]
        conn.close()
        snapshot.unlink(missing_ok=True)
        try:
            work_dir.rmdir()
        except OSError:
            pass

        manifest = {
            "bundleFormat": 1,
            "generator": "minimax-session-transfermation/export_bundle.py",
            "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sourceDataDir": str(data_dir),
            "sourceWorkspaceDir": workspace_dir,
            "sessionIds": session_ids,
            "sessionCount": len(session_ids),
            "sessionTitles": [label(r["title"]) for r in session_rows],
            "sessionTypes": sorted({r["session_type"] for r in session_rows}),
            "runtime": session_rows[0]["runtime"],
            "agentName": session_rows[0]["agent_name"],
            "columnarVersion": session_rows[0]["columnar_version"],
            "schemaVersion": schema_version,
            "historyRelativeDir": history_rel,
            "historyIncluded": bool(history_present and history_rel),
            "truncated": truncated,
            "truncatedToMessageId": message_rows[-1]["id"] if truncated else None,
            "messageRowCount": len(message_rows),
            "messageRowCountBeforeTruncation": total_before,
            "droppedMessageRows": (total_before - len(message_rows)) if truncated else 0,
            "messageRoleCounts": roles,
            "attachmentCount": attach_summary["packed"],
            "attachmentMissingCount": attach_summary["missing"],
            "attachmentTotalBytes": attach_summary["totalBytes"],
            "sourceWorkspaceName": Path(workspace_dir or "").name or "Migrated_Project",
            "projectSnapshotDir": PROJECT_SUBDIR,
            "projectFileCount": project_summary["files"],
            "projectSnapshotRecursive": project_summary.get("recursive", False),
            "projectSubdirectoriesLeftBehind":
                project_summary.get("subdirectories_left_behind", []),
            "projectSymlinkedSubdirectories":
                project_summary.get("symlinked_subdirectories", []),
            "projectSkippedSubdirectories":
                project_summary.get("skipped_subdirectories", []),
            "projectUnreadablePaths":
                project_summary.get("unreadable_paths", []),
            "knownColumnarVersions": columnar_versions,
            "sourceAssetsDirs": assets_dirs,
            "contextDigestIncluded": digest_summary["included"],
            "contextDigestCount": digest_summary["count"],
            "contextDigestBytes": digest_summary["bytes"],
            "memoryIncluded": memory_summary["included"],
            "memoryFiles": memory_summary["files"],
            "memoryBytes": memory_summary["bytes"],
            "excluded": "credentials, auth tokens, provider config, login state, "
                        "plugins, " + ("agent memory"
                                       if not memory_summary["included"]
                                       else "agent memory (bundled)") +
                        ", and sessions outside this project",
        }
        (bundle_dir / "00_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        discard_snapshot(work_dir)
        total_mb = sum(f.stat().st_size for f in out_dir.rglob("*") if f.is_file()) / 1e6
        if attach_summary.get("skipped"):
            print(f"      {len(attach_summary['skipped'])} attachment(s) could not be "
                  f"placed inside the bundle: {attach_summary['skipped'][:3]}")
        if attach_summary["missing"] or attach_summary.get("skipped"):
            print(f"[6/6] Finished WITH PROBLEMS. Bundle at {out_dir}  "
                  f"({total_mb:.1f} MB)")
            print(f"      {attach_summary['missing']} attachment(s) the "
                  f"conversation refers to are no longer on this machine, so they "
                  f"are not in the bundle.")
            print("      The conversation itself is complete. Copy those files "
                  "across by hand if you still have them; nothing else is "
                  "missing.")
            return 1

    finally:
        discard_snapshot(work_dir)
    print(f"[6/6] Done. Bundle at {out_dir}  ({total_mb:.1f} MB)")

    print()
    print("Next: copy the whole bundle folder to the other machine, enter it, and run")
    print("    python3 import_bundle.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
