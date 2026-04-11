# Epic Runner — Requirements Document

## 1. Overview

Epic Runner is a standalone CLI tool that automates the full lifecycle of working through a Jira epic. Given an epic key, it discovers sub-tickets, codes each one using an AI agent, validates the code against the ticket requirements, runs iterative code review, and ships the result as a pull request — transitioning the Jira ticket to "In Review" when complete.

The orchestrator is a sequential state machine with persistent state. It delegates all code generation, validation, and review work to Claude Code via subprocess calls. It uses Docker for its own Python runtime so the host machine needs no Python installation.

### 1.1 Design Philosophy

The complexity lives in the prompts, not the orchestrator. The orchestrator is a thin state machine that assembles prompts from templates, shells out to Claude Code, parses the output for pass/fail signals, persists state, and decides what to do next. It is deliberately not a framework, not a platform, and not extensible beyond what is described here.

### 1.2 Target Codebases

The orchestrator itself is language-agnostic. It must support being pointed at PHP (Laravel) and Go codebases. The target codebase language does not affect the orchestrator — it only affects the prompt content, which is user-authored.

---

## 2. Architecture

### 2.1 Component Separation

There are three layers:

1. **`supervisor.sh`** (bash, runs on host) — Outer loop. Runs the Docker container. Restarts on crash. Executes Claude Code and git commands on the host when the container requests them.
2. **`run.py`** (Python, runs in Docker) — Inner loop. The state machine. Reads `state.json`, decides the next action, composes prompts, calls Jira API, parses agent output, writes state. Communicates with the supervisor via structured JSON on stdout and exit codes.
3. **Prompt templates** (Markdown files in `prompts/`) — Define agent behaviour. Each prompt is a `.md` file with `{{VARIABLE}}` placeholders and `{{> partial}}` includes. The orchestrator assembles and substitutes these at runtime.

### 2.2 Execution Model

The supervisor and the Python state machine communicate via a request-response loop:

```
supervisor.sh (host)
  │
  ├─▸ docker run epic-runner next-action
  │     └─▸ Reads state.json
  │     └─▸ Outputs JSON action to stdout
  │     └─▸ Exits with code 0
  │
  ├─▸ Interprets action, e.g.:
  │     { "action": "run_agent", "prompt_file": "/tmp/prompt.md", "log_file": "logs/DP-203_code_1.md" }
  │
  ├─▸ Executes: claude -p "$(cat /tmp/prompt.md)" --print --dangerously-skip-permissions > logs/DP-203_code_1.md
  │
  ├─▸ docker run epic-runner process-result --log logs/DP-203_code_1.md
  │     └─▸ Parses output, updates state.json
  │     └─▸ Outputs next action or signals completion
  │
  └─▸ ... loop
```

This separation means the Docker container never needs Claude Code, git, `gh`, SSH keys, or any host credentials. It only needs network access for Jira API calls.

### 2.3 State Persistence

All state is stored in a single `state.json` file on the host filesystem, mounted into the container. The state file is written after every meaningful transition. If the process crashes at any point, the supervisor restarts it and the state machine resumes from the last persisted state.

### 2.4 Shared Memory

A `MEMORY.md` file on the host filesystem accumulates learnings across agent invocations within an epic. Every agent reads it at the start of its prompt and appends to it at the end. It is cleared at the start of each new epic run.

---

## 3. Workflow Phases

### 3.1 Phase: Initialise

**Trigger:** Start of a new epic run (no existing `state.json`, or `--fresh` flag).

**Actions:**
1. Read `config.yaml` for Jira credentials, epic key, and configuration.
2. Clear `MEMORY.md` (write empty file with epic header only).
3. Clear `logs/` directory.
4. Query Jira API for all sub-tickets of the epic.
5. Filter to tickets with status "To Do" (configurable status name).
6. Write initial `state.json` with the ticket list and phase set to `select_ticket`.

**Error handling:**
- Jira API failure: retry 3 times with exponential backoff (2s, 4s, 8s). If all retries fail, exit with code 3 (config/auth error — supervisor will not restart).

