# Epic Runner — CLAUDE.md

## What this is

Epic Runner is a Jira-epic automation tool. Given an epic key, it works through sub-tickets sequentially: code → validate → review → ship (git push + PR + Jira transition). It is a thin state machine — complexity lives in the prompts, not the orchestrator.

## Architecture

Three layers:

1. **`supervisor.sh`** — Bash, runs on host. Outer loop: runs Docker container, executes actions (Claude, git, gh), restarts on crash.
2. **`run.py`** + **`lib/`** — Python, runs inside Docker. Reads `state.json`, emits a single JSON action to stdout, exits. Never has access to host tools.
3. **`prompts/`** — Markdown templates with `{{VARIABLE}}` and `{{> partial}}` substitution. User-authored for the target codebase.

The container and supervisor communicate via a request-response loop: container outputs one JSON action, supervisor executes it, calls container again with the result.

## Running things

**Run tests** (always use Docker — no local Python):
```bash
docker compose run --rm test
```

**Build the image manually:**
```bash
docker build -t epic-runner:latest .
```

**Run the state machine directly (for debugging):**
```bash
docker compose run --rm epic-runner next-action
docker compose run --rm epic-runner status
```

**Start an epic run:**
```bash
./supervisor.sh --epic DP-196
./supervisor.sh --resume         # resume from state.json
./supervisor.sh --epic DP-196 --fresh  # discard existing state
```

**Manual overrides (run inside Docker):**
```bash
docker compose run --rm epic-runner skip --ticket DP-203
docker compose run --rm epic-runner retry --ticket DP-203 --phase review_loop
```

## Key files

| File | Purpose |
|------|---------|
| `config.yaml` | User configuration — Jira creds, git settings, limits |
| `state.json` | Runtime state (ephemeral, not committed) |
| `MEMORY.md` | Shared agent memory for the current epic (ephemeral, not committed) |
| `lib/state_machine.py` | Phase transitions and all state logic |
| `lib/actions.py` | Action dataclasses serialised to JSON for the supervisor |
| `lib/parse.py` | Signal detection (`VALIDATION PASSED`, `REVIEW COMPLETE`) |
| `lib/template.py` | `{{VAR}}` substitution + `{{> partial}}` includes |
| `lib/jira.py` | Jira REST API client |
| `lib/config.py` | Load and validate `config.yaml` |
| `supervisor.sh` | Host-side orchestrator (bash) |
| `run.py` | Docker entrypoint, CLI subcommands |

## State machine phases

```
initialise → select_ticket → code_loop → validate → review_loop → ship → select_ticket → ...
                                                                            epic_complete
```

Stuck tickets (exceeded iterations or ship failures) move to `stuck_*` status and are skipped. If `failed_ticket_count > max_ticket_failures`, the epic aborts with exit code 2.

## Signal strings

The parser looks for these in agent output:

- `VALIDATION PASSED` — validator approves, move to review
- `REVIEW COMPLETE` — reviewer approves, move to ship

Both support exact-line match and relaxed case-insensitive match.

## Template variables

Prompts use `{{VARIABLE}}` placeholders. Every agent invocation wraps the role prompt with `memory_header.md` + role prompt + `memory_footer.md`. Undefined variables are silently replaced with empty strings.

## Ephemeral files (not committed)

- `state.json` — written by the state machine, reset with `--fresh`
- `MEMORY.md` — cleared at epic start, accumulates agent learnings
- `logs/` — raw Claude output per phase/iteration (`{ticket}_{phase}_{iteration}.md`)
- `.claude/` — Claude Code local settings

## Environment variables

- `JIRA_API_TOKEN` — required at runtime, must be set in the host environment

## Dependencies

Python (in Docker): `pyyaml`, `requests` only — no frameworks.  
Host: `docker`, `claude` (CLI, authenticated), `gh` (authenticated), `git` 2.25+, `jq`, `bash` 4+.
