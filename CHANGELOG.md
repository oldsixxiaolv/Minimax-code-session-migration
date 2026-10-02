# Changelog

All notable changes to this skill. Dates are the day the change landed.

## 1.0

First public release. What it does, what it refuses to guess at, and what has
not been tested on this page are all in `README.md`; the short version is that
it moves a MiniMax Code conversation between computers, and that its only
supported configuration is the one it has been run on.

## 0.3.1 — the verifier is gone

- **`verify_bundle.py` removed.** The project ships two scripts now:
  `export_bundle.py` and `import_bundle.py`. The export is its own check — it
  names the folder it wrote, every subdirectory it left behind, and whether it
  finished cleanly — and a third program is one more thing that can drift from
  what the documentation says. The two cases that only tested the verifier now
  test the properties it checked, at the point where they are acted on: a bad
  columnar version and a bundle carrying a symbolic link are both refused by
  the importer, before anything is written. The remaining bundle assertions
  moved into a small in-process helper in the suite rather than being deleted.

## 0.3.0 — documentation and the defects the rewrite exposed

- **`--dry-run` no longer creates the target folder.** `mkdir` ran before its
  own dry-run guard, so the rehearsal a suspicious receiver runs first left a
  directory behind on a machine it was supposed to touch.
- **A version mismatch and a missing FTS5 are now caught before the first file
  is copied.** Both checks sat in the step-2 writer, which runs after step 1
  has already put the project into the target folder — while the refusal still
  said "Nothing has been written". That is the sentence a person trusts and
  then does not restore the backup over. A regression test now holds each of
  them: disabling the preflight makes the project folder appear while the
  message still claims otherwise.
- **The one question's answer is cleaned up.** Shell quotes became part of the
  folder name, a bare name resolved against the bundle folder, and `~` scattered
  the project loose in the home directory. All three are things people type.
- **`verify_bundle.py` warns again about subdirectories instead of failing.**
  Failing turned the documented export-then-verify flow red for every project
  that has a subfolder at all, which is nearly all of them. A symlinked or
  unreadable directory still fails, because those are files the user asked for.
  The result line now names the warning count, so a warning is not read as a
  pass.
- **`README.md` and `SKILL.md` rewritten.** A four-agent audit found thirteen
  defects between them, including three that would actively mislead: a claim
  that a truncated export drops the memory file (it does not), a claim that the
  two machines must run the same version (only an *older* destination is
  refused), and an exit-code rule contradicted by a third failure banner the
  document never mentioned. The agent-facing file was missing the gates that
  matter most: which flag a single-chat request means, that an output folder is
  replaced in place, and what to tell the user at the end.
- **`assets/BUNDLE_README.md` cut from 156 to 130 lines** and now leads with
  what the receiver is being asked to install on their own machine — the
  sender's profile, in cleartext, replacing theirs.

- **A cut is now an exact boundary.** The filter is on the timestamp, which is
  right — a project's branches have interleaved id ranges, so an id cut would
  keep rows from a later point in time in a different branch. But two messages
  in one conversation can share a millisecond, and the one *after* the anchor
  travelled into the bundle. The live session has such a pair; cutting at
  id=2401 kept 2402 as well. Rows in the anchor's own conversation are now
  also bounded by its id.
- **A failed cut no longer leaves an empty folder that looks like a bundle.**
  The snapshot needs a directory, and the three error paths returned before the
  cleanup that the preview path already had.
- **"Through 08:22" now includes 08:22**, rather than cutting at 08:22:00 and
  silently dropping the rest of the minute the user named.

- **Attachments are copied atomically.** `shutil.copy2` is not: a process
  killed mid-copy leaves a half-written file at the real destination name. It
  exists, it opens, and the next import overwrites it without ever mentioning
  that the earlier one was interrupted. The copy now lands under a temporary
  name and is renamed into place, so a reader sees the old complete file or
  the new one, never a splice of the two.
- **Two imports into one data directory are now refused.** There was no
  mutual exclusion, so two imports interleaved writes over the same
  destinations and the loser reported a perfectly good bundle as damaged —
  advice that costs a 300 MB re-transfer. A lock file now names the running
  process, and a lock left by a killed run is cleared on sight.