### 3.2 Phase: Select Ticket

**Trigger:** State phase is `select_ticket`.

**Actions:**
1. Read the ticket list from `state.json`.
2. Find the first ticket with status `pending`.
3. If no pending tickets remain, transition to `epic_complete` and exit 0.
4. Set the current ticket in state.
5. Emit an action to the supervisor to create a git worktree and branch for this ticket.
6. Transition to `code_loop`.

**Worktree/branch naming:**
- Branch name: `{ticket_key}` (e.g. `DP-203`). This is configurable via `config.yaml` with a pattern like `{ticket_key}_{summary_slug}`.
- Worktree path: `{worktree_root}/{ticket_key}` where `worktree_root` is defined in `config.yaml`.

### 3.3 Phase: Code Loop

**Trigger:** State phase is `code_loop`.

**Actions:**
1. Read current iteration count from state (starts at 1).
2. If iteration exceeds `max_code_iterations`, transition to `stuck_validation` and move to next ticket.
3. Compose the coder prompt:
   - Read `prompts/coder.md`.
   - Prepend `prompts/partials/memory_header.md` (with `{{MEMORY}}` substituted from current `MEMORY.md` contents).
   - Append `prompts/partials/memory_footer.md` (with `{{MEMORY_PATH}}` substituted).
   - Substitute all `{{VARIABLES}}` (see §6 Template Variables).
4. Write composed prompt to a temp file.
5. Emit `run_agent` action to supervisor.
6. Supervisor executes Claude Code in the worktree directory, capturing output to a log file.
7. On `process-result`, parse the log file (see §5 Output Parsing).
8. Increment iteration counter in state.
9. Transition to `validate`.

### 3.4 Phase: Validate

**Trigger:** State phase is `validate`.

**Actions:**
1. Compose the validator prompt (same assembly process as coder: header + `prompts/validator.md` + footer).
2. Emit `run_agent` action to supervisor.
3. Supervisor executes Claude Code in the worktree directory, capturing output.
4. On `process-result`, parse the log for the signal string `VALIDATION PASSED`.
5. If found: transition to `review_loop`, reset review iteration counter to 1.
6. If not found: record the failure reason in state, transition back to `code_loop` (next iteration).

### 3.5 Phase: Review Loop

**Trigger:** State phase is `review_loop`.

**Actions:**
1. Read current review iteration count from state (starts at 1).
2. If iteration exceeds `max_review_iterations`, transition to `stuck_review` and move to next ticket.
3. Compose the reviewer prompt (header + `prompts/reviewer.md` + footer).
4. Emit `run_agent` action to supervisor.
5. Supervisor executes Claude Code in the worktree directory, capturing output.
6. On `process-result`, parse the log for the signal string `REVIEW COMPLETE`.
7. If found: transition to `ship`.
8. If not found: increment review iteration counter, stay in `review_loop` (the reviewer prompt already instructs the agent to fix issues in-place, so re-running review on the modified code is the correct next step).

### 3.6 Phase: Ship

**Trigger:** State phase is `ship`.

**Actions:**
1. Emit `git_commit` action to supervisor. The supervisor runs:
   - `git add -A` in the worktree
   - `git commit -m "{ticket_key}: {ticket_summary}"` (message pattern configurable)
   - `git push origin {branch_name}`
2. Emit `create_pr` action to supervisor. The supervisor runs:
   - `gh pr create --base {base_branch} --head {branch_name} --title "{ticket_key}: {ticket_summary}" --body "{pr_body}"`
   - `pr_body` is generated from the ticket description and a summary of work done (from state history).
3. Emit `jira_transition` action. The container calls the Jira API directly to transition the ticket to "In Review" (target status configurable).
4. Record the PR URL in state.
5. Mark the ticket as `shipped` in state.
6. Transition to `select_ticket` for the next ticket.

**Error handling:**
- Git push failure: retry 2 times. If persistent, mark ticket as `stuck_ship`.
- PR creation failure: retry 2 times. If persistent, mark ticket as `stuck_ship`.
- Jira transition failure: retry 3 times with backoff. If persistent, log a warning but still mark the ticket as shipped (the PR exists — the Jira status is a nice-to-have, not a blocker).

