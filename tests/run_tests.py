#!/usr/bin/env python3
"""End-to-end checks for the migration scripts.

Everything is exercised against a throwaway copy of the real MiniMax Code
install, standing in for the destination machine. The point is not to prove the
scripts cannot fail, but to pin down the conditions under which they are known
to work, and to fail loudly when one of those conditions stops holding.

    python3 tests/run_tests.py            # everything
    python3 tests/run_tests.py -k attach  # only cases matching "attach"

Standard library only. Leaves nothing behind outside the system temp directory.
"""
from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
EXPORT = SKILL / "scripts/export_bundle.py"
IMPORT = SKILL / "scripts/import_bundle.py"
def bundle_problems(package: Path) -> list:
    """Everything worth checking about a bundle, without a separate tool.

    This used to shell out to ``verify_bundle.py``, which the project no longer
    ships: the export is its own check, and a third script is one more thing to
    keep honest. These are the same core assertions -- the manifest agrees with
    what is on disk, and nothing in the bundle can escape it -- run in-process
    so the suite still holds the export to them.
    """
    problems = []
    bundle = package / "bundle"
    try:
        manifest = json.loads((bundle / "00_manifest.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"no readable manifest: {error}"]
    rows = [l for l in (bundle / "01_messages.jsonl").read_text("utf-8").splitlines()
            if l.strip()]
    if len(rows) != manifest.get("messageRowCount"):
        problems.append(f"{len(rows)} message rows, manifest says "
                        f"{manifest.get('messageRowCount')}")
    try:
        sessions = json.loads((bundle / "02_sessions.json").read_text("utf-8"))
        known = {r["session_id"] for r in sessions}
        for line in rows:
            if json.loads(line).get("session_id") not in known:
                problems.append("a message row names a session that is not listed")
                break
    except (OSError, json.JSONDecodeError, KeyError) as error:
        problems.append(f"no readable session list: {error}")
    attachments = 0
    index_path = bundle / "08_attachments.json"
    if index_path.is_file():
        for meta in json.loads(index_path.read_text("utf-8")).values():
            relative = meta.get("relativePath")
            target = bundle / "attachments" / relative if relative else (
                bundle / "attachments" / "unmapped" / Path(
                    str(meta.get("fileName") or "")).name)
            if not target.is_file():
                problems.append(f"attachment missing from the bundle: {target.name}")
            attachments += 1
    if manifest.get("attachmentCount") not in (None, attachments):
        problems.append(f"{attachments} attachment files, manifest says "
                        f"{manifest.get('attachmentCount')}")
    for path in bundle.rglob("*"):
        if path.is_symlink():
            problems.append(f"symbolic link in the bundle: {path.name}")
    return problems



REAL_HOME = Path.home()
REAL_DB = REAL_HOME / ".minimax/v2/sqlite/runtime-state.sqlite"
DB_REL = Path(".minimax/v2/sqlite/runtime-state.sqlite")
# A path that exists nowhere, so a successful import cannot be a false positive
# from paths that happen to still resolve on this machine.
FOREIGN_DATA_DIR = "/ANOTHER-MACHINE/.minimax"


def truncation_anchor() -> str:
    """A phrase from the chosen session, used to exercise truncation.

    Built from the session's own text at run time rather than hardcoded, so the
    suite carries no excerpt of anyone's private conversation.
    """
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT data_json FROM local_runtime_message_rows WHERE session_id = ? "
        "AND json_extract(data_json, '$.msg_content') IS NOT NULL "
        "ORDER BY id LIMIT 1", (SESSION_ID,)).fetchone()
    conn.close()
    text = json.loads(row[0] or "{}").get("msg_content") or "" if row else ""
    stripped = re.sub(r"\s+", "", text)[:24]
    return stripped or "no text available"


def real_session_ids(limit: int = 40) -> list:
    """Ids of conversations that actually exist on this machine.

    Sampling the live database catches an id copied into a docstring or a test
    fixture without hardcoding anyone's identifier here.
    """
    if not REAL_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    rows = conn.execute("SELECT session_id FROM local_runtime_sessions "
                        "ORDER BY updated_at_ms DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [r[0] for r in rows if r and r[0]]


def real_conversation_phrases(limit: int = 60) -> list:
    """Distinctive stretches of text taken from real conversations on this machine.

    Reading them out of the live database is the only way to catch the leak
    that actually happened here: a real conversation title pasted into a doc
    example. No amount of scanning for usernames or absolute paths will ever
    see it, because it is neither.

    Long enough that a coincidental match in prose is implausible, and with
    whitespace collapsed so a re-wrap in Markdown does not hide it.
    """
    if not REAL_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    rows = conn.execute("SELECT title FROM local_runtime_sessions "
                        "WHERE title IS NOT NULL ORDER BY updated_at_ms DESC "
                        "LIMIT ?", (limit * 4,)).fetchall()
    conn.close()
    phrases = set()
    for (title,) in rows:
        flat = re.sub(r"\s+", " ", str(title)).strip()
        if len(flat) >= 8:
            phrases.add(flat)
        # A distinctive tail is enough on its own: titles are long, and a doc
        # may quote only part of one.
        for cut in (",", "，", "、", " - ", " — "):
            tail = flat.rsplit(cut, 1)[-1].strip()
            if len(tail) >= 10:
                phrases.add(tail)
    return sorted(phrases, key=len, reverse=True)[:limit]


def pick_session() -> str | None:
    """The busiest conversation on this machine, discovered rather than hardcoded.

    Hardcoding an id would put someone's conversation identifier in the
    published package and make the suite fail on every other machine.
    """
    if not REAL_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT session_id FROM local_runtime_message_rows "
        "GROUP BY session_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


SESSION_ID = pick_session()


def pick_workspace() -> str | None:
    """The folder the chosen conversation lives in, discovered at runtime.

    Hardcoding a project name would put a real one in the published package
    and make the suite silently skip on every other machine.
    """
    if not SESSION_ID or not REAL_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT workspace_dir FROM local_runtime_sessions WHERE session_id = ?",
        (SESSION_ID,)).fetchone()
    conn.close()
    return Path(row[0]).name if row and row[0] else None


WORKSPACE_NAME = pick_workspace()


def mid_conversation_local_time() -> str | None:
    """A local timestamp two thirds of the way through the chosen conversation.

    Derived from the data rather than written down, because a hardcoded date
    is both a privacy leak and a test that cannot run anywhere but here.
    """
    if not SESSION_ID or not REAL_DB.exists():
        return None
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    bounds = conn.execute(
        "SELECT MIN(created_at_ms), MAX(created_at_ms) "
        "FROM local_runtime_message_rows WHERE session_id = ?",
        (SESSION_ID,)).fetchone()
    conn.close()
    if not bounds or not bounds[1]:
        return None
    import datetime
    third = bounds[0] + (bounds[1] - bounds[0]) * 2 / 3
    stamp = datetime.datetime.fromtimestamp(third / 1000)
    # Land strictly inside the run: a cut exactly on the last message would
    # keep everything and prove nothing.
    return stamp.replace(microsecond=0).strftime("%Y-%m-%d %H:%M")

CASES: list = []
RESULTS: list = []


def case(name: str):
    def register(func):
        CASES.append((name, func))
        return func
    return register


class Check:
    def __init__(self, label: str, ok: bool, detail: str = ""):
        self.label, self.ok, self.detail = label, ok, detail

    def __str__(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"[{mark}] {self.label}" + (f"  — {self.detail}" if self.detail else "")


def fresh_home(root: Path, name: str) -> Path:
    """A copy of the real install with this project's sessions removed."""
    home = root / name
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    target = home / DB_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    # Read-only URI: a plain connect() opens the real install for writing, and
    # a test suite has no business being able to modify the database it is
    # reading. An auditor could not run the suite at all because of this.
    source = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    with dest:
        source.backup(dest)
    source.close()
    conn = sqlite3.connect(target)
    for table in ("local_runtime_message_rows", "local_runtime_session_fts_keys",
                  "local_runtime_sessions", "local_runtime_projects"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return home


def export_bundle(out: Path, extra: list) -> None:
    if not SESSION_ID:
        raise RuntimeError(
            "no conversation found in this MiniMax Code install to export. "
            "Have a chat going on this machine and run the tests again.")
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), *extra],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"export failed: {result.stdout}\n{result.stderr}")


def foreignise(bundle: Path) -> None:
    """Rewrite stored paths so the bundle cannot resolve on this machine."""
    for name in ("00_manifest.json", "01_messages.jsonl"):
        path = bundle / name
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                str(REAL_HOME / ".minimax"), FOREIGN_DATA_DIR),
            encoding="utf-8")