- **The runtime history's top-level files are restored at all.** Only
  subdirectories were walked, so `messages.jsonl`, `manifest.json` and
  `llm-call.json` — about 1.3 MB — were dropped on every import, silently. A
  half-copied history directory is now replaced rather than skipped, so an
  interrupted run gets repaired instead of accepted for ever.
- **Project files that already exist are named before they are overwritten.**
  Memory files are backed up; these were not, and the local edit they
  destroyed was the only copy.
- **Backups are pruned to the newest five.** Each is a full copy of the
  session database and nothing ever removed them; three imports left 354 MB
  growing without bound.

- **A path is rewritten only at a real path boundary.** A bare substring
  replace turned the project's sibling `-old` folder into a path under the new
  project, and a `.minimax-backup` sibling into the new data directory. That
  is worse than a dead link: it is a live link pointing at the wrong file,
  with nothing to report it.
- **The import now counts the references that still point at the old machine.**
  The rewriter works on a field allowlist, so a path in any other field
  survives; the only symptom used to be a dead card weeks later.

- **A real conversation title was being published.** `references/schema-notes.md`
  carried a verbatim title from a real session as a tokenisation example. An
  earlier round had moved this out of `README.md` rather than deleting it, and
  the guard test reported `[PASS]` over it. Removed, and the guard rebuilt: it
  had excluded **its own source file** from the scan — the one file most likely
  to accumulate a real name in a fixture — and could only look for usernames,
  paths and ids, which is the wrong class entirely for a leaked phrase. It now
  samples real titles and distinctive phrases out of the live database and
  matches them against every shipped file, including its own. Verified by
  injecting a known real title and watching the check go red.
- **Two test fixtures held a real project name and a real date.** Both were
  privacy leaks and both made the suite silently skip on any other machine.
  The project name and the cut timestamp are now derived from the data at
  runtime, which removes the leak and the portability bug together.
- Stale `__pycache__` (which embeds the account name in `co_filename`),
  `.DS_Store`, and the old project name in `LICENSE` are gone. A `.gitignore`
  only protects a git checkout — anyone zipping or copying the folder by hand
  was shipping them.

- **The exporter no longer opens the live database for writing.** It used a
  plain `sqlite3.connect()`, which on a WAL database is a read-write handle
  that can run recovery — a write. Reading somebody's live database must never
  be able to modify it. The test suite had the same problem in four places,
  including one that runs at import time on every invocation.
- **A symlink on any intermediate path component is now caught before the
  write.** Checking only the leaf and the assets root let a link on
  `v2/assets/2026` through, and the refusal then happened after the
  conversation was committed and an existing copy purged.
- **A read-only database is caught before the first file is copied.** The probe
  took and released a write lock, but an empty transaction writes no page, so
  `chmod 0444` passed it and the failure surfaced at step 2 with the project
  files already on disk.
- **A partially copied bundle can no longer leave a dangling history
  reference.** The manifest's `historyIncluded` was believed; the directory's
  actual presence is now checked, the way the memory path already was.
- **Parked `unmapped/` attachments are size-checked too.** They were the only
  attachments the audit skipped, which made them the only ones the fix exists
  to save and never verified.

- **A bundle that leaves out project subdirectories now fails verification.**
  It was a warning, and warnings do not affect the exit code, so a bundle with
  none of a project's source in it exited 0 and printed "bundle looks good" —
  the same silent loss the warning was added to end. A missing project
  directory also aborted the export, instead of warning and exiting 0.
- **"Recursive" is no longer assumed to be complete.** `os.walk` does not
  follow directory symlinks and swallows read errors, so a linked subtree or an
  unreadable directory vanished with no report. Both are now named. A
  directory skipped by the `_migration` name rule stays a warning, because
  almost every project has a `.venv` and failing on that would make the tool
  unusable.

- **A locked database no longer aborts after the project files are already on
  disk.** The importer copied the project at step 1 and wrote the database at
  step 2, so `Database is busy` — the single most common failure — arrived with
  files scattered in the target folder and a message saying "nothing has been
  written". A user who believes that does not go looking for them. It now
  takes and releases a write lock before the first file moves.
- **A symlink already sitting at the destination path is refused before the
  write, not after.** Same shape of lie: the check ran in the attachment step,
  by which point the conversation was committed.