### 3.7 Phase: Stuck States

When a ticket enters `stuck_validation`, `stuck_review`, or `stuck_ship`:

1. Record the failure reason and last log file path in state.
2. Increment the `failed_ticket_count` in state.
3. If `failed_ticket_count` exceeds `max_ticket_failures`, transition to `epic_failed` and exit with code 2.
4. Otherwise, clean up the worktree (emit `cleanup_worktree` action) and transition to `select_ticket`.

---

## 4. Supervisor (`supervisor.sh`)

### 4.1 Responsibilities

The supervisor is a bash script that runs on the host. It:
1. Builds the Docker image (once, on first run or if `Dockerfile` has changed).
2. Runs the Python state machine in a container to get the next action.
3. Executes the action on the host (Claude Code, git, gh).
4. Runs the container again to process the result.
5. Loops until the container signals completion or abort.
6. Restarts the entire loop if the container crashes unexpectedly.

### 4.2 Action Protocol

The container outputs a single JSON object to stdout for each action. The supervisor reads this and acts accordingly.

**Action types:**

```json
{ "action": "run_agent", "prompt_file": "<path>", "log_file": "<path>", "workdir": "<path>" }
```
Supervisor runs: `cd <workdir> && claude -p "$(cat <prompt_file>)" --print --dangerously-skip-permissions > <log_file> 2>&1`

```json
{ "action": "git_worktree_create", "path": "<path>", "branch": "<name>", "base": "<base_branch>" }
```
Supervisor runs: `git worktree add -b <branch> <path> <base>`

```json
{ "action": "git_commit_and_push", "workdir": "<path>", "message": "<msg>", "branch": "<name>" }
```
Supervisor runs: `cd <workdir> && git add -A && git commit -m "<msg>" && git push origin <branch>`

```json
{ "action": "create_pr", "base": "<base_branch>", "head": "<branch>", "title": "<title>", "body": "<body>" }
```
Supervisor runs: `gh pr create --base <base> --head <head> --title "<title>" --body "<body>"`

```json
{ "action": "cleanup_worktree", "path": "<path>" }
```
Supervisor runs: `git worktree remove <path> --force`

```json
{ "action": "complete", "exit_code": 0 }
```
Supervisor stops the loop and exits.

```json
{ "action": "error", "exit_code": 2, "message": "<reason>" }
```
Supervisor prints the message and exits with the given code.

### 4.3 Exit Codes

| Code | Meaning | Supervisor Action |
|------|---------|-------------------|
| 0 | Action emitted successfully (normal loop) | Execute action, continue |
| 0 + `complete` action | Epic finished | Stop, exit 0 |
| 1 | Unhandled crash in Python | Restart (up to max) |
| 2 | Max ticket failures exceeded | Stop, exit 2 |
| 3 | Config/auth error | Stop, exit 3 (no restart) |

### 4.4 Crash Recovery

The supervisor tracks consecutive crash restarts (exit code 1 only). Configuration:

- `MAX_CRASHES`: Maximum consecutive crash restarts before giving up. Default: 5.
- `COOLDOWN`: Seconds to wait between restarts. Default: 10.

A successful action loop (any non-crash exit) resets the crash counter to 0.

### 4.5 Host Prerequisites

The supervisor must verify these exist before starting:
- `docker` (buildable and runnable)
- `claude` CLI (authenticated)
- `gh` CLI (authenticated)
- `git` (with worktree support)
- `jq` (for parsing container JSON output)

If any are missing, print a clear error message and exit with code 3.

---

## 5. Output Parsing

### 5.1 Signal Strings

Each agent prompt defines a specific signal string that indicates success. The parser looks for these in the raw output:

| Phase | Signal | Meaning |
|-------|--------|---------|
| Validate | `VALIDATION PASSED` | Code meets requirements |
| Review | `REVIEW COMPLETE` | No critical/high/medium issues remain |

### 5.2 Parsing Strategy