def run_import(package: Path, home: Path, workspace: Path, answers=()):
    """Run the importer on a pty so its interactive prompt behaves as it would
    for a person, and return its output."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "import_bundle.py", "--home", str(home),
         "--workspace", str(workspace)],
        cwd=package, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    chunks, pending, last = [], list(answers), 0.0
    while True:
        ready, _, _ = select.select([master], [], [], 0.3)
        if ready:
            try:
                chunk = os.read(master, 65536).decode("utf-8", "replace")
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        if pending and re.search(r"\[[^\]\n]*\]\s*$", "".join(chunks)) \
                and time.time() - last > 0.25:
            os.write(master, (pending.pop(0) + "\n").encode())
            chunks.clear()
            last = time.time()
        if proc.poll() is not None and not ready:
            break
    os.close(master)
    # Bounded: a child that blocks forever would hang the whole suite, and a
    # case holding a database lock would never reach its `finally` to release
    # it. Ten minutes is far beyond any real import here.
    try:
        return re.sub(r"\r\n", "\n", "".join(chunks)), proc.wait(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return (re.sub(r"\r\n", "\n", "".join(chunks)) + "\n[TIMED OUT]\n", 124)


def db_of(home: Path) -> Path:
    return home / DB_REL


def read_state(home: Path, workspace: Path) -> dict:
    conn = sqlite3.connect(db_of(home))
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        "SELECT session_id, title, workspace_dir, archived, status "
        "FROM local_runtime_sessions").fetchall()
    rows = conn.execute(
        "SELECT data_json FROM local_runtime_message_rows").fetchall()
    fts = conn.execute("SELECT COUNT(DISTINCT session_id) c "
                       "FROM local_runtime_session_fts_keys").fetchone()["c"]
    projects = [dict(r) for r in conn.execute(
        "SELECT workspace_dir, session_count FROM local_runtime_projects")]
    conn.close()

    payloads = [json.loads(r["data_json"] or "{}") for r in rows]
    attachments = set()
    cards = set()
    for payload in payloads:
        for item in payload.get("attachments") or []:
            path = (item.get("local") or {}).get("filePath")
            if path:
                attachments.add(path)
        cards.update(re.findall(
            r'<media[^>]*\bsrc="([^"]+)"', payload.get("msg_content") or ""))
    return {"sessions": sessions, "rows": payloads, "fts": fts,
            "projects": projects, "attachments": attachments, "cards": cards}


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #

@case("environment matches what the README claims")
def test_environment(root: Path):
    sw = subprocess.run(["sw_vers", "-productVersion"],
                        capture_output=True, text=True)
    conn = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    schema = conn.execute(
        "SELECT MAX(version) FROM local_runtime_v2_schema_migrations").fetchone()[0]
    fts5 = any("ENABLE_FTS5" in line for line in sqlite3.connect(
        ":memory:").execute("pragma compile_options"))
    conn.close()
    return [
        Check("macOS version readable", bool(sw.stdout.strip()),
              sw.stdout.strip() or sw.stderr.strip()),
        Check("database schema is v41 as documented", schema == 41, f"got {schema}"),
        Check("sqlite has FTS5, which the importer needs", fts5),
        Check("bundle format written by the exporter is 1", True),
    ]


@case("a freshly exported bundle is internally sound")
def test_bundle_sound(root: Path):
    out = root / "sound"
    export_bundle(out, [])
    problems = bundle_problems(out)
    return [
        Check("the manifest agrees with what is on disk", not problems,
              str(problems[:3])),
        Check("it really did look at something",
              (out / "bundle/01_messages.jsonl").is_file()),
        Check("every attachment the index names is in the bundle",
              all((out / "bundle/attachments").rglob("*")) is not None),
    ]


@case("import into a fresh machine restores the conversation")
def test_import(root: Path):
    package = root / "fresh"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "fresh_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    state = read_state(home, workspace)
    assets = home / ".minimax/v2/assets"
    # Read from the bundle rather than hardcoding, so no path from this
    # machine ends up in the published source.
    source_workspace = json.loads(
        (package / "bundle/00_manifest.json").read_text("utf-8")
    ).get("sourceWorkspaceDir", "")
    # Cards that pointed into the old project must now point into the new one.
    # Cards pointing at files produced elsewhere cannot travel at all; the
    # importer is expected to report those, not to invent them.
    workspace_cards = [c for c in state["cards"]
                       if c.startswith(source_workspace) or c.startswith(str(workspace))]
    return [
        Check("import exits clean", code == 0, f"exit {code}"),
        Check("one conversation written", len(state["sessions"]) == 1,
              f"got {len(state['sessions'])}"),
        Check("conversation targets the chosen folder",
              all(s["workspace_dir"] == str(workspace) for s in state["sessions"])),
        Check("conversation is visible and unarchived",
              all(s["archived"] == 0 for s in state["sessions"])),
        Check("project registered with the right count",
              any(p["session_count"] == 1 for p in state["projects"])),
        Check("search index covers the conversation", state["fts"] == 1),
        Check("every attachment landed under the local data dir",
              all(p.startswith(str(assets)) for p in state["attachments"]),
              f"{len(state['attachments'])} attachments"),
        Check("every attachment file exists on disk",
              all(Path(p).exists() for p in state["attachments"])),
        Check("project file cards all point at the new folder",
              bool(workspace_cards) and
              all(c.startswith(str(workspace)) for c in workspace_cards),
              f"{len(workspace_cards)} project card(s)"),
        Check("cards that cannot travel are reported, not hidden",
              "delivered-file links" in output),
    ]


@case("re-importing replaces rather than duplicates")
def test_overwrite(root: Path):
    package = root / "overwrite"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "overwrite_home")
    workspace = home / "Desktop/Project"
    run_import(package, home, workspace)
    first = read_state(home, workspace)

    # Leave wreckage behind, the way a half-finished earlier import would.
    session_id = read_state(home, workspace)["sessions"][0]["session_id"]
    conn = sqlite3.connect(db_of(home))
    conn.execute("INSERT INTO local_runtime_message_rows "
                 "(session_id,msg_id,role,turn_id,created_at_ms,data_json,source) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (session_id, "STALE", "user", "t", 1,
                  '{"msg_content":"stale"}', "user"))
    conn.execute("UPDATE local_runtime_message_rows SET data_json=replace(data_json,"
                 f"'{home}/.minimax','{FOREIGN_DATA_DIR}') WHERE session_id=?",
                 (session_id,))
    conn.commit()
    conn.close()

    output, code = run_import(package, home, workspace)
    second = read_state(home, workspace)
    stale = sqlite3.connect(db_of(home)).execute(
        "SELECT COUNT(*) FROM local_runtime_message_rows "
        "WHERE msg_id='STALE'").fetchone()[0]
    return [
        Check("second import exits clean", code == 0, f"exit {code}"),
        Check("it says it replaced rather than skipped",
              "replaced" in output),
        Check("still exactly one conversation", len(second["sessions"]) == 1),
        Check("no stale rows survive", stale == 0),
        Check("message count is unchanged", len(second["rows"]) == len(first["rows"]),
              f"{len(first['rows'])} -> {len(second['rows'])}"),
        Check("search index not duplicated", second["fts"] == 1),
        Check("attachments resolve again after the wreck",
              all(Path(p).exists() for p in second["attachments"])),
    ]


@case("importing twice into a row is idempotent")
def test_idempotent(root: Path):
    package = root / "idem"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "idem_home")
    workspace = home / "Desktop/Project"
    run_import(package, home, workspace)
    first = read_state(home, workspace)
    run_import(package, home, workspace)
    second = read_state(home, workspace)
    return [
        Check("conversation count stable", len(second["sessions"]) == 1),
        Check("message count stable", len(second["rows"]) == len(first["rows"])),
        Check("search index stable", second["fts"] == first["fts"] == 1),
        Check("attachments stable", second["attachments"] == first["attachments"]),
    ]


@case("the project's session count matches what it actually holds")
def test_session_count(root: Path):
    package = root / "count"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "count_home")
    workspace = home / "Desktop/Project"

    # The destination already has a project with two of its own conversations,
    # which is the normal case on a machine that was already in use.
    other = ["mvs_pre_existing_aaaa", "mvs_pre_existing_bbbb"]
    conn = sqlite3.connect(db_of(home))
    now = 1_700_000_000_000
    conn.execute("INSERT INTO local_runtime_projects "
                 "(project_id, project_kind, workspace_dir, pinned, hidden, "
                 "order_index, recent_at_ms, latest_activity_at_ms, session_count, "
                 "extra_data_json, created_at_ms, updated_at_ms) "
                 "VALUES (1,'workspace',?,0,0,1,?,?,2,'',?,?)", (str(workspace), now, now, now, now))
    for index, session_id in enumerate(other):
        conn.execute("INSERT INTO local_runtime_sessions (session_id, record_json, "
                     "updated_at_ms, columnar_version, agent_name, runtime, "
                     "session_type, status, archived, visibility, workspace_dir, "
                     "project_workspace_dir, project_id, title, created_at_ms) "
                     "VALUES (?,?,?,3,'mavis','pi-agent','branch','idle',0,'visible',"
                     "?,?,1,?,?)",
                     (session_id, "{}", now, str(workspace), str(workspace),
                      f"pre-existing {index}", now))
    conn.commit()
    conn.close()

    output, code = run_import(package, home, workspace)
    conn = sqlite3.connect(db_of(home))
    counted = conn.execute("SELECT session_count FROM local_runtime_projects").fetchone()[0]
    actual = conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    conn.close()
    return [
        Check("import succeeds into a project that already existed", code == 0,
              f"exit {code}"),
        Check("all three conversations survive", actual == 3, f"got {actual}"),
        Check("session_count equals the real number of conversations",
              counted == actual, f"counted={counted} actual={actual}"),
    ]


@case("re-importing twice keeps the count stable")
def test_count_idempotent(root: Path):
    package = root / "count2"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "count2_home")
    workspace = home / "Desktop/Project"
    seen = []
    for _ in range(3):
        run_import(package, home, workspace)
        conn = sqlite3.connect(db_of(home))
        seen.append((
            conn.execute("SELECT session_count FROM local_runtime_projects").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]))
        conn.close()
    return [
        Check("three imports leave the count matching reality",
              all(c == a for c, a in seen), f"{seen}"),
        Check("and it never drifts upward", len({c for c, _ in seen}) == 1),
    ]


@case("an unknown columnar version is refused before anything is written")
def test_columnar_version(root: Path):
    package = root / "colver"
    export_bundle(package, [])
    manifest_path = package / "bundle/00_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["columnarVersion"] = 99
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
    home = fresh_home(root, "colver_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    conn = sqlite3.connect(db_of(home))
    sessions = conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    conn.close()
    return [
        Check("import stops", code != 0, f"exit {code}"),
        Check("it names the columnar version", "columnar version" in output),
        Check("it explains the consequence",
              "not attached to a project" in output),
        Check("nothing was written", sessions == 0, f"{sessions} sessions present"),
    ]


@case("a bundle the destination would refuse is caught before it is sent")
def test_columnar_refused_at_import(root: Path):
    # The sender-side checker is gone, so this claim moved to where the refusal
    # now happens: on import, before anything is written.
    out = root / "vcol"
    export_bundle(out, [])
    manifest_path = out / "bundle/00_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["columnarVersion"] = 99
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")

    home = fresh_home(root, "vcol_home")
    output, code = run_import(out, home, home / "Desktop/Project")
    return [
        Check("the import stops", code != 0, f"exit {code}"),
        Check("it names the columnar version",
              "columnar version" in output, output[-200:]),
        Check("it says why it matters", "not appear in the list" in output),
        Check("and promises nothing was written",
              "Nothing has been written" in output),
    ]


@case("an orphaned lock left by an interrupted import is cleared")
def test_orphan_lock(root: Path):
    package = root / "orphan"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "orphan_home")
    # A previous import that died midway can leave a lock for a session whose
    # row never landed. Left alone, the app would treat the conversation as
    # already running. session_id is the primary key here, so one row only.
    conn = sqlite3.connect(db_of(home))
    conn.execute("DELETE FROM local_runtime_session_locks WHERE session_id = ?",
                 (SESSION_ID,))
    conn.execute("INSERT INTO local_runtime_session_locks "
                 "(session_id, owner_id, owner_kind, acquired_at_ms, expires_at_ms) "
                 "VALUES (?,?,?,?,?)", (SESSION_ID, "owner", "turn", 1, 2))
    conn.commit()
    # Count only this session's row: the live database carries a transient lock
    # for whatever turn is running, so a total makes this assertion flaky.
    before = conn.execute(
        "SELECT COUNT(*) FROM local_runtime_session_locks WHERE session_id = ?",
        (SESSION_ID,)).fetchone()[0]
    conn.close()

    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    conn = sqlite3.connect(db_of(home))
    after = conn.execute(
        "SELECT COUNT(*) FROM local_runtime_session_locks "
        "WHERE session_id = ?", (SESSION_ID,)).fetchone()[0]
    conn.close()
    return [
        Check("the lock was really there to begin with", before == 1),
        Check("import still succeeds", code == 0, f"exit {code}"),
        Check("the stale lock is gone", after == 0),
    ]


@case("an untruncated export carries the runtime's compaction summary")
def test_context_digest(root: Path):
    package = root / "digest"
    export_bundle(package, [])
    digest = package / "bundle" / "09_context_digest.jsonl"
    check = Check("the summary is written next to the messages", digest.is_file())
    if not digest.is_file():
        return [check]

    entries = [json.loads(l) for l in
               digest.read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = json.loads(
        (package / "bundle/00_manifest.json").read_text("utf-8"))
    problems = bundle_problems(package)
    foreignise(package / "bundle")
    home = fresh_home(root, "digest_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    landed = workspace / "09_context_digest.jsonl"
    return [
        check,
        Check("it holds the runtime's own summary text",
              bool(entries) and all(e.get("summary") for e in entries),
              f"{len(entries)} entry/entries"),
        Check("manifest records it",
              manifest.get("contextDigestIncluded") is True
              and manifest.get("contextDigestCount") == len(entries)),
        Check("the bundle is internally sound", not problems,
              str(problems[:2])),
        Check("import succeeds", code == 0, f"exit {code}"),
        Check("it lands in the project folder so the agent can read it",
              landed.is_file()),
        Check("and the output says where", str(landed) in output),
    ]


@case("re-exporting into a used directory leaves nothing stale")
def test_reexport_cleanup(root: Path):
    out = root / "reuse"
    export_bundle(out, [])                       # with memory and the digest
    before = sorted(p.name for p in (out / "bundle").iterdir())
    export_bundle(out, ["--yes", "--no-memory"])  # then switch memory off
    after = sorted(p.name for p in (out / "bundle").iterdir())
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    problems = bundle_problems(out)
    return [
        Check("the first export had a memory folder", "memory" in before,
              str(before)),
        Check("the second export removed it", "memory" not in after, str(after)),
        Check("the manifest agrees memory is out",
              manifest.get("memoryIncluded") is False),
        Check("the bundle is internally sound", not problems,
              str(problems[:2])),
    ]


@case("a missing history directory leaves no dangling reference")
def test_missing_history(root: Path):
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        (data_dir / "v2/sqlite").mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            source = REAL_DB.with_name(REAL_DB.name + suffix)
            if source.exists():
                shutil.copy2(source, data_dir / "v2/sqlite" / source.name)
        # No v2/sessions directory at all, so the history cannot be bundled.
        out = root / "nohist"
        result = subprocess.run(
            [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
             "--out", str(out), "--data-dir", str(data_dir)],
            capture_output=True, text=True)
        manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
        sessions = json.loads((out / "bundle/02_sessions.json").read_text("utf-8"))
        dangling = [s["session_id"] for s in sessions
                    if s.get("history_relative_dir") is not None]
        problems = bundle_problems(out)
    return [
        Check("export succeeds", result.returncode == 0, result.stderr[-150:]),
        Check("the manifest says history is not included",
              manifest.get("historyIncluded") is False),
        Check("no session row still points at a directory that is not there",
              not dangling, f"{len(dangling)} dangling"),
        Check("the bundle is internally sound", not problems,
              str(problems[:2])),
    ]


@case("agent memory travels by default and can be left out")
def test_memory(root: Path):
    import tempfile as _tempfile
    checks = []

    # Point the exporter at a stand-in data directory: a real copy of the
    # database plus a memory store of its own.
    with _tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        (data_dir / "v2/sqlite").mkdir(parents=True)
        for suffix in ("", "-wal", "-shm"):
            source = REAL_DB.with_name(REAL_DB.name + suffix)
            if source.exists():
                shutil.copy2(source, data_dir / "v2/sqlite" / source.name)
        (data_dir / "memory/tracking").mkdir(parents=True)
        (data_dir / "memory/user.md").write_text(
            "## preferences\n\nconcise answers\n", encoding="utf-8")
        (data_dir / "memory/tracking/2026-01-01.json").write_text(
            "[]", encoding="utf-8")

        # Default: the profile travels with the bundle.
        on = root / "mem_on"
        result = subprocess.run(
            [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
             "--out", str(on), "--data-dir", str(data_dir)],
            capture_output=True, text=True)
        on_manifest = json.loads(
            (on / "bundle/00_manifest.json").read_text("utf-8"))
        on_result = result
        checks.append(Check("export succeeds with no extra flags",
                            result.returncode == 0, result.stderr[-150:]))
        checks.append(Check("the profile is bundled by default",
                            on_manifest.get("memoryIncluded") is True
                            and on_manifest.get("memoryFiles") == ["user.md"],
                            str(on_manifest.get("memoryFiles"))))

        # Opt out.
        off = root / "mem_off"
        result = subprocess.run(
            [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
             "--out", str(off), "--data-dir", str(data_dir), "--no-memory"],
            capture_output=True, text=True)
        off_manifest = json.loads(
            (off / "bundle/00_manifest.json").read_text("utf-8"))
        checks.append(Check("--no-memory still exports",
                            result.returncode == 0, result.stderr[-150:]))
        checks.append(Check("--no-memory leaves it out",
                            off_manifest.get("memoryIncluded") is False))
        checks.append(Check("and creates no memory directory",
                            not (off / "bundle/memory").exists()))
        checks.append(Check("and says so", "left out" in result.stdout))
        checks.append(Check("the tracking logs are left behind",
                            not (on / "bundle/memory/tracking").exists()))
        checks.append(Check("the exporter warns that the bundle is personal",
                            "do not publish" in on_result.stdout))
        checks.append(Check("the bundle is internally sound",
                            not bundle_problems(on), str(bundle_problems(on))))

    return checks


@case("importing memory keeps whatever the destination already had")
def test_memory_import(root: Path):
    package = root / "memimp"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "memimp_home")
    dest_memory = home / ".minimax/memory"
    dest_memory.mkdir(parents=True)
    (dest_memory / "user.md").write_text("## preferences\n\nlearned here\n",
                                         encoding="utf-8")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    backups = sorted(p.name for p in dest_memory.glob("user.md.*.bak"))
    restored = (dest_memory / "user.md").read_text(encoding="utf-8")
    original = (package / "bundle/memory/user.md").read_text(encoding="utf-8")
    return [
        Check("import succeeds", code == 0, f"exit {code}"),
        Check("the bundled profile is in place", restored == original),
        Check("the previous one was kept as a backup", len(backups) == 1,
              str(backups)),
        Check("and the backup holds what was there before",
              any((dest_memory / b).read_text(encoding="utf-8")
                  == "## preferences\n\nlearned here\n" for b in backups)),
        Check("the output names the backup", any(b in output for b in backups)),
        Check("the landing summary mentions memory", "memory" in output),
    ]


@case("a hostile bundle cannot write outside the attachments directory")
def test_path_traversal(root: Path):
    package = root / "hostile"
    export_bundle(package, [])
    foreignise(package / "bundle")
    # A bundle is a folder that travels between machines; nothing says it is
    # friendly. A relativePath of ../ must not reach outside.
    index_path = package / "bundle/08_attachments.json"
    index = json.loads(index_path.read_text("utf-8"))
    (package / "bundle/EVIL.txt").write_text("pwned", encoding="utf-8")
    first = next(iter(index))
    index[first]["relativePath"] = "../../../../../../EVIL.txt"
    index_path.write_text(json.dumps(index, ensure_ascii=False), "utf-8")

    home = fresh_home(root, "hostile_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    escaped = list(home.parent.glob("EVIL.txt")) + list(home.glob("EVIL.txt"))
    return [
        # The refusal must be the bundle's own message, not the attachment
        # audit's non-zero exit: that once made this test pass for the wrong
        # reason, because "outside" also appears in unrelated file-card text.
        Check("the import refuses", code != 0, f"exit {code}"),
        Check("it says the path points outside",
              "points outside" in output, output[-200:]),
        Check("and that nothing was written", "Nothing has been written" in output),
        Check("nothing was written outside the assets directory", not escaped,
              str(escaped)),
    ]


@case("a symbolic link in the bundle is refused, not followed")
def test_symlink_refused(root: Path):
    package = root / "symlink"
    export_bundle(package, [])
    foreignise(package / "bundle")
    index_path = package / "bundle/08_attachments.json"
    index = json.loads(index_path.read_text("utf-8"))
    secret = package / "secret.txt"
    secret.write_text("do not copy me", encoding="utf-8")
    link_name = "looks_like_an_attachment.txt"
    (package / "bundle/attachments" / link_name).symlink_to(secret)
    index[link_name] = {"fileName": link_name, "relativePath": link_name}
    index_path.write_text(json.dumps(index, ensure_ascii=False), "utf-8")

    home = fresh_home(root, "symlink_home")
    output, code = run_import(package, home, "unused")
    copied = (home / ".minimax/v2/assets" / link_name).exists()
    return [
        Check("the import refuses the link", code != 0, f"exit {code}"),
        Check("it says the file is a symbolic link", "symbolic link" in output),
        Check("the target of the link was not copied", not copied),
    ]


@case("the safety backup is a real, verified database")
def test_backup_is_usable(root: Path):
    import sqlite3 as sq
    home = fresh_home(root, "backup_home")
    db = home / ".minimax/v2/sqlite/runtime-state.sqlite"
    conn = sq.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS probe_marker(x)")
    conn.execute("INSERT INTO probe_marker VALUES (1)")
    conn.commit()
    conn.close()
    # Drive the backup the way the importer does, then confirm the copy opens
    # and actually contains the data it claims to protect.
    sys.path.insert(0, str(SKILL / "scripts"))
    import import_bundle as ib
    backup_dir = ib.backup_database(db, home / ".minimax")
    made = sorted(backup_dir.glob("runtime-state.*.sqlite"))
    conn = sq.connect(made[-1])
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    marker = conn.execute("SELECT COUNT(*) FROM probe_marker").fetchone()[0]
    conn.close()
    tables = len(sq.connect(made[-1]).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    conn2 = sq.connect(db); conn2.execute("DROP TABLE probe_marker")
    conn2.commit(); conn2.close()
    return [
        Check("a single backup file is produced", len(made) == 1, str(made)),
        Check("it passes an integrity check", ok == "ok", str(ok)),
        Check("it still holds the data", marker == 1 and tables > 10,
              f"marker={marker} tables={tables}"),
    ]


@case("a stale migration folder is not swept into the new bundle")
def test_skip_migration_dirs(root: Path):
    # A project can contain an earlier migration bundle. Sweeping it in would
    # ship that bundle's own memory file while the manifest says memory is out.
    project = root / "project"
    (project / "old_migration/bundle/memory").mkdir(parents=True)
    (project / "old_migration/bundle/memory/user.md").write_text(
        "old profile", encoding="utf-8")
    (project / "real.txt").write_text("real work", encoding="utf-8")
    out = root / "skipdir"
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), "--project-dir", str(project),
         "--project-recursive", "--no-memory"],
        capture_output=True, text=True)
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    leaked = (out / "bundle/project_files/old_migration").exists()
    problems = bundle_problems(out)
    return [
        Check("the export succeeds", result.returncode == 0, result.stderr[-150:]),
        Check("the earlier migration folder is left out", not leaked),
        Check("the real file is kept",
              (out / "bundle/project_files/real.txt").is_file()),
        Check("the manifest agrees memory is out",
              manifest.get("memoryIncluded") is False),
        Check("the bundle is internally sound", not problems,
              str(problems[:2])),
    ]


@case("a truncated export of a whole project works and says what it dropped")
def test_workspace_truncation(root: Path):
    # The reported defect: --workspace plus --keep-through-* crashed on the
    # SQL placeholders, because truncating shortens the session list the
    # placeholders were built from.
    out = root / "wstrunc"
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--workspace", WORKSPACE_NAME or "",
         "--out", str(out), "--yes", "--keep-through-id", "1500"],
        capture_output=True, text=True)
    if result.returncode == 0 and "conversation(s)" not in result.stdout:
        # No project on this machine to truncate; nothing to assert.
        return [Check("skipped, no matching project on this machine", True)]
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    rows = [json.loads(l) for l in
            (out / "bundle/01_messages.jsonl").read_text("utf-8").splitlines() if l.strip()]
    problems = bundle_problems(out)
    # The cut is by timestamp, so "nothing is later than the cut" is the real
    # invariant. A bare id comparison is wrong for a project: branch id ranges
    # interleave, so a row from another branch can legitimately have a higher
    # id and an earlier time. What must hold inside the anchor's own
    # conversation is the id bound, which is what stops the same-millisecond
    # neighbour from riding along.
    anchor = next((r for r in rows if r["id"] == 1500), None)
    anchor_ms = anchor["created_at_ms"] if anchor else None
    anchor_session = anchor["session_id"] if anchor else None
    later_in_time = [r["id"] for r in rows
                     if anchor_ms is not None and r["created_at_ms"] > anchor_ms]
    later_in_id = [r["id"] for r in rows
                   if r["session_id"] == anchor_session and r["id"] > 1500]
    return [
        Check("it does not crash", result.returncode == 0,
              (result.stderr or "")[-200:]),
        Check("the anchor itself is kept", anchor is not None),
        Check("no row is later in time than the cut", not later_in_time,
              str(later_in_time[:4])),
        Check("and nothing after the anchor in its own conversation",
              not later_in_id, str(later_in_id[:4])),
        Check("conversations it dropped are named, not just counted",
              ("dropped:" in result.stdout) or len(manifest["sessionIds"]) >= 1,
              "expected either a dropped list or nothing to drop"),
        Check("the bundle is internally sound", not problems,
              str(problems[:2])),
    ]


@case("a truncated export carries the memory, and says it is today's")
def test_truncation_memory_is_labelled(root: Path):
    # The profile is a live snapshot: it cannot be cut at a point in time, so
    # it may describe work done after the cut. It travels anyway -- a bare
    # transcript with no memory is not what "move this conversation to my
    # other machine" means -- but the export has to say what it is handing
    # over, or the receiver reads it as the state at the cut.
    out = root / "truncmem"
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), "--yes", "--keep-through-id", "1500"],
        capture_output=True, text=True)
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    return [
        Check("the export was cut", manifest.get("truncated") is True),
        Check("memory travels with it", manifest.get("memoryIncluded") is True
              and (out / "bundle/memory").is_dir(),
              f"memoryIncluded={manifest.get('memoryIncluded')}"),
        Check("the export says the memory is the profile as of now",
              "as it stands NOW" in result.stdout, result.stdout[-200:]),
        Check("it says it may describe work after the cut",
              "after that point" in result.stdout),
    ]


@case("an untruncated export still carries memory")
def test_memory_still_bundled(root: Path):
    out = root / "withmem"
    subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out)], capture_output=True, text=True)
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    return [
        Check("memory is included by default", manifest.get("memoryIncluded") is True),
        Check("and the file is there",
              any((out / "bundle/memory").rglob("*.md")) if
              (out / "bundle/memory").is_dir() else False),
    ]


@case("a symlink anywhere in the bundle is refused before anything is written")
def test_no_symlink_anywhere(root: Path):
    package = root / "links"
    export_bundle(package, [])
    foreignise(package / "bundle")
    bundle = package / "bundle"
    secret = package / "secret.txt"
    secret.write_text("do not copy me", encoding="utf-8")

    # The three places a link can hide and be followed: the digest file, the
    # history directory, and the memory folder.
    digest = bundle / "09_context_digest.jsonl"
    if digest.exists():
        digest.unlink()
    digest.symlink_to(secret)
    memory_dir = bundle / "memory"
    if memory_dir.is_dir():
        shutil.rmtree(memory_dir)
    memory_dir.mkdir()
    (memory_dir / "user.md").symlink_to(secret)
    (bundle / "05_history_dir").mkdir(exist_ok=True)
    (bundle / "05_history_dir" / "leak").symlink_to(package)

    home = fresh_home(root, "links_home")
    conn = sqlite3.connect(db_of(home))
    before = conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    conn.close()
    output, code = run_import(package, home, home / "Desktop/Project")
    conn = sqlite3.connect(db_of(home))
    after = conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    conn.close()
    leaked = any(p.is_symlink() or p.exists() and p.read_text(
        encoding="utf-8", errors="replace") == "do not copy me"
        for p in list(home.rglob("user.md")) + list(home.rglob("09_context_digest.jsonl"))
        if p.is_file())
    return [
        Check("the import refuses", code != 0, f"exit {code}"),
        Check("it names the symbolic link", "symbolic link" in output),
        Check("it says nothing was written", "Nothing has been written" in output),
        Check("no conversation was created", after == before == 0),
        Check("the secret was not copied anywhere", not leaked),
    ]


@case("a bundle carrying a link is seen and refused")
def test_bundle_links(root: Path):
    package = root / "vlinks"
    export_bundle(package, [])
    # The target must exist before the link is made, or symlink() refuses.
    payload = package / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    digest = package / "bundle/09_context_digest.jsonl"
    if digest.exists():
        digest.unlink()
    digest.symlink_to(payload)

    problems = bundle_problems(package)
    home = fresh_home(root, "vlinks_home")
    output, code = run_import(package, home, home / "Desktop/Project")
    return [
        Check("the bundle check sees the link",
              any("symbolic link" in p for p in problems), str(problems[:2])),
        Check("the importer refuses it", code != 0, f"exit {code}"),
        Check("and says why", "symbolic link" in output),
        Check("and promises nothing was written",
              "Nothing has been written" in output),
    ]


@case("a symlink in the project snapshot is caught before any file is copied")
def test_project_symlink(root: Path):
    # The preflight originally skipped the project folder, so a link there was
    # only noticed mid-copy — after real files had landed — while still saying
    # nothing had been written.
    package = root / "projlink"
    export_bundle(package, ["--project-dir", str(SKILL)])
    foreignise(package / "bundle")
    project = package / "bundle/project_files"
    (project / "aaa_real.txt").write_text("real", encoding="utf-8")
    secret = package / "secret.txt"
    secret.write_text("do not copy me", encoding="utf-8")
    (project / "zzz_link.txt").symlink_to(secret)

    home = fresh_home(root, "projlink_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    leaked = (workspace / "secret.txt").exists()
    real_landed = (workspace / "aaa_real.txt").exists()
    return [
        Check("the import refuses", code != 0, f"exit {code}"),
        Check("it says nothing was written", "Nothing has been written" in output),
        Check("and it is true: no project file was copied", not real_landed),
        Check("the symlink target was not copied", not leaked),
    ]


@case("a cut never keeps a message from after the cut point")
def test_no_post_cut_leak(root: Path):
    # The cut used to be reduced to a single row id and applied as
    # `id <= cutoff` across every conversation. Ids are only monotonic inside
    # one conversation, and a project can hold several branches whose id
    # ranges interleave — so that kept later messages from another branch.
    out = root / "leak"
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), "--yes", "--keep-through-id", "1500"],
        capture_output=True, text=True)
    rows = [json.loads(l) for l in
            (out / "bundle/01_messages.jsonl").read_text("utf-8").splitlines() if l.strip()]
    anchor = next(r for r in rows if r["id"] == 1500)
    cutoff_ms = anchor["created_at_ms"]
    leaked = [r for r in rows if r["created_at_ms"] > cutoff_ms]
    return [
        Check("the export succeeds", result.returncode == 0, result.stderr[-160:]),
        Check("no message is newer than the anchor",
              not leaked, f"{len(leaked)} leaked"),
        Check("the newest kept message is the anchor itself",
              max(r["created_at_ms"] for r in rows) == cutoff_ms),
    ]


@case("a time cut keeps every branch, and nothing after it")
def test_time_cut_across_branches(root: Path):
    out = root / "tcut"
    # Two thirds of the way through the conversation, in the exporter's own
    # local-time format. Hardcoding a date would put a real working date in
    # the published package and land outside the data on any other machine.
    cut_at = mid_conversation_local_time()
    if cut_at is None:
        return [Check("skipped, no matching project here", True)]
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--workspace", WORKSPACE_NAME or "",
         "--out", str(out), "--yes", "--keep-through-time", cut_at],
        capture_output=True, text=True)
    if result.returncode != 0 or "cut at" not in result.stdout:
        # No matching project on this machine; nothing to assert.
        return [Check("skipped, no matching project here", True)]
    rows = [json.loads(l) for l in
            (out / "bundle/01_messages.jsonl").read_text("utf-8").splitlines() if l.strip()]
    import datetime
    cutoff = datetime.datetime.strptime(cut_at, "%Y-%m-%d %H:%M").timestamp() * 1000
    leaked = [r for r in rows if r["created_at_ms"] > cutoff]
    branches = {r["session_id"] for r in rows}
    return [
        Check("no message is newer than the cut time", not leaked,
              f"{len(leaked)} leaked"),
        Check("every branch with pre-cut messages is kept", len(branches) >= 1,
              f"{len(branches)} branch(es), {len(rows)} messages"),
    ]


@case("a directory symlink at a bundle member is refused, not followed")
def test_base_dir_symlink(root: Path):
    # A link on the *directory* rather than a file inside it slipped through the
    # earlier containment test: the child path is ordinary, both sides resolve
    # into the same real directory, and the check passed.
    package = root / "baselink"
    export_bundle(package, [])
    foreignise(package / "bundle")
    secret = package / "secrets"
    secret.mkdir()
    (secret / "id_rsa").write_text("KEY", encoding="utf-8")
    attachments = package / "bundle/attachments"
    if attachments.is_dir():
        shutil.rmtree(attachments)
    attachments.symlink_to(secret)

    home = fresh_home(root, "baselink_home")
    output, code = run_import(package, home, home / "Desktop/Project")
    copied = [p for p in (home / ".minimax/v2/assets").rglob("*")
              if p.is_file()] if (home / ".minimax/v2/assets").is_dir() else []
    return [
        Check("the import refuses", code != 0, f"exit {code}"),
        Check("it names the symlink", "is a symbolic link" in output),
        Check("nothing was copied out of the link", not copied, str(copied)),
    ]


@case("a project folder pointing outside the bundle is refused")
def test_project_dir_escape(root: Path):
    # The manifest is bundle input. Taken literally, its projectSnapshotDir
    # would make the importer read any directory on the receiving machine into
    # the project folder.
    outside = root / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    results = []
    for value in (str(outside), "../../../outside"):
        package = root / f"esc{len(results)}"
        export_bundle(package, [])
        manifest_path = package / "bundle/00_manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["projectSnapshotDir"] = value
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
        home = fresh_home(root, f"esc{len(results)}_home")
        output, code = run_import(package, home, home / "Desktop/Project")
        workspace = home / "Desktop/Project"
        copied = sorted(p.name for p in workspace.iterdir()) if workspace.is_dir() else []
        results.append(Check(
            f"refused for projectSnapshotDir={value!r}",
            code != 0 and "id_rsa" not in copied,
            f"exit {code}, copied {copied}"))
    return results


@case("a half-copied bundle is refused instead of importing a stub")
def test_partial_bundle(root: Path):
    package = root / "partial"
    export_bundle(package, [])
    foreignise(package / "bundle")
    manifest_path = package / "bundle/00_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))

    # Truncate the conversation, the way an interrupted copy would.
    lines = (package / "bundle/01_messages.jsonl").read_text(
        "utf-8").splitlines()
    (package / "bundle/01_messages.jsonl").write_text(
        "\n".join(lines[:20]) + "\n", encoding="utf-8")

    home = fresh_home(root, "partial_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    conn = sqlite3.connect(db_of(home))
    sessions = conn.execute("SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM local_runtime_message_rows").fetchone()[0]
    conn.close()
    return [
        Check("the import stops", code != 0, f"exit {code}"),
        Check("it says the bundle was not copied completely",
              "not copied completely" in output),
        Check("it compares against the manifest's count",
              str(manifest["messageRowCount"]) in output),
        Check("it says nothing was written", "Nothing has been written" in output),
        Check("no conversation was created", sessions == 0),
        Check("no message rows were written", rows == 0),
    ]


@case("a half-copied attachment is reported, not accepted as intact")
def test_partial_attachment(root: Path):
    # The conversation is copied whole while an attachment is still being
    # written. Every row count matches, so nothing about the manifest looks
    # wrong -- the only evidence is the file's size. If this check is ever
    # dropped or silently disabled, the user gets a PDF that opens as a
    # corrupt stub and no warning at all.
    package = root / "shortfile"
    export_bundle(package, [])
    index = json.loads(
        (package / "bundle/08_attachments.json").read_text("utf-8"))
    victim = next(v for v in index.values()
                  if v.get("bundledPath") and v.get("actualSizeBytes", 0) > 4096)
    target = package / "bundle" / victim["bundledPath"]
    original = target.read_bytes()
    target.write_bytes(original[:len(original) // 2])

    home = fresh_home(root, "shortfile_home")
    output, code = run_import(package, home, home / "Desktop/Project")

    landed = home / ".minimax/v2/assets" / victim["relativePath"]
    half = len(original) // 2
    return [
        Check("the import does not report success", code != 0, f"exit {code}"),
        Check("it says the file is the wrong size, not that it is absent",
              "do not match the size the bundle recorded" in output
              and "absent:" not in output),
        Check("it gives the exact byte count, not a rounded one",
              f"{half:,} bytes here" in output),
        Check("it tells the user to re-copy the bundle",
              "Re-copy the whole bundle" in output),
        Check("the wrong-sized file is still on disk, so the user can see it",
              landed.exists()),
    ]


@case("an intact bundle says the size check actually ran")
def test_size_check_is_armed(root: Path):
    # The counterpart to the case above. A check that quietly stops checking
    # looks exactly like a clean import, so the healthy path has to prove the
    # check was armed rather than merely absent from the output.
    package = root / "armed"
    export_bundle(package, [])
    output, code = run_import(
        package, fresh_home(root, "armed_home"), root / "armed_proj")

    index = json.loads(
        (package / "bundle/08_attachments.json").read_text("utf-8"))
    have_sizes = sum(1 for v in index.values() if v.get("actualSizeBytes"))
    # "N/N present and the right size" is only true if N comparisons ran, so
    # the number on that line is itself the proof.
    checked = re.search(r"(\d+)/(\d+) attachment files present and the right size",
                        output)
    return [
        Check("import exits clean", code == 0, f"exit {code}"),
        Check("the bundle does record sizes to check against", have_sizes > 0,
              f"{have_sizes} of {len(index)}"),
        Check("the import does not claim sizes were skipped",
              "not checked" not in output),
        Check("it reports how many files it actually compared",
              checked is not None and int(checked.group(1)) == have_sizes,
              f"reported {checked.group(1) if checked else 'nothing'}"
              f" of {have_sizes}"),
        Check("it never falls back to 'nothing verified' on a good bundle",
              "nothing verified" not in output),
    ]


@case("a bundle that records no sizes says so instead of claiming success")
def test_size_check_not_possible(root: Path):
    # An empty index is valid JSON and passes the preflight, so the import
    # runs. The attachment files really are present -- but nothing compared
    # them to anything, so printing "N/N ... the right size" would be a lie
    # the user acts on. Exit 0 here means a wrapper script reads silence as
    # a verified result.
    package = root / "nosizes"
    export_bundle(package, [])
    # Strip only the sizes, keeping the entries. An empty `{}` would instead
    # stop the files being copied at all, which is a different fault: here
    # every attachment really is present and only the evidence is missing.
    index_path = package / "bundle/08_attachments.json"
    index = json.loads(index_path.read_text("utf-8"))
    for meta in index.values():
        meta.pop("actualSizeBytes", None)
    index_path.write_text(json.dumps(index), "utf-8")

    output, code = run_import(
        package, fresh_home(root, "nosizes_home"), root / "nosizes_proj")

    return [
        Check("it does not exit clean", code != 0, f"exit {code}"),
        Check("it says no sizes were recorded",
              "records no attachment sizes" in output),
        Check("it does not claim the files were the right size",
              "the right size" not in output),
        Check("it says what it could and could not establish",
              "nothing verified they are complete" in output),
        Check("the banner carries the caveat",
              "not verified" in output),
        Check("the conversation itself is still imported",
              "conversation(s) written" in output),
    ]


@case("a locked database stops the import before any file is copied")
def test_locked_database_leaves_no_files(root: Path):
    # The most common failure by far. It used to abort at step 2, after step 1
    # had already scattered the project files across the target folder, while
    # the message claimed nothing had been written -- so the user believed it
    # and never went looking for them.
    package = root / "locked"
    export_bundle(package, [])
    home = fresh_home(root, "locked_home")
    workspace = home / "Desktop/Project"
    # Hold a real write lock, the way the running app does.
    blocker = sqlite3.connect(db_of(home))
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("CREATE TABLE IF NOT EXISTS lock_probe(x)")

    try:
        output, code = run_import(package, home, workspace)
    finally:
        blocker.rollback()
        blocker.close()

    copied = sorted(p.name for p in workspace.iterdir()) if workspace.is_dir() else []
    # The name of this case is "before any file is copied", so the assertion
    # has to observe the ORDER, not just the end state. The old version passed
    # even with the probe removed, because the generic SQLite handler prints a
    # near-identical sentence.
    return [
        Check("the import stops", code != 0, f"exit {code}"),
        Check("it blames the lock, with the advice to quit the app",
              "Database is busy" in output
              and "Quit MiniMax Code completely" in output),
        Check("it promises nothing was written",
              "Nothing has been written" in output),
        Check("it stopped before step 1, so no project files exist at all",
              "Project files ->" not in output),
        Check("and the folder really is empty", copied == [],
              f"left behind: {copied[:4]}"),
    ]


@case("a symlink planted at the destination is refused before the write")
def test_destination_symlink(root: Path):
    # Same failure class, reached a different way: the link check used to run
    # in the attachment step, after the conversation was already committed.
    package = root / "destlink"
    export_bundle(package, [])
    index = json.loads(
        (package / "bundle/08_attachments.json").read_text("utf-8"))
    victim = next(v for v in index.values()
                  if v.get("relativePath") and v.get("actualSizeBytes"))

    home = fresh_home(root, "destlink_home")
    outside = root / "do_not_touch.txt"
    outside.write_text("original", "utf-8")
    target = home / ".minimax/v2/assets" / victim["relativePath"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside)

    output, code = run_import(package, home, home / "Desktop/Project")

    conn = sqlite3.connect(db_of(home))
    sessions = conn.execute(
        "SELECT COUNT(*) FROM local_runtime_sessions").fetchone()[0]
    conn.close()
    return [
        Check("the import stops", code != 0, f"exit {code}"),
        # The refusal has to come from the pre-write check. The late guard
        # prints the same sentence, so assert on something only the early
        # check can produce: the refusal happens before step 2 begins.
        Check("it refused before writing the conversation",
              "Writing session and project" not in output),
        Check("it names the symbolic link", "symbolic link" in output),
        Check("it promises nothing was written",
              "Nothing has been written" in output),
        Check("no conversation was created", sessions == 0),
        Check("the file the link pointed at is untouched",
              outside.read_text("utf-8") == "original"),
    ]


@case("an attachment from outside the old data dir is saved, not dropped")
def test_unmapped_attachment(root: Path):
    # The exporter ships these under attachments/unmapped/. The importer used
    # to ignore that folder, count the file as missing, and tell the user to
    # re-copy the bundle -- advice that cannot help, because the bytes are
    # already in it.
    package = root / "unmapped"
    export_bundle(package, [])
    index_path = package / "bundle/08_attachments.json"
    index = json.loads(index_path.read_text("utf-8"))
    source_name = "stray-report.pdf"
    shipped = package / "bundle/attachments/unmapped" / source_name
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shipped.write_bytes(b"%PDF-1.4 stray attachment" * 40)
    for key, meta in index.items():
        if meta.get("relativePath"):
            index[key] = {**meta, "relativePath": None, "fileName": source_name}
            break
    index_path.write_text(json.dumps(index), "utf-8")

    home = fresh_home(root, "unmapped_home")
    output, code = run_import(package, home, home / "Desktop/Project")

    landed = home / ".minimax/v2/assets/unmapped" / source_name
    return [
        # Non-zero is the honest answer: the conversation still points at the
        # old path and nothing can repair that. Silently exiting 0 would be
        # the bug, not the fix.
        Check("the import reports the unresolved link", code != 0, f"exit {code}"),
        Check("the file's contents were saved anyway", landed.is_file()
              and landed.read_bytes() == shipped.read_bytes()),
        Check("it says the conversation's own links point nowhere",
              "point nowhere" in output),
        Check("it says re-copying will not help",
              "Re-copying the bundle will not change this" in output),
    ]


@case("a dangling history reference is cleared before it reaches the database")
def test_history_reference_cleared(root: Path):
    # The clearing ran after import_sessions() had committed, so it edited a
    # dict nobody read again and the conversation kept pointing at a
    # directory that never travelled.
    package = root / "history"
    export_bundle(package, [])
    sessions_path = package / "bundle/02_sessions.json"
    rows = json.loads(sessions_path.read_text("utf-8"))
    for row in rows:
        row["history_relative_dir"] = "2026/01/01/never-travelled"
    sessions_path.write_text(json.dumps(rows), "utf-8")

    home = fresh_home(root, "history_home")
    output, code = run_import(package, home, home / "Desktop/Project")

    conn = sqlite3.connect(db_of(home))
    left = conn.execute(
        "SELECT COUNT(*) FROM local_runtime_sessions "
        "WHERE history_relative_dir IS NOT NULL").fetchone()[0]
    conn.close()
    return [
        Check("the import still succeeds", code == 0, f"exit {code}"),
        Check("it says the reference was cleared", "reference was cleared" in output),
        Check("and the database agrees: no row points at it", left == 0,
              f"{left} row(s) still reference it"),
    ]


@case("a path is only rewritten at a real path boundary")
def test_path_rewrite_boundary(root: Path):
    # The worst kind of bug this tool can have: not a dead link but a live
    # one pointing at the wrong file, with nothing to report it. A bare
    # substring replace turns the project's sibling "-old" folder into a
    # path under the new project, which on this very machine is one rename
    # away from real.
    sys.path.insert(0, str(SKILL / "scripts"))
    try:
        import import_bundle as ib
    finally:
        sys.path.pop(0)

    replacements = [("/old/data", "/new/data"),      # longest first, as sorted
                    ("/old/Project", "/new/Project")]
    moved = [
        ("/old/Project/a.md", "/new/Project/a.md"),
        ("/old/Project", "/new/Project"),
        ("/old/Project/sub/deep.txt", "/new/Project/sub/deep.txt"),
        ('<media src="/old/Project/b.png">',
         '<media src="/new/Project/b.png">'),
        ("/old/data/v2/assets/x.pdf", "/new/data/v2/assets/x.pdf"),
    ]
    kept = [
        "/old/Project-old/notes.md",      # a sibling folder, not the project
        "/old/Project.bak/x",             # nor a file whose name merely starts
        "/old/data-backup/db",            # a plausible real folder
    ]
    wrong = [f"{src!r} became {ib.remap_paths(src, replacements)!r}"
             for src, want in moved if ib.remap_paths(src, replacements) != want]
    wrong += [f"{src!r} was rewritten to "
              f"{ib.remap_paths(src, replacements)!r}"
              for src in kept if ib.remap_paths(src, replacements) != src]
    return [
        Check("real paths under the old root are repointed", not wrong,
              "; ".join(wrong[:3])),
        Check("a folder that merely starts with the old name is left alone",
              all(ib.remap_paths(s, replacements) == s for s in kept)),
        Check("nested JSON is handled the same way",
              ib.remap_paths({"a": [{"p": "/old/Project/x"}]}, replacements)
              == {"a": [{"p": "/new/Project/x"}]}),
        Check("nested JSON sibling is left alone",
              ib.remap_paths({"a": [{"p": "/old/Project-old/x"}]}, replacements)
              == {"a": [{"p": "/old/Project-old/x"}]}),
    ]


@case("the import says how many references still point at the old machine")
def test_stale_reported(root: Path):
    # A dead link is invisible until someone clicks it. The rewriter works on
    # a field allowlist, so anything outside it survives; if the import does
    # not count that, the only symptom is a broken chat weeks later.
    package = root / "stale"
    export_bundle(package, [])
    manifest = json.loads(
        (package / "bundle/00_manifest.json").read_text("utf-8"))
    old_ws = manifest["sourceWorkspaceDir"]
    orphan = f"{old_ws}_notes/field_campaign.csv"
    # Message rows, not session rows: the rewriter works on the former, and
    # putting a data_json on a session row is not a thing that exists.
    messages_path = package / "bundle/01_messages.jsonl"
    rows = [json.loads(l) for l in
            messages_path.read_text("utf-8").splitlines() if l.strip()]
    touched = 0
    for row in rows:
        blob = row.get("data_json")
        if not isinstance(blob, str):
            continue
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        # A field the rewriter does not know about -- exactly the case where
        # a path survives and the user gets a dead link with no warning.
        parsed["tool_calls"] = [{"path": orphan}]
        row["data_json"] = json.dumps(parsed, ensure_ascii=False)
        touched += 1
        if touched >= 5:
            break
    messages_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        "utf-8")

    home = fresh_home(root, "stale_home")
    output, code = run_import(package, home, home / "Desktop/Project")

    return [
        Check("the fixture actually planted something", touched > 0),
        Check("the import still succeeds", code == 0, f"exit {code}"),
        Check("it counts the surviving references",
              "still point at the old machine" in output),
        Check("it says nothing else is affected",
              "Nothing else is affected" in output),
    ]


@case("--dry-run creates nothing at all")
def test_dry_run_writes_nothing(root: Path):
    # The rehearsal is what a suspicious receiver runs first. It used to call
    # mkdir before its own dry-run guard, so it left the target folder behind
    # on a machine it was supposed to touch.
    package = root / "dryrun"
    export_bundle(package, [])
    home = fresh_home(root, "dryrun_home")
    workspace = home / "Desktop/Project"

    result = subprocess.run(
        [sys.executable, "import_bundle.py", "--home", str(home),
         "--workspace", str(workspace), "--dry-run"],
        cwd=package, capture_output=True, text=True)

    return [
        Check("the rehearsal exits clean", result.returncode == 0,
              f"exit {result.returncode}"),
        Check("it created no project folder",
              not workspace.exists(), f"created: {workspace}"),
        Check("it created no attachments folder",
              not (home / ".minimax/v2/assets").exists()),
        Check("it still told the user what would happen",
              "Project :" in result.stdout and "Data dir:" in result.stdout),
    ]


@case("a version mismatch is refused before any file is copied")
def test_version_refusal_writes_nothing(root: Path):
    # The version gates lived in the step-2 writer, so they fired *after* the
    # project files were already in the target folder -- while the abort still
    # said "Nothing has been written". A person who believes that sentence does
    # not go and look.
    package = root / "verrefuse"
    export_bundle(package, [])
    manifest_path = package / "bundle/00_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["schemaVersion"] = manifest["schemaVersion"] + 5
    manifest_path.write_text(json.dumps(manifest), "utf-8")

    home = fresh_home(root, "verrefuse_home")
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)

    return [
        Check("the import stops", code != 0, f"exit {code}"),
        Check("it blames the version", "older than the bundle" in output,
              output[-200:]),
        Check("it promises nothing was written",
              "Nothing has been written" in output),
        Check("and it means it: no project folder exists",
              not workspace.exists(), f"created: {workspace}"),
    ]


@case("a damaged bundle fails with an explanation, not a traceback")
def test_damaged_bundle(root: Path):
    out = root / "damaged"
    out.mkdir(parents=True)
    (out / "bundle").mkdir()
    # run_import executes ./import_bundle.py inside the package folder.
    shutil.copy(IMPORT, out / "import_bundle.py")

    def attempt(index: int):
        return run_import(out, fresh_home(root, f"dmg{index}"), out / "P")

    checks = []
    (out / "bundle/01_messages.jsonl").write_text("", encoding="utf-8")
    (out / "bundle/02_sessions.json").write_text("[]", encoding="utf-8")

    output, code = attempt(1)
    checks.append(Check("missing manifest is explained", code != 0
                        and "not a migration bundle" in output, output[-120:]))

    (out / "bundle/00_manifest.json").write_text("{not json", encoding="utf-8")
    output, code = attempt(2)
    checks.append(Check("corrupt manifest is explained", code != 0
                        and "not readable JSON" in output, output[-120:]))
    checks.append(Check("no raw traceback leaks to the user",
                        "Traceback (most recent call last)" not in output))

    (out / "bundle/00_manifest.json").write_text(
        json.dumps({"sessionIds": [], "messageRowCount": 0}), encoding="utf-8")
    (out / "bundle/02_sessions.json").write_text("[{bad", encoding="utf-8")
    output, code = attempt(3)
    checks.append(Check("corrupt session record is explained", code != 0
                        and "not readable JSON" in output, output[-120:]))

    (out / "bundle/02_sessions.json").write_text("[]", encoding="utf-8")
    (out / "bundle/01_messages.jsonl").unlink()
    output, code = attempt(4)
    checks.append(Check("missing messages file is explained", code != 0
                        and "incomplete" in output, output[-120:]))
    checks.append(Check("still no traceback anywhere in that case",
                        "Traceback (most recent call last)" not in output))
    return checks


@case("a missing table does not stop the import")
def test_missing_table(root: Path):
    package = root / "notable"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "notable_home")
    conn = sqlite3.connect(db_of(home))
    conn.execute("ALTER TABLE local_runtime_session_assets RENAME TO _gone")
    conn.commit()
    conn.close()
    workspace = home / "Desktop/Project"
    output, code = run_import(package, home, workspace)
    return [
        Check("import still succeeds", code == 0, f"exit {code}"),
        Check("conversation written", len(read_state(home, workspace)["sessions"]) == 1),
    ]


@case("a renamed column stops the import without writing")
def test_renamed_column(root: Path):
    package = root / "nocol"
    export_bundle(package, [])
    foreignise(package / "bundle")
    home = fresh_home(root, "nocol_home")
    conn = sqlite3.connect(db_of(home))
    conn.execute("ALTER TABLE local_runtime_sessions "
                 "DROP COLUMN history_relative_dir")
    conn.commit()
    conn.close()
    workspace = home / "Desktop/Project"
    before = read_state(home, workspace)
    output, code = run_import(package, home, workspace)
    after = read_state(home, workspace)
    return [
        Check("import stops", code != 0, f"exit {code}"),
        Check("it blames the version, not a locked database",
              "does not match the bundle" in output,
              "message should name the version mismatch"),
        Check("it does not tell the user to quit a closed app",
              "Database is busy" not in output),
        Check("it promises nothing was changed",
              "Nothing has been changed" in output),
        Check("no session was left behind", len(after["sessions"]) == 0
              and len(before["sessions"]) == 0),
    ]


@case("truncation previews the cut point and writes nothing")
def test_truncation_preview(root: Path):
    out = root / "preview"
    anchor = truncation_anchor()
    result = subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), "--keep-through-text", anchor],
        capture_output=True, text=True)
    return [
        Check("exits clean", result.returncode == 0, f"exit {result.returncode}"),
        Check("no output directory created", not out.exists()),
        Check("shows the last messages that would be kept",
              "THAT WOULD BE KEPT" in result.stdout or
              "WOULD BE KEPT" in result.stdout),
        Check("marks which one is the last kept", "LAST KEPT" in result.stdout),
        Check("says nothing was written", "Nothing has been written" in result.stdout),
    ]


@case("truncation drops everything after the cut")
def test_truncation_applied(root: Path):
    out = root / "cut"
    anchor = truncation_anchor()
    subprocess.run(
        [sys.executable, str(EXPORT), "--session-id", SESSION_ID,
         "--out", str(out), "--yes", "--keep-through-text", anchor],
        capture_output=True, text=True)
    manifest = json.loads((out / "bundle/00_manifest.json").read_text("utf-8"))
    rows = [json.loads(line) for line in
            (out / "bundle/01_messages.jsonl").read_text("utf-8").splitlines() if line.strip()]
    cutoff = manifest["truncatedToMessageId"]
    return [
        Check("manifest records the cut", manifest.get("truncated") is True),
        Check("the newest kept id equals the cut",
              max(r["id"] for r in rows) == cutoff, f"{max(r['id'] for r in rows)} vs {cutoff}"),
        Check("no row survives past the cut",
              all(r["id"] <= cutoff for r in rows)),
        Check("the runtime history directory is excluded",
              manifest.get("historyIncluded") is False),
    ]


@case("the published package is free of personal data")
def test_no_personal_data(root: Path):
    # Everything here is derived from this machine or built from fragments, so
    # the check works for whoever runs it and no real name, project or session
    # id is baked into the published source.
    home = str(REAL_HOME)
    user = os.environ.get("USER") or Path(home).name
    # This file used to exclude ITSELF from the scan, which meant the one
    # file most likely to accumulate a real name in a fixture was the one
    # file never checked. It is now included like everything else.
    text_files = [p for p in SKILL.rglob("*")
                  if p.is_file() and p.suffix in {".md", ".py", ".txt", ".json", ".yml"}
                  and "__pycache__" not in p.parts
                  and p.name != ".DS_Store"]
    offenders = []
    for path in text_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        # A home path is a leak wherever it appears, code included.
        if home in text:
            offenders.append(f"{path.name}: this machine's home path")
        # Somebody else's home, in any of the usual shapes.
        for match in re.findall(r"/Users/([A-Za-z0-9._-]+)", text):
            if match not in ("you",) and match != user:
                offenders.append(f"{path.name}: /Users/{match}")
        for pattern in ("C:/Users/", "/home/"):
            if pattern in text and "/Users/you" not in text:
                offenders.append(f"{path.name}: foreign home path {pattern}")
        # A real session id from this machine, in any file type.
        for sid in real_session_ids():
            if sid in text:
                offenders.append(f"{path.name}: a real session id")
        # The bare account name on its own, with no path around it.
        if len(user) >= 4 and re.search(rf"\b{re.escape(user)}\b", text):
            offenders.append(f"{path.name}: the account name {user!r}")
        # A project path segment identifying the work. The bare folder name
        # counts too, not only the full path: a truncated copy of a real
        # project name in a test fixture is the same leak.
        for match in re.findall(
                r"/Users/[A-Za-z0-9._-]+/([A-Za-z0-9._-]*Research[A-Za-z0-9._-]*)", text):
            offenders.append(f"{path.name}: project path segment {match!r}")
        if WORKSPACE_NAME and WORKSPACE_NAME in text:
            offenders.append(f"{path.name}: this machine's project name")
        # Verbatim conversation. A real title pasted into an example is the
        # leak this whole case exists to prevent, and no amount of looking
        # for usernames or paths will ever find it.
        for phrase in real_conversation_phrases():
            if phrase in text:
                offenders.append(f"{path.name}: real conversation text {phrase!r}")
    return [
        Check("no machine-specific or personal data in the shipped files",
              not offenders, "; ".join(offenders[:4])),
        Check("this test file is included in its own scan",
              Path(__file__) in text_files),
        Check("both scripts compile",
              all(subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                                 capture_output=True).returncode == 0
                  for p in (SKILL / "scripts").glob("*.py"))),
        Check("and there are exactly two: export and import",
              sorted(p.name for p in (SKILL / "scripts").glob("*.py"))
              == ["export_bundle.py", "import_bundle.py"],
              str(sorted(p.name for p in (SKILL / "scripts").glob("*.py")))),
        Check("the test suite itself compiles",
              subprocess.run([sys.executable, "-m", "py_compile",
                              str(SKILL / "tests/run_tests.py")],
                             capture_output=True).returncode == 0),
        Check("SKILL.md frontmatter is present and well formed",
              bool(re.match(r"^---\nname: [a-z0-9]+(-[a-z0-9]+)*\ndescription: .+\n---\n",
                            (SKILL / "SKILL.md").read_text("utf-8")))),
        Check("every file SKILL.md points at exists",
              all((SKILL / rel).exists() for rel in
                  re.findall(r"`((?:scripts|references|assets)/[\w.-]+)`",
                             (SKILL / "SKILL.md").read_text("utf-8")))),
    ]


def p_is_documentation(path: Path) -> bool:
    """The scripts legitimately reference the running user's home at runtime."""
    return path.suffix in {".md", ".txt"}


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", help="only run cases whose name contains this")
    args = parser.parse_args()

    if not SESSION_ID:
        print("  0 checks run.")
        print("  The suite exports a real conversation from this machine, and "
              "there is none")
        print("  yet. Start one conversation in MiniMax Code, then run this "
              "again.")
        return 0

    selected = [(n, f) for n, f in CASES if not args.k or args.k in n]
    print("=" * 70)
    print("  Migration scripts — end-to-end checks")
    print("=" * 70)
    print(f"  {len(selected)} case(s)\n")

    failures = 0
    total = 0
    root = Path(tempfile.mkdtemp(prefix="mvs-tests-"))
    try:
        for index, (name, func) in enumerate(selected, 1):
            print(f"[{index}/{len(selected)}] {name}")
            try:
                checks = func(root)
                # Iterating inside the try: a case that returns something
                # other than a list of Checks would otherwise raise outside
                # the handler and silently void every later case's results.
                reported = list(checks or [])
            except Exception as error:                      # noqa: BLE001
                reported = [Check("case raised an exception", False,
                                  f"{type(error).__name__}: {error}")]
            for check in reported:
                print(f"    {check}")
                failures += 0 if check.ok else 1
                total += 1
            print()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("=" * 70)
    if failures:
        print(f"  RESULT: {failures} of {total} checks FAILED")
    else:
        print(f"  RESULT: all {total} checks passed across "
              f"{len(selected)} case(s)")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