- **Attachments that live outside the old data directory are now saved, not
  dropped.** The exporter ships their bytes under `attachments/unmapped/`; the
  importer ignored that folder, counted them as missing, and told the user to
  re-copy the bundle — advice that could never help, because the bytes were
  already in it. They are restored under `unmapped/` now, and the report says
  the conversation's own links to them cannot be repaired.
- **Clearing a dangling `history_relative_dir` now actually reaches the
  database.** It ran after `import_sessions()` had already committed, so it
  edited a dict nobody read again. A multi-conversation bundle kept 42
  references to a directory that never travelled. The decision is made before
  the write.
- **A bundle that leaves out project subdirectories says so.** The export
  printed a clean summary and `verify_bundle.py` reported all green while
  every subdirectory stayed behind. Both now name what was dropped; a project
  whose source lives in folders could be handed over with none of it in.
- **`--list` shows every conversation.** The no-argument listing showed three
  per project, so a project with dozens of conversations hid the one you
  wanted and there was no way to see the rest.

- **`N/N restored intact` is only printed when something was compared.** With
  an index recording no usable size, the tool verified nothing and certified
  it anyway. The line now reports how many files were actually compared, says
  plainly when that was none, and the exit code stops being 0.
- **A size mismatch reports exact bytes, not rounded megabytes.** `1.0 MB of
  1.0 MB` was the tool's own rounding hiding a 1000-byte difference, and
  `0.0 MB of 0.0 MB` for anything small. It also said "incomplete" for files
  *larger* than recorded, and truncated the list at three with no overflow
  line. All three fixed.
- **The audit reads `source_context_json` as well as `data_json`.** The
  rewriter repoints paths in both, so a path that existed only in the second
  was moved onto the new machine and then never checked.
- The test suite opens the real install read-only. A plain `connect()` let a
  test run modify the database it was reading, and blocked an independent
  auditor from running the suite at all.

- **The half-copied-attachment check actually ran.** The comparison was built
  but called with a `data_dir` that only existed inside `main()`, so it raised
  on the first record and took every import down with it. A bare `except
  AttributeError` around the index loader is what hid this: a check that
  silently stops checking is indistinguishable from one that never existed.
  The loader is now a named function that returns the reason it could not arm
  itself, and that reason is printed.
- **A short file and an absent file are reported as different problems.** Both
  used to print as `missing`, which sent the user hunting for a file that was
  sitting right there at half its size. Now a short file is named with the size
  that arrived against the size expected, and the advice is to re-copy.
- Two cases cover this: one halves an attachment and requires the import to
  fail and name it as incomplete, the other requires a clean bundle to prove
  the size check was armed rather than merely quiet. Both were confirmed to
  fail when the check is removed.

- **The exporter quotes its database path like the importer already did.** A
  data directory containing `?` or `#` truncated the `file:` URI, and the
  truncated path could be a different, valid database — which was then bundled
  with exit 0. The two scripts disagreed about the same line of code.

- **Replacing a conversation the app is using is announced.** A held lock or an
  unfinished turn meant the purge silently removed the mutual exclusion while
  the owning background task still said "running", so a second turn could start
  on the same rows. It now marks the line `[IN USE]`, says what will be lost,
  and names the backup that can undo it. The check runs *before* the purge,
  which is what removed the evidence it needs.
- **A `--keep-through-text` matching more than one message no longer crashes.**
  The cut became time-based, and the multi-match branch still referenced the
  variable that change removed.
- **An attachment file name can no longer write outside the output directory.**
  A name containing `../` landed outside the bundle while the export reported
  success.
- `--out` problems and an odd project file (a fifo, a socket, a mode-000 file)
  still produce a raw traceback rather than a sentence.

- **A failed export no longer leaves a copy of the session database behind.**
  Cleanup was spread across ten explicit calls, so any unhandled error skipped
  it — leaving ~93 MB of snapshot in the very folder the user was told to copy
  to the other machine. The write phase now runs under a `finally`.
- **The verifier no longer rejects a bundle cut at the user's first message.**
  Having no assistant turn is the expected result of that cut, not a fault; it
  now only insists on both turns for a complete conversation.
- Replacing a conversation that is in use now checks the lock **before** the
  purge — the purge removes the rows that would have said so.

