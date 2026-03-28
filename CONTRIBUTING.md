# Contributing

Thanks for helping improve the Codex session archive watcher.

## Scope

This tool is intentionally narrow:

- archive local Codex Desktop sessions
- extract plan blocks
- keep outputs auditable
- avoid non-standard dependencies

Please try to preserve those boundaries unless a change clearly improves portability or reliability.

## Development Principles

- Prefer Python standard library only
- Keep Windows, Linux, and macOS compatibility in mind
- Preserve existing archive formats unless a migration path exists
- Make incremental behavior observable and auditable
- Avoid assumptions about hidden reasoning availability

## Before Opening a Change

Check these areas first:

- archive completeness
- filename safety
- plan extraction behavior
- incremental discovery behavior
- chunking and migration behavior

## Local Validation

Run at least:

```bash
python -m py_compile watch_codex_sessions.py tests/test_watch_codex_sessions.py
pytest tests/test_watch_codex_sessions.py -q -p no:cacheprovider
```

If you changed archive semantics, also test against a real local Codex home when possible:

```bash
python watch_codex_sessions.py --backfill-only
python watch_codex_sessions.py --audit-archive
```

## Change Checklist

- Keep new docs paths up to date in both README files
- Add or update tests when behavior changes
- Document new CLI flags
- Document migration or compatibility impact
- Avoid silently changing output filenames or state keys
- Do not commit real archive output or copied local Codex state as test fixtures

## Pull Request Notes

Good PR descriptions usually include:

- what changed
- why it changed
- whether archive format changed
- whether backfill or rescan is recommended
- how it was validated
