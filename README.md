# Codex Session Archive Watcher

[中文说明](./README.zh-CN.md)

`watch_codex_sessions.py` archives local Codex Desktop sessions into a repository-controlled folder and extracts `<proposed_plan>...</proposed_plan>` blocks into standalone Markdown files.

This folder is structured so it can later be published to GitHub with minimal cleanup.

Command examples below assume this tool is the repository root. If you keep it embedded inside another repository, adjust the script path accordingly.

## What It Does

- Backfills historical Codex sessions
- Follows newly created sessions
- Archives both main threads and spawned subagent threads
- Persists visible messages, tool calls, tool outputs, and orchestration events
- Extracts proposed plans into separate Markdown files
- Audits archive completeness, filename safety, and plan retention
- Supports incremental discovery and incremental reading

## Supported Environments

- Windows
- Linux
- macOS
- Python standard library only

Default Codex home resolution:

- Use `CODEX_HOME` if it is set
- Otherwise fall back to `~/.codex`

## Data Sources

The script reads these local Codex files:

- `~/.codex/session_index.jsonl`
- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/archived_sessions/**/*.jsonl`
- `~/.codex/state_5.sqlite`

How they are used:

- rollout JSONL files are the source of truth for session content
- `session_index.jsonl` is used to discover new threads and thread names
- `state_5.sqlite` enriches thread metadata and parent-child relationships for subagents

## Incremental Behavior

The watcher now works in two layers:

- Incremental reading
  - each session keeps a persisted `last_offset`
  - an existing rollout is re-read only from the last known byte offset
- Incremental discovery
  - the first run, or an explicit `--rescan`, performs a full source discovery
  - normal runs reuse previously known rollout paths from `_state/manifest.json`
  - new thread discovery primarily uses appended rows from `session_index.jsonl`
  - thread metadata is refreshed from `state_5.sqlite`

Operationally this means:

- the first archive build is a full discovery pass
- later runs prefer structured incremental discovery
- `--rescan` is available when you suspect missing sources or moved local Codex data

The latest discovery mode and counters are recorded in:

- `output/codex-archive/_state/manifest.json`
- `output/codex-archive/reports/archive-audit.md`

## What Gets Archived

Visible archive content includes:

- user messages
- assistant final messages
- assistant commentary
- tool calls
- tool results
- orchestration events
  - `spawn_agent`
  - `send_input`
  - `wait_agent`
  - `close_agent`
- reasoning event metadata

Raw hidden chain-of-thought is not exported. Reasoning entries are recorded only as metadata, for example whether encrypted content exists.

## Plan Extraction

The watcher extracts:

```text
<proposed_plan>
...
</proposed_plan>
```

Rules:

- strict match: assistant message contains a plan block and the current turn is in `plan` mode
- fallback match: plan block exists but mode cannot be confirmed from historical data
- title is taken from the first Markdown H1 when available
- otherwise the first non-empty line is used
- filenames are sanitized for Windows and GitHub-friendly usage
- duplicate names receive `-2`, `-3`, and so on

Plan front matter includes:

- `title`
- `session_id`
- `thread_name`
- `source_kind`
- `parent_thread_id`
- `agent_nickname`
- `agent_role`
- `source_turn_id`
- `plan_mode_confirmed`
- `plan_generated_at`
- `extracted_at`
- `source_rollout`

On Windows the script also makes a best-effort attempt to align the plan file creation time with `plan_generated_at`.

## Output Layout

Default output root:

- `output/codex-archive/`

Structure:

```text
output/codex-archive/
  thread_index.json
  _state/
    manifest.json
    plans.json
    sessions/<session-id>.json
  reports/
    archive-audit.md
    filename-audit.md
    retention-audit.md
  sessions/
    <session-id>/
      meta.json
      events/
        part-0001.jsonl
      transcript/
        part-0001.md
  plans/
    <sanitized-title>.md
```

## Chunking Large Files

To keep output manageable, the watcher automatically splits large files:

- events parts: `32 MiB` by default
- transcript parts: `16 MiB` by default
- plan parts: `8 MiB` by default

Example:

```bash
python watch_codex_sessions.py --events-max-bytes 33554432 --transcript-max-bytes 16777216 --plan-max-bytes 8388608
```

## Common Commands

Backfill everything and exit:

```bash
python watch_codex_sessions.py --backfill-only
```

Follow new sessions only:

```bash
python watch_codex_sessions.py --follow-only
```

Backfill once, then continue following:

```bash
python watch_codex_sessions.py
```

Force a full source rediscovery:

```bash
python watch_codex_sessions.py --rescan --backfill-only
```

Verify retained plans:

```bash
python watch_codex_sessions.py --verify-retention
```

Audit archive completeness:

```bash
python watch_codex_sessions.py --audit-archive
```

Audit archived filenames:

```bash
python watch_codex_sessions.py --audit-filenames
```

Repair invalid dynamic filenames:

```bash
python watch_codex_sessions.py --repair-filenames
```

Use a custom Codex home and output directory:

```bash
python watch_codex_sessions.py --codex-home ~/.codex --output-dir output/codex-archive
```

## Optional Auto Git Sync

The watcher can optionally commit and push archive output:

```bash
python watch_codex_sessions.py --follow-only --auto-git
```

Related flags:

- `--auto-git`
- `--git-remote origin`
- `--git-commit-interval-seconds 300`

Safety rules:

- only `output/codex-archive/` is eligible
- if staged changes already exist outside the archive path, auto-sync is refused
- if there is no archive delta, nothing is committed or pushed

## Audit Reports

- `reports/archive-audit.md`
  - discovery mode
  - discovered source counts
  - missing sessions
  - orphan sessions
  - incomplete exports
  - parent-child link mismatches
- `reports/filename-audit.md`
  - invalid path components
  - unsafe dynamic filenames
- `reports/retention-audit.md`
  - whether extracted plan hashes can still be found in source rollout files

## Limits

- hidden reasoning text is generally not accessible
- some UI-only state may not exist in local persisted files
- background compression state is not used as a source of truth
- plan retention verification relies on rollout JSONL, not UI summaries

## Documentation Set

- Chinese README: [README.zh-CN.md](./README.zh-CN.md)
- Contribution guide: [CONTRIBUTING.md](./CONTRIBUTING.md)
- Publishing checklist: [PUBLISHING_CHECKLIST.md](./PUBLISHING_CHECKLIST.md)
- Standalone gitignore template: [.gitignore.example](./.gitignore.example)
- Script entry: [watch_codex_sessions.py](./watch_codex_sessions.py)
- Tests: `tests/test_watch_codex_sessions.py`

## Public Repository Safety

These docs and the script are safe to publish as code and documentation.

Do not publish real archive output unless it has been fully sanitized first. In particular, avoid uploading:

- `output/codex-archive/`
- raw local session transcripts
- raw plan exports from real work
- any local Codex state copied from `~/.codex`

If you split this tool into its own repository, rename [.gitignore.example](./.gitignore.example) to `.gitignore` before the first public push.
