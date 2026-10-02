---
name: minimax-session-transfermation
description: Move a MiniMax Code conversation, its attachments and its project to another computer, then keep chatting there. Use when the user says things like "把这个对话迁移到新电脑", "把这个 session 搬过去", "换个电脑继续这个项目", "把当前项目连同对话打包拷过去", "只迁移到某句话为止", or the English equivalents ("move this conversation to my other machine", "migrate this session", "export a session bundle", "truncate the conversation at ..."). Also when they hand you a migration bundle to import.
---

# Move a MiniMax Code conversation between computers

Run the scripts. Never write SQL against the session database, and never work
around a refusal by hand.

## Export

Start by finding the project name — this writes nothing and prints a
copy-pasteable line per project:

```bash
python3 scripts/export_bundle.py
```

Then export, using the `--workspace` value it printed:

```bash
python3 scripts/export_bundle.py --workspace 'my-project' --out ~/Desktop/mybundle
```

There is no separate verification step; **read the export's own output.** It
names what it wrote, what it left behind, and whether it finished cleanly.

Rules that change what you run:

- **"This conversation" means `--session-id`, not `--workspace`.** Use
  `--workspace` only when the user says project, 项目, or "everything". Passing
  it for a single-chat request silently exports every conversation in it.
- **`--workspace` is a substring match on the folder path.** If it matches
  several, the busiest wins and the exporter says so — check the name it
  printed is the one the user meant.
- **The listing shows 12 projects.** If theirs is missing, pass any unique
  fragment of the path instead.
- **Never reuse an existing `--out`.** An export replaces whatever bundle is
  already there, in place.
- An export is a snapshot. If the conversation continues, export again before
  they leave.
- **Check the export's exit code, and read its warnings.** Three lines decide
  whether the bundle is good to hand over:
  - `Done.` — complete.
  - `WARNING: N subdirector… did NOT go into the bundle` — tell the user, and
    offer to re-export with `--project-recursive` if those folders held work.
  - `Finished WITH PROBLEMS` — it exited 1: some attachments the conversation
    points at are no longer on this machine. The conversation is still
    complete; say so, and offer to carry those files across by hand.

## Cutting the conversation short

`--keep-through-text` / `--keep-through-id` / `--keep-through-time` all preview
the cut and **write nothing**.

**Run the preview as its own command, before you say anything about the cut,
and never put `--yes` in a first invocation.** Then show the user, verbatim:

- the message marked `<-- LAST KEPT`, in full — do not summarise it;
- the `truncated at id=…` line and the `keeping N, dropping M` counts;
- every `WARNING:` line above them.

If the user has already said yes, that is not the confirmation. Run the
preview, show it, and wait for a reply that comes after they have seen it. A cut
in the wrong place is easy to miss.

```bash
# 1. preview — safe, changes nothing
python3 scripts/export_bundle.py ... --keep-through-text '…'
# 2. only after they have seen it and agreed
python3 scripts/export_bundle.py ... --keep-through-text '…' --yes
```

- Use **single quotes**. `$_` is a shell variable and will expand.
- Matching ignores markdown, so a passage they quoted back still matches. The
  first 8 matches are listed with their ids and the **earliest** always wins — a
  phrase they read at the end often occurs earlier too. If the earliest is not
  the message they meant, use `--keep-through-id`.
- The cut is **inclusive**: the matching message is kept. For "everything
  *before* X", cut on the message before X.
- Times are local to this machine.

Say all four of these before they confirm:

- A conversation with nothing at or before the cut is **left out entirely**, and
  the exporter says how many. With `--workspace` that can mean most of the
  project.
- **The context digest is not bundled in a truncated export** — it summarises
  the runtime's own view and would carry content from after the cut. **Memory
  still is**, and it is the profile as it stands *now*, not as it stood at the
  cut. Say so, and pass `--no-memory` if they want the profile out.
- **The project snapshot is the folder as it is now**, not as it was at the cut.
- **Branch id ranges interleave**, which is why the cut filters on the
  timestamp and not the id. One cut spans every branch at that instant. For the
  whole project, export each conversation with `--session-id`.

## Import

Confirm on the **destination** first: MiniMax Code installed, **launched at
least once**, and **not older** than the source. A newer install is fine only
while its storage format is one the importer knows; it refuses rather than
guesses. The user must then quit it completely, including tray and background
processes.

```bash
cd mybundle && python3 import_bundle.py
```

The importer checks the bundle, the destination, and the database's writability
before copying anything, so a refusal leaves nothing behind. It writes the
conversation even when some files cannot be restored, naming them; **always
check the exit code** — `0` is the only success, and two failure banners begin
with the words `Import complete`.

Two things to warn them about before they import:

- **Re-importing replaces any conversation on the destination that shares an
  id, with no check on which copy is newer.** An old bundle overwrites one they
  have since continued. Tell them to import onto a machine that does not
  already hold that conversation, never over one that is open.
- **If the project folder they are asked for already holds work, same-named
  files are overwritten and are not backed up** (memory files are; project files
  are not). Tell them to pick a new folder, or move the existing one aside.

It rebuilds the search index, but MiniMax Code must be launched or relaunched
before the session list and search show it.

## What to tell them when you are done

Five lines, no more:

```
Bundle: <absolute path> — copy the whole folder to the other machine.
There: quit MiniMax Code, then  cd <folder> && python3 import_bundle.py
Check the exit code — only `Import complete` with 0 is a clean run.
Then launch MiniMax Code; the chat appears only after it starts.
Not included: project subdirectories (unless --project-recursive), files outside
the project, credentials, plugins, other projects' conversations.
```

## Flags

Normal runs use only these:

| Flag | Where | Purpose |
|---|---|---|
| `--workspace NAME` | export | the project; **every** conversation in it, substring-matched |
| `--session-id ID` | export | exactly one conversation |
| `--out DIR` | export | output folder |
| `--keep-through-text '…'` | export | cut here; previews only, writes nothing |
| `--keep-through-id N` | export | the same, by message id — use to disambiguate |
| `--yes` | export | write a truncated bundle, only after they agreed |
| `--project-recursive` | export | also include project subfolders |
| `--no-memory` | export | leave the profile out |

Everything else is in `--help`, and the two scripts do not share a flag set:

```bash
python3 scripts/export_bundle.py --help    # --list, --keep-through-time,
                                           # --project-dir, --data-dir
python3 scripts/import_bundle.py --help    # --dry-run, --non-interactive,
                                           # --home, --project-subdir
```

Do not pass one on your own judgement. The exception worth knowing is
`--dry-run`: a rehearsal that writes nothing, which is worth offering before an
import that will overwrite something.

## Why not to write SQL

Six things break, all silently, which is why these scripts exist. One thing you
can act on: **a refusal from the importer really does mean nothing was written**
— so never work around a refusal by hand. Detail: `README.md`.
