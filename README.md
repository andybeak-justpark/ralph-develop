# Epic Runner

Epic Runner automates working through a Jira epic. Given an epic key, it discovers sub-tickets and works through them one by one: a coder agent implements the ticket, a validator agent checks the requirements are met, a reviewer agent reviews the code and fixes issues, then the result is committed, pushed, and a PR is opened. The Jira ticket is transitioned to "In Review".

The orchestrator is a sequential state machine with persistent state. All agent work is delegated to Claude Code via subprocess. The Python runtime is containerised — no Python installation required on the host.

## Prerequisites

- Docker
- [Claude Code CLI](https://docs.anthropic.com/claude-code) (authenticated — run `claude` once to log in)
- [GitHub CLI](https://cli.github.com/) (`gh auth login`)
- Git 2.25+ (worktree support)
- `jq`
- bash 4+

## Setup

**1. Clone this repo:**

```bash
git clone <repo-url> epic-runner
cd epic-runner
```

**2. Copy and edit the config:**

```bash
cp config.yaml my-config.yaml   # or edit config.yaml directly
```

See [Configuration](#configuration) below for all options.

**3. Set your Jira API token:**

```bash
export JIRA_API_TOKEN=your_token_here
```

The token must be set in the environment before running the supervisor. The env var name is configurable via `jira.api_token_env` in `config.yaml`.

**4. Customise the prompt templates** in `prompts/` for your codebase (language, conventions, test commands). See [Prompts](#prompts) below.

**5. Build the Docker image:**

```bash
docker build -t epic-runner:latest .
```

The supervisor builds the image automatically on first run, but building manually first saves time.

---

## Usage

### Start a new epic run

```bash
./supervisor.sh --epic DP-196
```

Queries Jira for all sub-tickets of `DP-196`, then works through them. Requires `--repo` if `git.repo_dir` is not set in `config.yaml`.

### Resume a stopped or crashed run

```bash
./supervisor.sh --resume
```

Reads the existing `state.json` and picks up from where it left off. Use this after a crash, a manual stop (`Ctrl+C`), or a machine restart.

### Start fresh, discarding previous state

```bash
./supervisor.sh --epic DP-196 --fresh
```

Clears `state.json`, empties `logs/`, removes any worktrees and branches created by the previous run, then starts over. Use this to re-run an epic from scratch.

### Show current status

```bash
./supervisor.sh --status
```

Prints the current phase, active ticket, and status of all tickets. Does not start or resume a run.

---

## Flags

| Flag | Argument | Description |
|------|----------|-------------|
| `--epic` | `KEY` | Jira epic key to run (e.g. `DP-196`). Required unless `--resume` is given. |
| `--repo` | `PATH` | Absolute path to the target git repository. Falls back to `git.repo_dir` in `config.yaml`, then the current directory if it is a git repo. |
| `--resume` | — | Resume from existing `state.json`. Mutually exclusive intent with `--epic` (though `--epic` can be combined with `--fresh`). |
| `--fresh` | — | Clear state, logs, and worktrees before starting. Must be combined with `--epic`. |
| `--status` | — | Print current run status and exit. No other flags needed. |

**Examples:**

```bash
# New run, repo specified
./supervisor.sh --epic DP-196 --repo /home/andy/projects/my-app

# Resume after interruption
./supervisor.sh --resume

# Re-run the same epic from scratch
./supervisor.sh --epic DP-196 --fresh

# Check what's happening without starting anything
./supervisor.sh --status
```

---

## Configuration

All configuration lives in `config.yaml`. The file is mounted read-only into Docker — no rebuild needed when you change it.

### `jira`

| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | — | Your Atlassian instance URL, e.g. `https://yourcompany.atlassian.net` |
| `email` | — | Jira account email for API authentication |
| `api_token_env` | `JIRA_API_TOKEN` | Name of the environment variable holding the API token |
| `todo_status` | `To Do` | Jira status name for tickets that should be picked up |
| `in_review_status` | `In Review` | Jira status to transition tickets to after a PR is created |
| `acceptance_criteria_field` | `null` | Custom field ID for acceptance criteria (e.g. `customfield_10100`). `null` if not used. |

### `git`

| Key | Default | Description |
|-----|---------|-------------|
| `repo_dir` | — | Absolute path to the target repository on the host. Can be overridden with `--repo`. |
| `base_branch` | `master` | Branch that ticket branches are created from and PRs target |
| `worktree_root` | — | Absolute path where git worktrees are created, e.g. `/home/andy/projects/worktrees` |
| `branch_pattern` | `{ticket_key}` | Branch name pattern. Supports `{ticket_key}` and `{summary_slug}`. |
| `commit_message_pattern` | `{ticket_key}: {ticket_summary}` | Commit message pattern. Supports `{ticket_key}` and `{ticket_summary}`. |
| `git_crypt_key` | — | Absolute path to a git-crypt key file. Required if the repo uses git-crypt. `~` is expanded. |

### `agent`

| Key | Default | Description |
|-----|---------|-------------|
| `claude_command` | `claude` | Path or name of the Claude Code CLI binary |
| `claude_args` | `["--print", "--dangerously-skip-permissions", "--model", "claude-sonnet-4-6"]` | Arguments passed to Claude Code on every invocation |

### `limits`

| Key | Default | Description |
|-----|---------|-------------|
| `max_code_iterations` | `5` | Maximum times the coder+validator loop runs per ticket before the ticket is marked stuck |
| `max_review_iterations` | `5` | Maximum review passes per ticket before the ticket is marked stuck |
| `max_ticket_failures` | `2` | Maximum number of stuck tickets before the entire epic is aborted |

### `supervisor`

| Key | Default | Description |
|-----|---------|-------------|
| `max_crashes` | `5` | Maximum consecutive Python process crashes before the supervisor gives up |
| `cooldown_seconds` | `10` | Seconds to wait between crash restarts |

---

## How it works

Each ticket goes through these phases in order:

```
initialise → select_ticket → code_loop ⇄ validate → review_loop → ship → select_ticket → ...
```

1. **Initialise** — queries Jira, writes `state.json`, clears `MEMORY.md`.
2. **Select ticket** — picks the next `pending` ticket, creates a git worktree and branch.
3. **Code loop** — runs the coder agent in the worktree. Then immediately runs the validator agent. If validation fails, the coder runs again (up to `max_code_iterations`).
4. **Review loop** — runs the reviewer agent. The reviewer fixes up to 5 critical/high/medium issues per pass. If issues were fixed, it loops (up to `max_review_iterations`).
5. **Ship** — commits and pushes the branch, opens a PR via `gh`, transitions the Jira ticket to "In Review".
6. Moves to the next ticket.

If a ticket exceeds iteration limits or encounters a ship failure, it is marked `stuck` and skipped. If too many tickets get stuck (controlled by `max_ticket_failures`), the run aborts.

All state is persisted to `state.json` after every transition, so the run can be resumed from any point after a crash.

---

## Prompts

The `prompts/` directory contains the agent instructions. These are Markdown files with `{{VARIABLE}}` placeholders. **You should edit these for your codebase** — add your language, framework, test commands, and conventions.

| File | Agent | Purpose |
|------|-------|---------|
| `prompts/coder.md` | Coder | Implements the ticket |
| `prompts/validator.md` | Validator | Checks requirements are met; outputs `VALIDATION PASSED` |
| `prompts/reviewer.md` | Reviewer | Reviews code quality; fixes issues; outputs `REVIEW COMPLETE` |
| `prompts/partials/memory_header.md` | All | Prepended to every prompt; injects shared memory |
| `prompts/partials/memory_footer.md` | All | Appended to every prompt; instructs the agent to update shared memory |

### Template variables

All prompts have access to these variables:

| Variable | Description |
|----------|-------------|
| `{{TICKET}}` | Ticket key, e.g. `DP-203` |
| `{{TICKET_SUMMARY}}` | Ticket title |
| `{{TICKET_DESCRIPTION}}` | Full ticket description from Jira |
| `{{TICKET_ACCEPTANCE_CRITERIA}}` | Acceptance criteria (from custom field, if configured) |
| `{{BRANCH}}` | Git branch name for this ticket |
| `{{BASE_REF}}` | Base branch, e.g. `master` |
| `{{WORKTREE_PATH}}` | Absolute path to the git worktree for this ticket |
| `{{MEMORY}}` | Current contents of `MEMORY.md` |
| `{{MEMORY_PATH}}` | Path to `MEMORY.md` |
| `{{ITERATION}}` | Current iteration number for this phase |
| `{{MAX_ITERATIONS}}` | Maximum iterations allowed for this phase |
| `{{EPIC_KEY}}` | The epic ticket key |

---

## Shared memory

`MEMORY.md` accumulates learnings across all agent invocations within an epic. Every agent reads it at prompt time and is instructed to append discoveries before finishing. This lets later agents benefit from what earlier agents found — project conventions, test commands, gotchas, file locations.

Memory is cleared at the start of each new epic run (`--fresh` or a new `--epic` key).

---

## Manual intervention

If a ticket gets stuck or you need to intervene, use the Python CLI directly via Docker:

### Skip a ticket

```bash
docker compose run --rm epic-runner skip --ticket DP-203
```

Marks the ticket as skipped and moves on to the next one.

### Retry a ticket from a specific phase

```bash
docker compose run --rm epic-runner retry --ticket DP-203 --phase code_loop
```

Resets the ticket to `in_progress` and re-queues it at the given phase. Also decrements the failure counter if the ticket was previously stuck.

Valid phases: `code_loop`, `validate`, `review_loop`, `ship`

### Print current status

```bash
docker compose run --rm epic-runner status
```

Or via the supervisor (without starting a run):

```bash
./supervisor.sh --status
```

---

## Logs

Agent output is saved to `logs/` with the naming convention:

```
logs/{ticket_key}_{phase}_{iteration}.md
```

For example: `logs/DP-203_code_1.md`, `logs/DP-203_validate_1.md`, `logs/DP-203_review_2.md`.

These are the raw, unmodified output from Claude Code. Check them to understand what an agent did or why it failed.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Epic completed — all tickets processed |
| `1` | Too many consecutive crashes — supervisor gave up |
| `2` | Epic aborted — too many tickets got stuck |
| `3` | Config or auth error — check your `config.yaml` and credentials |

---

## Running tests

```bash
docker compose run --rm test
```

---

## File structure

```
epic-runner/
├── supervisor.sh          # Host-side orchestrator (entry point)
├── run.py                 # Python state machine (runs in Docker)
├── Dockerfile
├── docker-compose.yaml
├── requirements.txt
├── config.yaml            # Your configuration
├── lib/
│   ├── state_machine.py   # Phase transitions and state logic
│   ├── actions.py         # Action types (JSON protocol with supervisor)
│   ├── parse.py           # Signal detection (VALIDATION PASSED, REVIEW COMPLETE)
│   ├── template.py        # {{VAR}} substitution and {{> partial}} includes
│   ├── jira.py            # Jira REST API client
│   └── config.py          # Config loading and validation
├── prompts/
│   ├── coder.md
│   ├── validator.md
│   ├── reviewer.md
│   └── partials/
│       ├── memory_header.md
│       └── memory_footer.md
├── logs/                  # Agent output (generated at runtime)
├── state.json             # Run state (generated at runtime)
├── MEMORY.md              # Shared agent memory (generated at runtime)
└── tests/
```