- **A half-copied attachment is caught instead of reported as success.** The
  self-check asked only whether the file existed, so a copy interrupted
  part-way — a PDF at half its size, no footer, unopenable — came back as
  `24/24 resolve` and `Import complete`. The size recorded in the bundle is now
  compared against what landed.
- An interrupted export no longer leaves ~93 MB of session-database snapshot in
  the folder the user was told to copy, and a bundle cut at the user's very
  first message no longer fails verification for having no assistant turn yet.

## 0.2.6

- **An export says so when an attachment is gone.** A file the conversation
  refers to but this machine no longer has was dropped silently, and the export
  still printed "Done" with exit 0. It now reports the count and exits non-zero.
  The count was structurally always zero: the collection step discarded
  non-existent paths before anything could count them.
- **A leftover snapshot no longer bricks a directory.** The snapshot is written
  into an existing file rather than replacing it, so a `snapshot.sqlite` left by
  a killed or out-of-space run made every later export into that directory fail
  for good, with an error that never mentioned the cause.
- The database backup is written under a `.partial` name and renamed only after
  it verifies, so an interrupted backup can never be mistaken for the "newest
  backup" the undo instructions point at.
- Two imports in the same second no longer overwrite each other's backup; the
  timestamp carries microseconds.
- Permission and disk-space failures no longer tell you to quit MiniMax Code,
  which could never help.
- The history line no longer claims a truncated export when the conversation
  simply has no history directory.

## 0.2.5

- **A cut is applied by time, never by row id.** Ids are only monotonic inside
  one conversation, and a project can hold several branches whose id ranges
  interleave — cutting on an id kept later messages from another branch, so a
  bundle meant to end on 28 September contained 79 messages from 1 October.
  Every cut flag now resolves to a timestamp and filters on that. A time cut
  across a three-branch project now keeps all three branches' 471 pre-cut
  messages instead of one branch and 79 leaked rows.
- **Pressing Enter now uses the project's own name** for the destination
  folder, instead of a folder called `Project`.
- **The format-version guard now checks the value it writes.** It compared the
  manifest's `columnarVersion` and then wrote the row's own value, so a row at
  an unknown version passed the check and was written — and then never joined a
  project. The rows themselves are now checked.
- **`sourceAssetsDirs` is guarded like the other roots.** It feeds the same
  path-replacement list, and a `/` in it rewrote every slash in every stored
  path, silently.
- **The manifest's project folder is no longer taken literally.** A
  `projectSnapshotDir` that was absolute or contained `../` made the importer
  read arbitrary directories off the receiving machine into the project
  folder — silently, with exit 0. It is now refused before anything is written.
- An audit that found no conversations to check no longer aborts claiming
  "nothing has been written" after the project files and the memory had already
  been written. The side effect is real, so the message is gone.
- A symbolic link on a bundle *directory* (`attachments/`, `memory/`) is now
  refused. The containment check only looked at the last path component, so a
  link on the base resolved both sides into the same real directory and passed,
  which could copy a private key or auth token out of the receiving machine.

## 0.2.4

- **The project snapshot is checked in the preflight too.** A symbolic link
  there was refused only while copying, after earlier files had already landed
  in the target folder — while the tool still said nothing had been written.
- The documentation described the database backup as three per-file copies of
  a live WAL database. It is one file written with SQLite's backup API, and
  undoing it means removing the `-wal` / `-shm` beside the restored copy.

## 0.2.3

- **Every path in a bundle is checked before anything is written.** Previously
  a hostile `relativePath` was refused at step [3/4], by which point the
  conversation was already committed and "nothing has been written" was false.
  The same check now runs first, which also makes `--dry-run` meaningful on a
  doctored bundle.
- **Symbolic links are refused everywhere, not just where they were
  expected.** The digest and the history directory were still followed, so a
  bundle could pull a file off the receiving machine into the install — and
  because it lands in the data directory, the next export shipped it onward.
- **The verifier reports links.** It previously certified a bundle carrying
  them, rejecting broken ones only as a side effect of a failed read.
- The importer installs the project folder the manifest names, instead of a
  default that could differ from what the verifier inspected.
- A manifest claiming a source root of `/` is refused rather than rewriting
  every stored path.
- The attachment audit checks the conversations that were actually written
  rather than the ids the manifest claims, so a shifted list can no longer
  produce a false all-clear.


