# Publishing Checklist

Use this checklist before extracting this folder into a standalone GitHub repository.

## Must Decide Before Publishing

- Choose an open-source license
- Decide the public repository name
- Decide whether sample archive output should be included
- Decide whether any local-path examples should be generalized

This repository does not add a standalone license automatically because that is a legal choice you should make explicitly.

## Repository-Ready Files

Recommended minimum set:

- `README.md`
- `README.zh-CN.md`
- `CONTRIBUTING.md`
- `.gitignore`
- `watch_codex_sessions.py`
- `__init__.py`
- tests moved with the script or copied into a standalone `tests/` folder

## Before the First Public Push

- Replace repo-local absolute links with relative GitHub links if you split this tool into a new repository
- Rename `.gitignore.example` to `.gitignore`
- Review command examples and normalize local paths
- Remove any environment-specific notes that only apply to the source repository where this tool was originally developed
- Confirm no private session data is being committed
- Confirm no personal local paths remain in examples unless intentionally shown
- Confirm that `output/`, `archive/`, and copied `~/.codex` data are ignored

## Suggested Extras

- `LICENSE`
- `CHANGELOG.md`
- `SECURITY.md`
- GitHub Actions workflow for test runs
- Example sanitized fixture data for demonstrations

## Recommended First Release Notes

Call out these boundaries clearly:

- archives visible Codex data only
- hidden reasoning text is not exported
- relies on local Codex Desktop storage layout
- plan retention checks use rollout JSONL as the source of truth
