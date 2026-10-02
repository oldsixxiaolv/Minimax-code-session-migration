# MiniMax Code conversation migration bundle

This folder restores a MiniMax Code conversation, its attachments and a copy of
its project files onto this computer.

## Before you run anything

This is somebody's private material. It contains their conversation, their file
names and paths, and — unless they exported with `--no-memory` — their agent
profile (`bundle/memory/user.md`), which names them and records how they like to
work. It is **plain, unencrypted files**: anyone who has this folder can read all
of it.

- If you do not know the sender, or did not expect this, **delete the folder.**
  Do not run it. Importing replaces your own `~/.minimax/memory/user.md` with
  theirs, and that profile then colours every future session. Yours is kept, but
  only as a timestamped `.bak` beside it.
- If you did expect it, you still need about **1 GB of free disk**: the bundle
  itself plus roughly the same again written into `~/.minimax`, including a full
  copy of your session database as a safety backup.
- You can read `import_bundle.py` before running it, but it is a large program
  and that is not a quick check. The quick check is: run `--dry-run` and read the
  header it prints — the conversation title should be one you recognise, and
  `Project :` and `Data dir :` should say what you expect.

## Two things to check first

1. **MiniMax Code must be installed on this computer and have been launched at
   least once.** There is no database to write to otherwise, and the import stops.
2. **It must be the same version** as the machine this bundle came from. Quit it
   completely before continuing — including tray and background processes. A
   half-quit app holds the database and the import will refuse.

## Run it

```bash
python3 import_bundle.py --dry-run     # rehearse: writes no files
python3 import_bundle.py
```

It asks one question — where the project files should go — and then prints where
everything landed.

**Answer with a plain absolute path**, for example
`/Users/you/Desktop/MyProject`. Three habits land the files somewhere
surprising, so the importer now warns you if it sees one:

- **Do not paste it in quotes.** The quotes become part of the folder name.
- **Do not type a bare name** like `myproject`. It lands inside *this* folder,
  next to the script.
- **Do not type `~`.** It expands to your home folder, and the project ends up
  scattered loose in it.

**If the folder you name already has files in it, same-named ones are
overwritten and are not backed up.** Choose a new folder, or move the existing
one aside first.

## How to tell it worked

The last banner must be exactly:

```
  Import complete
```

Two other banners start with those same words — `Import complete (with
problems)` and `Import complete (attachment sizes not verified)` — and **both
mean something went wrong**. The exit code agrees: run

```bash
echo $?
```

`0` is the only success. Anything else is a non-zero exit, and the lines above
name what happened. `[PROBLEM]` lines name the individual files.

**Then launch MiniMax Code.** The restored conversation only appears after the
app starts, and search needs a relaunch to pick up the new index.

## What you may see that is not a problem

- **`N reference(s) still point at the old machine`** — those are links to files
  that were never in the bundle, most often because they sat outside the project
  folder. Nothing else is affected; copy them across by hand if you need them.
- **`WARNING: N file(s) already existed`** — you chose a folder that already had
  work in it. Read this one; it is the only case where something is overwritten.
- **Chinese text and file names in the output are normal** if the sender's
  project uses them. They are not a sign of corruption. The same goes for a
  `~$` prefix: that is a Microsoft Office lock file, created while a document is
  open, and it is deliberately left out of the bundle.
- **Attachments are not in the project folder.** They go under
  `~/.minimax/v2/assets/`, because the conversation refers to each one by its
  absolute path there. That is correct, not a mistake.

## Where things land

| | |
|---|---|
| project files | the folder you chose |
| the conversation | your MiniMax Code session list |
| attachment files | `~/.minimax/v2/assets/`, original relative layout, paths rewritten to this machine |
| the sender's agent profile | `~/.minimax/memory/` — yours kept as `.bak` |
| a safety backup | `~/.minimax/v2/sqlite/backups/`, one verified file, newest five kept |

## If you need to undo it

Quit MiniMax Code, copy the newest file in `~/.minimax/v2/sqlite/backups/` over
`~/.minimax/v2/sqlite/runtime-state.sqlite`, and **delete the `-wal` and `-shm`
files beside it**. The restored project files are not undone by this — delete the
folder you chose them into.

## Layout

```
import_bundle.py            the only file you need to run
README.md                   this file
bundle/00_manifest.json     what is in here, and the version it needs
bundle/01_messages.jsonl    the conversation
bundle/02_sessions.json     which conversations
bundle/06_transcript.md     the conversation as readable Markdown
bundle/08_attachments.json  every attachment, with its size
bundle/attachments/         the attachment files themselves
bundle/memory/              the sender's agent profile, if it was included
bundle/project_files/       the copy of their project
```

Three things are deliberately absent, and nothing will supply them later: files
that were outside the project folder, the runtime's own logs for the
conversation, and per-conversation tables such as undo history. The importer
prints how many references point at things that did not come along.
