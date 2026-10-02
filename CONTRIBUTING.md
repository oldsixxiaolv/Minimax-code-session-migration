# Contributing

Thanks for looking. This skill writes rows straight into an undocumented
SQLite database, so most changes are small and sharp rather than large.

## Ground rules

- **Standard library only.** No third-party imports, in any script.
- **Python 3.9+.** `from __future__ import annotations` is present, but do not
  rely on 3.10+ syntax in a runtime position.
- **English throughout.** This is published internationally.
- **No personal data, ever.** Not in a docstring, a comment, a test fixture, or
  an example. The test suite samples real conversation titles and phrases out
  of the live database and fails if any of them appears in a shipped file —
  including its own source, which is where the last two leaks were. If you add
  a realistic-looking example, derive it from the running machine instead of
  pasting a real one.

## Before you open a pull request

```bash
python3 tests/run_tests.py          # must be fully green (209 checks, 47 cases)
python3 tests/run_tests.py -k your  # while iterating
```

The suite builds its fixtures from your own MiniMax Code install, so it needs
at least one conversation to exist. Cases that corrupt something copy a bundle
into a temporary directory first.

A new case is not done until you have seen it fail: comment out the fix, watch
the case go red, put the fix back. Several checks here once passed while the
bug they were written for was still live, because they asserted a string that
some other code path also produced.

## What a good change looks like

The scripts exist because a hand-written import fails **quietly** — the
conversation appears, but its PDFs will not open, or search finds nothing, or
it never appears in the list. So:

- Prefer failing loudly over failing quietly. A refusal with a sentence
  explaining what to do beats a warning nobody reads.
- If you learn a fact about the database or the runtime, put it in
  `references/schema-notes.md` and say how you know.
- If a step can leave the install inconsistent, say so in the output.

## Reporting a problem

Open an issue with what you ran, what you expected, and what happened. The
database schema version and platform help most:

```bash
python3 -c "import sqlite3,sys,platform; \
  print(platform.platform(), sys.version.split()[0], \
        sqlite3.connect('$HOME/.minimax/v2/sqlite/runtime-state.sqlite') \
        .execute('SELECT MAX(version) FROM local_runtime_v2_schema_migrations') \
        .fetchone()[0])"
```

Never attach a bundle or a database to an issue. A bundle contains somebody's
conversation, their file names, and their profile.