The parser must handle the fact that Claude may wrap signal strings in markdown formatting, add preamble/postamble, or include them in code blocks. The parsing strategy is:

1. **Exact match first:** Search for the signal string as a standalone line (stripped of whitespace).
2. **Relaxed match second:** Search for the signal string appearing anywhere in the output, case-insensitive.
3. **Negative signals:** Also look for explicit failure indicators to record better failure reasons:
   - Validator: look for lines describing failing requirements.
   - Reviewer: count the number of `critical`, `high`, and `medium` severity issues mentioned.
4. **Fallback:** If neither pass nor explicit fail signals are found, treat as a failure with reason "No clear signal in output — see log file."

### 5.3 Logging

Every Claude Code invocation must be logged to a file in `logs/` with a naming convention of:

```
{ticket_key}_{phase}_{iteration}.md
```

For example: `DP-203_code_1.md`, `DP-203_validate_1.md`, `DP-203_review_2.md`.

These files contain the raw, unmodified output from Claude Code. They are never parsed destructively — the parser reads them but does not modify them.

---

## 6. Template System

### 6.1 Variable Substitution

The template engine performs simple mustache-style substitution. It replaces `{{VARIABLE_NAME}}` with the corresponding value. Variables are not nested or conditional — this is flat string replacement only.

**Available variables:**

| Variable | Source | Description |
|----------|--------|-------------|
| `{{TICKET}}` | Jira API | The ticket key (e.g. `DP-203`) |
| `{{TICKET_SUMMARY}}` | Jira API | The ticket title/summary |
| `{{TICKET_DESCRIPTION}}` | Jira API | The full ticket description |
| `{{TICKET_ACCEPTANCE_CRITERIA}}` | Jira API | Acceptance criteria (from a custom field, configurable) |
| `{{BRANCH}}` | State | The git branch name |
| `{{BASE_REF}}` | Config | The base branch (e.g. `master`, `main`) |
| `{{WORKTREE_PATH}}` | State | Absolute path to the worktree |
| `{{MEMORY}}` | MEMORY.md | The full contents of the memory file |
| `{{MEMORY_PATH}}` | Config/State | Absolute path to the memory file |
| `{{AGENT_ROLE}}` | Phase | The current agent role: `coder`, `validator`, or `reviewer` |
| `{{ITERATION}}` | State | The current iteration number for this phase |
| `{{MAX_ITERATIONS}}` | Config | The max iterations allowed for this phase |
| `{{EPIC_KEY}}` | Config | The epic ticket key |

### 6.2 Partial Includes

The template engine supports `{{> partial_name}}` syntax to include another template file. Partials are loaded from `prompts/partials/` and are themselves subject to variable substitution.

**Assembly order for every agent invocation:**

```
1. prompts/partials/memory_header.md    (substituted)
2. prompts/{agent_role}.md              (substituted)
3. prompts/partials/memory_footer.md    (substituted)
```

The final composed prompt is written to a temp file that the supervisor passes to Claude Code.

### 6.3 Undefined Variables

If a `{{VARIABLE}}` appears in a template but has no value defined, the template engine must replace it with an empty string and log a warning (not an error). This prevents broken prompts from crashing the run but makes the issue visible.

---

## 7. Shared Memory (`MEMORY.md`)

### 7.1 Lifecycle

1. **Epic start:** `MEMORY.md` is cleared and written with only an epic header:
   ```markdown
   # Epic: {EPIC_KEY} — Agent Memory
   
   This file contains discoveries and learnings from agents working on this epic.
   
   ---
   ```
2. **Before each agent call:** The current contents of `MEMORY.md` are read and injected into the prompt as `{{MEMORY}}`.
3. **During each agent call:** The agent is instructed (via `memory_footer.md`) to append learnings to `{{MEMORY_PATH}}`.
4. **Between tickets:** Memory is NOT cleared. Learnings accumulate across tickets within the same epic.
5. **Between epics:** Memory IS cleared (step 1 above).

### 7.2 Memory Header Partial (`prompts/partials/memory_header.md`)

