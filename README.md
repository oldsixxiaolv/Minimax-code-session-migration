# minimax-session-transfermation

Move a [MiniMax Code](https://www.minimax.io) chat — its messages, its
attachments, and a copy of your project files — to another computer, and keep
chatting there.

MiniMax Code keeps every chat in a local SQLite database, and moving one by hand
fails quietly: the chat appears but its files will not open, search finds
nothing, or it never shows up in the list at all. This repo does it in one step
and refuses loudly when it cannot.

The tool and its skill are registered as `minimax-session-transfermation`. The
spelling is deliberate — renaming it breaks the installed skill.

Python 3.9+, standard library only. No installation step.

## Get it

```bash
git clone <this repository's URL>
cd minimax-session-transfermation
```

Nothing to install, nothing to build. Every command below runs from that
folder, and you need a real MiniMax Code conversation on the machine to export
from.

## Before you start

- **Both computers must have MiniMax Code installed, and the new one must have
  been launched at least once.** The import stops without a database to write to.
- **The new one must not be older than the old one** — and its storage format
  must still be one the importer knows. A newer MiniMax Code is fine as long as
  that is true; the importer refuses anything it would have to guess about.
  Check before you travel, not after.
- **Where it has actually been tested:** macOS 15.7.7, MiniMax Code schema v41,
  Python 3.14.3, 47 test cases. Linux and Windows contain no platform-specific
  code but are **untested**. The design refuses rather than guesses, but that
  is a design intent, not something measured there.

## Use it

**1. On the old computer — find the project name.** This writes nothing:

```bash
python3 scripts/export_bundle.py
```

It prints one copy-pasteable line per project. If yours is missing, you have
more than 12 projects — pass any unique fragment of the folder's path instead,
since the name is matched as a substring of the path.

**2. Export.** Use the `--workspace` value it printed:

```bash
python3 scripts/export_bundle.py --workspace 'my-project' --out ~/Desktop/mybundle
```

It names the folder it wrote and what it left behind. **`Finished WITH
PROBLEMS` is the one result to stop on** — see below.

An export is a snapshot. If you keep chatting afterwards, export again before
you leave. If the export ends `Finished WITH PROBLEMS` it exited non-zero:
some attachments the conversation points at are no longer on this machine. The
conversation itself is still complete — copy those files across by hand if you
still have them.

**3. Copy the whole `mybundle` folder to the new computer** — everything in it,
including the hidden files. It carries its own `import_bundle.py`, so you do not
need the clone you started from.

**4. On the new computer — quit MiniMax Code first**, including tray and
background processes, then:

```bash
cd mybundle
python3 import_bundle.py
```

It asks one question (where the project should go) and prints where everything
landed.

**How to tell it worked.** The last banner must read exactly `Import complete`.
Two other banners start with those same words — `Import complete  (with
problems)` and `Import complete  (attachment sizes not verified)`, with two
spaces — and **both are failures**. The exit code tells you the same thing: `0` is success, non-zero is
not, whatever the banner says. Any `[PROBLEM]` line above names the files
involved.

**5. Launch MiniMax Code.** The restored chat only appears after the app has
started, and search needs a relaunch to see the new index.

## Checking the export before you send it

There is no separate verification tool — the export is its own check. Read three
lines in its output:

- **`Done.`** — the bundle is complete.
- **`WARNING: N subdirector... did NOT go into the bundle`** — re-run with
  `--project-recursive` if those folders held work.
- **`Finished WITH PROBLEMS`** — some attachments the conversation points at are
  no longer on this machine. The conversation is still complete; copy those
  files across by hand if you still have them.

## Cutting a conversation short

If you want the conversation up to a point and no further, add one of
`--keep-through-text`, `--keep-through-id` or `--keep-through-time`. Each one
**previews the cut and writes nothing.** Read the last messages it shows, then
re-run the same command with `--yes`.

```bash
# preview — safe to run, changes nothing
python3 scripts/export_bundle.py --workspace 'my-project' --out ~/Desktop/mybundle \
    --keep-through-text 'the sentence they named'

# only after you have read the preview
python3 scripts/export_bundle.py --workspace 'my-project' --out ~/Desktop/mybundle \
    --keep-through-text 'the sentence they named' --yes
```

Use **single quotes**: `$_` is a shell variable and will expand. The cut is
inclusive — the matching message is kept. If the text matches more than once,
the first 8 matches are listed with their ids and the **earliest** one is used;
pass `--keep-through-id` to choose a different one.

## What travels, and what does not

| Travels | Does not |
|---|---|
| The conversation text, up to the cut | Project subdirectories, unless you pass `--project-recursive` |
| Every attachment file, each compared against the size the bundle recorded where it has one | Files that were outside the project folder |
| A copy of the project files | Credentials, login state, plugins |
| The search index, rebuilt on import | Other projects' conversations |
| The agent's memory — your profile and working preferences, unless `--no-memory` | Runtime history for more than one conversation |

A **truncated** export additionally leaves out the context digest, because it
summarises the runtime's view of the whole conversation and would carry content
from after the cut. The memory file still travels, and it is your profile *as it
stands today* rather than as it stood at the cut — both scripts say so when they
run.

Two things are deliberately not carried and are easy to form the wrong belief
about:

- **Attachments do not go in your project folder.** They go under
  `~/.minimax/v2/assets/`, because the chat addresses each one by absolute path
  there. That is correct, not a bug.
- **The defaults are narrower than they look.** `--workspace` takes **every**
  conversation in the project; use `--session-id` for one.

## Before you import onto a computer

- **If that computer already holds this conversation, importing replaces it** —
  with no check on which copy is newer. Import onto a machine that does not
  already have it, and never over one that is open.
- **If the project folder already holds work, same-named files are overwritten
  and are not backed up.** Memory files are; project files are not. Choose a
  new folder, or move the existing one aside.

## Before you copy anything

The bundle is plain, unencrypted files. Anyone who receives the folder can read
the conversation and your profile in full. Move it on a USB stick or an
encrypted drive; treat any cloud upload as publishing it. Do not commit it.

## If it goes wrong

| What you see | What to do |
|---|---|
| `Database is busy` | Quit MiniMax Code completely on the new computer, retry |
| `older than the bundle's` / `expects columnar version` / `does not match the bundle` | Nothing was written. Update MiniMax Code on **this** computer to the version you exported from |
| `No MiniMax Code session database` | MiniMax Code was never launched on this computer — open it once |
| `No 00_manifest.json` | The `bundle/` folder did not arrive next to the script — copy the whole folder, not just `import_bundle.py` |
| `Another import ... is already running` | Wait for the other one to finish. Two imports at once overwrite each other's files |
| `[PROBLEM]` on attachments | An attachment is `absent`, or its size does not match what the bundle recorded. Re-copy the whole folder and import again |
| `N reference(s) still point at the old machine` | Those files were outside the project and are not in the bundle. Nothing else is affected |
| `WARNING: N file(s) already existed` | The import overwrote same-named files in the project folder. They were **not** backed up |
| `[NOTE]` about delivered-file links | Those files were produced outside the project; copy them by hand |
| Search finds nothing | Quit and relaunch MiniMax Code, then search again |
| Want to undo | Quit the app, restore the newest file in `~/.minimax/v2/sqlite/backups/` over `runtime-state.sqlite`, and **delete the `-wal` and `-shm` files beside it** |

The importer checks the bundle, the destination, the database's writability, its
FTS5 support and its version *before* it copies a single file, so a refusal
leaves nothing behind. The five newest backups are kept. Re-running after a
failure converges on the same result.

One exception, because it is easy to get wrong: the import is not transactional
across the whole run. If something fails *after* step 1, project files are
already in the target folder — check it before assuming a failed run changed
nothing.

## Running the tests

```bash
python3 tests/run_tests.py
```

209 checks across 47 cases. They run against a real MiniMax Code install on
this machine, so they need one with at least one conversation in it.

## Layout

```
scripts/export_bundle.py     make a bundle
scripts/import_bundle.py     restore one — a copy of this ships inside every bundle
tests/run_tests.py           the suite above
```

## License

MIT — see [LICENSE](LICENSE). Contributions welcome: see
[CONTRIBUTING](CONTRIBUTING.md).

**Current version: 1.0.** Changes are listed in
[CHANGELOG](CHANGELOG.md).

There is deliberately no CI: the test suite builds its fixtures from a real
MiniMax Code install with a real conversation in it, so it cannot run on a
fresh machine. That is why the suite exists locally rather than in a
pipeline.
