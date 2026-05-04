# Claude Code Notes

This repository uses [AGENTS.md](AGENTS.md) as the shared technical
runbook for all coding agents. Claude Code should read that file first
and follow the same repo conventions as Codex, Cursor, Aider, and other
agents.

## Repo-Specific Context

- This is a Python `uv` workspace monorepo.
- The root `.venv/` serves `sdk`, `tools`, and every package under
  `agents/`.
- Prefer `make <verb>` over direct script calls; the Makefile is the
  stable operator surface.
- Runtime secrets live under `secrets/` and are gitignored.
- Real `config/*.yaml`, `config/sip-*.json`, and `logs/` are local-only
  and must not be committed.

## Standard Loop

1. Read `docs/AGENT-LESSONS.md` for repo-specific lessons.
2. Inspect the current diff before changing files.
3. Keep edits narrowly scoped to the request.
4. Run the relevant gate, usually `make verify`.
5. Summarize what changed and what was verified.

For full details, see [AGENTS.md](AGENTS.md).