```markdown
## Context from previous agents

The following notes were left by previous agents working on this epic.
Read these carefully before starting — they contain discoveries about
project conventions, gotchas, file locations, and architectural patterns
that are relevant to your task.

{{MEMORY}}
```

If `{{MEMORY}}` is empty (first agent on a fresh epic), this section will still appear but with no content below the instructions. This is acceptable — the agent will see the instruction and know there is no prior context.

### 7.3 Memory Footer Partial (`prompts/partials/memory_footer.md`)

```markdown
## Update shared memory

Before finishing, append any useful discoveries to `{{MEMORY_PATH}}`.
Format your additions under a `### {{TICKET}} — {{AGENT_ROLE}}` subheading.

Include: project conventions you discovered, gotchas or surprises,
file locations that weren't obvious, test commands that work,
architectural patterns, dependency relationships, or anything
the next agent would benefit from knowing.

Do NOT remove or modify existing content — only append.
```

---

## 8. State File (`state.json`)

### 8.1 Schema

```json
{
  "epic_key": "DP-196",
  "started_at": "2026-04-10T09:00:00Z",
  "config_hash": "sha256:abc123...",
  "memory_file": "/app/MEMORY.md",
  "failed_ticket_count": 0,
  "current_ticket": "DP-203",
  "phase": "review_loop",
  "tickets": {
    "DP-203": {
      "status": "in_progress",
      "summary": "Add shadow pricing endpoint",
      "branch": "DP-203",
      "worktree": "/worktrees/DP-203",
      "phase": "review_loop",
      "code_iteration": 2,
      "review_iteration": 1,
      "history": [
        {
          "phase": "code_loop",
          "iteration": 1,
          "result": "validation_failed",
          "reason": "Missing database migration",
          "log_file": "logs/DP-203_code_1.md",
          "timestamp": "2026-04-10T09:05:00Z"
        },
        {
          "phase": "code_loop",
          "iteration": 2,
          "result": "validation_passed",
          "log_file": "logs/DP-203_validate_2.md",
          "timestamp": "2026-04-10T09:12:00Z"
        }
      ]
    },
    "DP-204": {
      "status": "pending",
      "summary": "Add pricing audit logging"
    }
  }
}
```

### 8.2 Ticket Statuses

| Status | Meaning |
|--------|---------|
| `pending` | Not yet started |
| `in_progress` | Currently being worked on |
| `shipped` | PR created, Jira transitioned |
| `stuck_validation` | Exceeded max code iterations |
| `stuck_review` | Exceeded max review iterations |
| `stuck_ship` | Git/PR/Jira failure after retries |

### 8.3 Write Discipline

`state.json` must be written atomically: write to a temp file, then rename. This prevents corruption if the process is killed mid-write. Every phase transition, every iteration increment, and every action result must trigger a state write.

---

## 9. Configuration (`config.yaml`)

```yaml
# Jira
jira:
  base_url: "https://yourcompany.atlassian.net"
  email: "you@company.com"
  api_token_env: "JIRA_API_TOKEN"        # Name of env var holding the token
  todo_status: "To Do"                    # Status name for unstarted tickets
  in_review_status: "In Review"           # Status name to transition to after PR
  acceptance_criteria_field: "customfield_10100"  # Jira custom field ID, or null

# Git
git:
  base_branch: "master"
  worktree_root: "/worktrees"             # Where worktrees are created
  branch_pattern: "{ticket_key}"          # Can include {summary_slug}
  commit_message_pattern: "{ticket_key}: {ticket_summary}"

# Agent
agent:
  claude_command: "claude"                # Path or name of Claude CLI
  claude_args: ["--print", "--dangerously-skip-permissions"]

# Limits
limits:
  max_code_iterations: 5
  max_review_iterations: 5
  max_ticket_failures: 2

# Supervisor
supervisor:
  max_crashes: 5
  cooldown_seconds: 10