## 0.2.2

- **`--workspace` with a truncation no longer crashes.** The SQL placeholders
  were built before truncating shortened the conversation list, so the statement
  asked for more bindings than were supplied. Found by having a fresh agent
  follow the documentation on a real request.
- **Conversations dropped by a cut are named.** A conversation with nothing at
  or before the cut disappears; previously only a row count was reported, so a
  project of twelve could silently become one.
- **A truncated export no longer bundles the memory file.** It is a live
  snapshot of the profile and cannot be cut at a point in time, so including it
  carried knowledge from after the cut — the opposite of what truncating is
  for. Untruncated exports are unaffected.
- A preview that finds no match no longer leaves an empty output directory
  behind, and the verifier no longer warns when there is nothing to warn about.
- `--project-recursive` no longer promises a "much larger bundle"; it says what
  you lose without it.


## 0.2.1

- **The destination is asked, not assumed.** The columnar-version guard checked
  the bundle against a constant, so importing into a machine whose runtime had
  moved on wrote a session that its project trigger would never attach. The
  expected version is now read out of the destination's own trigger SQL.
- **A default-workspace conversation no longer lands in an empty project.** A
  session the app had flagged as belonging to its own default workspace was
  matched against `project_kind='default'`, missed, and filed under a project
  with no folder, leaving a stray entry in the sidebar. The flag is cleared on
  import, because the import always means "the folder the user named".
- **The NOT NULL guard actually binds something.** It filtered the values but
  still sent an explicit NULL for every filtered column, which defeats a
  `NOT NULL DEFAULT`. The insert now lists only the columns it has values for.
- **A path that escapes its directory is refused, not downgraded.** It was
  counted as a missing file and the import continued, so the check that looked
  like it was guarding against it never fired.
- **The database path is quoted.** A `#` or `?` in it truncated the URI used to
  open the backup source, and the "verified" backup contained zero tables. A
  Windows path did not open at all.
- **The exporter refuses before it copies.** Running it with no arguments used
  to copy the whole database and print a progress line before declining.
- `--workspace` and `--session-id` help text now agrees with the README; the
  missing `[2/6]` step is printed; role counts read as English rather than a
  dict repr; `verify_bundle.py` travels inside the bundle so the documented
  self-check can actually be run; the troubleshooting tables use message strings
  the tools really print.


## 0.2.0

- **Bundle paths are validated.** A `relativePath` containing `../` used to
  write outside the attachments directory; symbolic links were followed, which
  could copy a file off the receiving machine into the install. Both are now
  refused, and `is_symlink()` is checked before `resolve()`, which would
  otherwise have silently undone the check.
- **The safety backup is a real database.** It was three `copy2` calls on a live
  WAL database, which can reassemble into something that passes
  `integrity_check` and is empty. Now uses SQLite's backup API and verifies the
  result before reporting success.
- **A truncating preview no longer destroys anything.** It removed the output
  directory while printing "Nothing has been written". It now touches nothing it
  did not create.
- **A half-copied bundle is refused.** Importing one used to produce a
  conversation that quietly stopped partway through; the manifest's message
  count is now checked before anything is written.
- **`--project-recursive` round-trips.** Subdirectories were bundled but
  silently dropped on import, and a valid recursive bundle failed its own
  verification.
- **Columnar version is checked on every session**, not just the first.
- **The runtime history directory is no longer left dangling.** It is now
  restored when bundled and the reference cleared when not.
- **A stale migration folder is no longer swept into a new bundle** — which had
  been carrying the previous bundle's `memory/user.md` while the manifest
  reported memory as excluded.
- Two fewer ways to get a traceback instead of an explanation: a damaged
  attachment index, and a manifest missing keys the verifier then subscripts.


## 0.1.0

- First public release: export, import, and a sender-side self-check.
- Truncating exports preview the cut point and write nothing without `--yes`.
- Matching ignores markdown decoration, so a passage quoted back still matches
  the message it came from, and every candidate is listed with a timestamp.
- Re-importing replaces rather than skips, clearing the conversation's messages,
  search index, and derived state together.
- Attachments and delivered-file links are repointed at the new machine, and
  the import verifies each one opens.
- The runtime's compaction summary ships as `09_context_digest.jsonl`, so a
  truncated export does not drop the context it had compressed away.