```

### 9.1 Environment Variables

Sensitive values (API tokens) must not appear in `config.yaml`. Instead, the config references environment variable names, and the orchestrator reads the value from the environment at runtime.

Required environment variables:
- `JIRA_API_TOKEN` (or whatever name `jira.api_token_env` specifies)

---

## 10. Docker

### 10.1 Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY lib/ lib/
COPY prompts/ prompts/
COPY run.py .

ENTRYPOINT ["python3", "run.py"]
```

### 10.2 Volume Mounts

The supervisor mounts these host paths into the container:

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `./config.yaml` | `/app/config.yaml` | `ro` | Configuration |
| `./state.json` | `/app/state.json` | `rw` | State persistence |
| `./MEMORY.md` | `/app/MEMORY.md` | `rw` | Shared memory |
| `./logs/` | `/app/logs/` | `rw` | Agent output logs |
| `./prompts/` | `/app/prompts/` | `ro` | Prompt templates |
| Temp dir for composed prompts | `/app/tmp/` | `rw` | Composed prompt output |

### 10.3 Network

The container needs network access for Jira API calls. No other external network access is required. The container does not need access to Claude Code, git, GitHub, or any other host tool.

### 10.4 docker-compose.yaml

A `docker-compose.yaml` should be provided for convenient building and volume management, but the supervisor must not depend on `docker-compose` — it uses `docker run` directly.

---

## 11. Prompt Templates

### 11.1 Provided Prompts

The repo ships with three prompt templates. These are starting points — users will customise them for their codebase.

#### `prompts/coder.md`

Must instruct the agent to:
- Read the ticket requirements (provided via `{{TICKET_DESCRIPTION}}` and `{{TICKET_ACCEPTANCE_CRITERIA}}`)
- Implement the required changes in the worktree
- Run existing tests to check for regressions
- Not over-engineer or add scope beyond the ticket

#### `prompts/validator.md`

Must instruct the agent to:
- Read the ticket requirements
- Check the code in the worktree against each requirement
- Run the test suite
- Output `VALIDATION PASSED` on its own line if all requirements are met
- If requirements are not met, list what is missing or broken (no signal string)

#### `prompts/reviewer.md`

This is the user's existing review prompt (provided as reference in this document's companion `PROMPT.md` file). Key characteristics:
- Reviews all changes vs `{{BASE_REF}}`
- Evaluates against 7 criteria: code quality, cyclomatic complexity, security, defensive programming, observability, architecture, test coverage
- Outputs findings with severity levels
- Fixes the top 5 critical/high/medium issues
- Outputs `REVIEW COMPLETE` if no critical/high/medium issues remain
- If issues were fixed, does NOT output `REVIEW COMPLETE` (triggering re-review)

### 11.2 Partial Prompts

See §7.2 and §7.3 for the exact content of `memory_header.md` and `memory_footer.md`.

---

## 12. CLI Interface

### 12.1 Supervisor Commands

```bash
# Start a new epic run
./supervisor.sh --epic DP-196

# Resume a crashed/stopped run (reads existing state.json)
./supervisor.sh --resume

# Start fresh, ignoring existing state
./supervisor.sh --epic DP-196 --fresh

# Show current status without running
./supervisor.sh --status
```

### 12.2 Python CLI Commands (internal, called by supervisor)

```bash
# Get the next action to perform
python3 run.py next-action

# Process the result of an action
python3 run.py process-result --log <log_file>

# Process the result of a non-agent action (git, PR)
python3 run.py process-result --action-success
python3 run.py process-result --action-failure --reason "git push rejected"

# Manual overrides
python3 run.py skip --ticket DP-203
python3 run.py retry --ticket DP-203 --phase review_loop
python3 run.py status
```

---

## 13. File Structure

```
epic-runner/
├── Dockerfile
├── docker-compose.yaml
├── supervisor.sh
├── run.py
├── requirements.txt
├── config.yaml
├── state.json                  # Generated at runtime
├── MEMORY.md                   # Generated at runtime
├── lib/
│   ├── __init__.py
│   ├── state_machine.py        # Phase transitions, resume logic, state persistence
│   ├── jira.py                 # Jira REST API client (get subtasks, detail, transition)
│   ├── template.py             # {{VAR}} substitution + {{> partial}} inclusion
│   ├── actions.py              # Action dataclasses (RunAgent, GitCommit, CreatePR, etc.)
│   ├── parse.py                # Output signal detection (REVIEW COMPLETE, etc.)
│   └── config.py               # Load and validate config.yaml
├── prompts/
│   ├── coder.md
│   ├── validator.md
│   ├── reviewer.md
│   └── partials/
│       ├── memory_header.md
│       └── memory_footer.md
├── logs/                       # Generated at runtime
├── tests/
│   ├── test_state_machine.py
│   ├── test_template.py
│   ├── test_parse.py
│   ├── test_jira.py
│   └── test_actions.py
└── README.md
```

---

## 14. Testing

### 14.1 Unit Tests

All `lib/` modules must have unit tests. Tests must be runnable inside the Docker container via `pytest`. Key test scenarios:

- **state_machine.py**: Phase transitions follow the correct graph. Resume from every possible state. Stuck states trigger correctly at iteration limits. Failed ticket count increments and triggers epic abort.
- **template.py**: Variable substitution works. Partial includes work. Undefined variables produce empty strings and warnings. Nested partials (partial includes another partial) are not required but must not crash.
- **parse.py**: Signal detection works with exact match, relaxed match, markdown-wrapped signals, signals inside code blocks, and absent signals. Failure reason extraction works.
- **jira.py**: API calls are correctly formed. Auth headers are set. Retry logic works (use mock HTTP responses).
- **actions.py**: Action serialisation to JSON is correct. All fields are present.

### 14.2 Integration Tests

Not required in the initial build. The supervisor + container interaction can be tested manually.

---

## 15. Dependencies

### 15.1 Python (in Docker)

```
pyyaml>=6.0
requests>=2.31
```

No other Python dependencies. Do not add frameworks, SDKs, or heavyweight libraries.

### 15.2 Host

- Docker
- Claude Code CLI (authenticated)
- `gh` CLI (authenticated)
- Git 2.25+ (worktree support)
- `jq` (JSON parsing in bash)
- bash 4+

---

## 16. Error Handling Summary

| Error | Retry | Max | On Exhaustion |
|-------|-------|-----|---------------|
| Jira API call fails | Yes, exponential backoff | 3 | Exit code 3 (no restart) |
| Claude Code crashes/times out | Yes (next iteration) | Per-phase max | Stuck state |
| Validation fails | Yes (code loop) | `max_code_iterations` | `stuck_validation` |
| Review finds issues | Yes (review loop) | `max_review_iterations` | `stuck_review` |
| Git push fails | Yes | 2 | `stuck_ship` |
| PR creation fails | Yes | 2 | `stuck_ship` |
| Jira transition fails | Yes, backoff | 3 | Warning only (ticket still marked shipped) |
| Python process crashes | Supervisor restart | `max_crashes` | Supervisor exits |
| Too many tickets stuck | N/A | `max_ticket_failures` | Exit code 2 (epic failed) |

---

## 17. Out of Scope

The following are explicitly not part of this tool:

- Parallel ticket processing (tickets are worked sequentially)
- Web dashboard or UI
- Multiple LLM provider support (Claude Code only)
- Plugin/extension system
- Notification system (Slack, email, etc.)
- Automatic merge of PRs
- Running on CI/CD (this runs on a developer's machine)
- Windows support (Linux and macOS only)

---

## 18. Acceptance Criteria

The tool is complete when:

1. `./supervisor.sh --epic DP-XXX` starts an epic run from scratch.
2. The orchestrator queries Jira for sub-tickets and iterates through them.
3. For each ticket: code loop runs, validation loop runs, review loop runs.
4. Passing tickets get a commit, a push, a PR, and a Jira transition.
5. Failing tickets are skipped after hitting iteration limits.
6. The process survives crashes and resumes from `state.json`.
7. `MEMORY.md` accumulates learnings across tickets within an epic.
8. All prompt templates use `{{VARIABLE}}` substitution and `{{> partial}}` includes.
9. The entire Python runtime is containerised — no host Python required.
10. Unit tests pass for all `lib/` modules.
