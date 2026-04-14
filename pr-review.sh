#!/usr/bin/env bash
# pr-review.sh — PR bot comment resolution daemon.
#
# Polls state.json every 10 minutes for tickets that have a PR URL, then
# invokes the resolve-agent-reviews skill to address unresolved bot comments
# (medium severity and above only).  Maintains its own state file and never
# touches supervisor state.json.
#
# Usage:
#   ./pr-review.sh          Start polling daemon
#   ./pr-review.sh --once   Run a single poll and exit (useful for testing)
#   ./pr-review.sh --status Show current status

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
POLL_INTERVAL="${POLL_INTERVAL:-600}"   # seconds between polls (default 10 min)
MAX_ITERATIONS="${MAX_ITERATIONS:-7}"   # per-ticket iteration cap
IMAGE_NAME="epic-runner:latest"
CLAUDE_CMD="claude"
CLAUDE_ARGS=("--dangerously-skip-permissions")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
STATE_FILE="${SCRIPT_DIR}/state.json"           # read-only — never written
PR_STATE_FILE="${SCRIPT_DIR}/pr-review-state.json"
LOGS_DIR="${SCRIPT_DIR}/logs/pr-review"
PR_STATE_LOCK="/tmp/pr-review-state.lock"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
info() { log "INFO  $*"; }
warn() { log "WARN  $*"; }
error() { log "ERROR $*"; }

# ---------------------------------------------------------------------------
# Prerequisite check
# ---------------------------------------------------------------------------
check_prerequisites() {
    local missing=0
    for cmd in docker claude gh git jq npx; do
        if ! command -v "$cmd" &>/dev/null; then
            error "Required command not found: $cmd"
            missing=1
        fi
    done
    if [[ $missing -ne 0 ]]; then
        error "Please install the missing tools and try again."
        exit 3
    fi
    if ! claude --version &>/dev/null; then
        error "Claude CLI is not working. Run 'claude' to authenticate."
        exit 3
    fi
    if ! gh auth status &>/dev/null; then
        error "gh CLI is not authenticated. Run 'gh auth login'."
        exit 3
    fi
    info "All prerequisites found."
}

# ---------------------------------------------------------------------------
# Docker image build (needed to read config.yaml via Python/yaml)
# ---------------------------------------------------------------------------
build_image_if_needed() {
    if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
        info "Building Docker image $IMAGE_NAME..."
        docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
    else
        local dockerfile="${SCRIPT_DIR}/Dockerfile"
        local image_created
        image_created=$(docker image inspect "$IMAGE_NAME" --format '{{.Created}}' 2>/dev/null || echo "")
        if [[ -n "$image_created" ]]; then
            local df_mtime
            df_mtime=$(stat -c %Y "$dockerfile" 2>/dev/null || stat -f %m "$dockerfile" 2>/dev/null || echo "0")
            local image_epoch
            image_epoch=$(date -d "$image_created" +%s 2>/dev/null || date -j -f "%Y-%m-%dT%H:%M:%S" "${image_created%%.*}" +%s 2>/dev/null || echo "0")
            if [[ "$df_mtime" -gt "$image_epoch" ]]; then
                info "Dockerfile changed, rebuilding image..."
                docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
            fi
        fi
    fi
}

# ---------------------------------------------------------------------------
# Read agent config (claude_command / claude_args) from config.yaml
# ---------------------------------------------------------------------------
read_agent_config() {
    local raw
    raw=$(docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        --entrypoint python3 \
        "$IMAGE_NAME" \
        -c "
import yaml
with open('/app/config.yaml') as f:
    d = yaml.safe_load(f) or {}
agent = d.get('agent', {})
print(agent.get('claude_command', 'claude'))
for a in agent.get('claude_args', ['--dangerously-skip-permissions']):
    print(a)
" 2>/dev/null) || true

    if [[ -z "$raw" ]]; then
        warn "Could not read agent config from config.yaml; using defaults."
        return
    fi
    mapfile -t _lines <<< "$raw"
    CLAUDE_CMD="${_lines[0]}"
    CLAUDE_ARGS=("${_lines[@]:1}")
}

# ---------------------------------------------------------------------------
# pr-review-state.json management
# ---------------------------------------------------------------------------
init_pr_state() {
    if [[ ! -f "$PR_STATE_FILE" ]]; then
        jq -n '{"last_poll":"","tickets":{},"skip_list":[]}' > "$PR_STATE_FILE"
        info "Initialised $PR_STATE_FILE"
        return
    fi
    if ! jq . "$PR_STATE_FILE" > /dev/null 2>&1; then
        warn "pr-review-state.json is corrupt; reinitialising"
        jq -n '{"last_poll":"","tickets":{},"skip_list":[]}' > "$PR_STATE_FILE"
        return
    fi
    # Reset iteration counts and skip list on every startup so tickets are
    # retried fresh regardless of how many iterations accumulated in prior runs.
    local tmp; tmp=$(mktemp /tmp/pr-review-state-XXXXXX.json)
    jq '.tickets |= map_values(.iterations = 0) | .skip_list = []' \
        "$PR_STATE_FILE" > "$tmp" && mv "$tmp" "$PR_STATE_FILE"
    info "Reset iteration counts and skip list for new run"
}

# All writes to pr-review-state.json go through flock to avoid corruption
# when tickets are processed in parallel background jobs.
locked_update_pr_state() {
    local ticket_key="$1" iterations="$2" outcome="$3"
    local now; now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local tmp; tmp=$(mktemp /tmp/pr-review-state-XXXXXX.json)
    (
        flock -x 200
        jq --arg key "$ticket_key" \
           --argjson iters "$iterations" \
           --arg outcome "$outcome" \
           --arg now "$now" \
           '.tickets[$key] = {iterations: $iters, last_run: $now, last_outcome: $outcome}
            | .last_poll = $now' \
           "$PR_STATE_FILE" > "$tmp" && mv "$tmp" "$PR_STATE_FILE"
    ) 200>"$PR_STATE_LOCK"
}

locked_add_to_skip_list() {
    local ticket_key="$1"
    local tmp; tmp=$(mktemp /tmp/pr-review-state-XXXXXX.json)
    (
        flock -x 200
        jq --arg key "$ticket_key" \
           '.skip_list = ((.skip_list // []) + [$key] | unique)' \
           "$PR_STATE_FILE" > "$tmp" && mv "$tmp" "$PR_STATE_FILE"
    ) 200>"$PR_STATE_LOCK"
}

locked_update_last_poll() {
    local tmp; tmp=$(mktemp /tmp/pr-review-state-XXXXXX.json)
    (
        flock -x 200
        jq --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
           '.last_poll = $now' \
           "$PR_STATE_FILE" > "$tmp" && mv "$tmp" "$PR_STATE_FILE"
    ) 200>"$PR_STATE_LOCK"
}

# ---------------------------------------------------------------------------
# Ticket discovery (reads state.json — never writes it)
# ---------------------------------------------------------------------------
get_eligible_tickets() {
    [[ -s "$STATE_FILE" ]] || return
    local skip_list_json
    skip_list_json=$(jq -c '.skip_list // []' "$PR_STATE_FILE")
    jq -r --argjson skip "$skip_list_json" '
      .tickets
      | to_entries[]
      | select(.value.pr_url != null and .value.pr_url != "")
      | select(.key as $k | $skip | index($k) == null)
      | .key
    ' "$STATE_FILE"
}

get_ticket_pr_url() {
    jq -r --arg k "$1" '.tickets[$k].pr_url' "$STATE_FILE"
}

get_ticket_worktree() {
    jq -r --arg k "$1" '.tickets[$k].worktree // empty' "$STATE_FILE"
}

get_pr_number() {
    # https://github.com/owner/repo/pull/953 → 953
    echo "$1" | grep -oE '[0-9]+$'
}

# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------
run_claude_for_ticket() {
    local ticket_key="$1" pr_url="$2" worktree="$3"
    local pr_number; pr_number=$(get_pr_number "$pr_url")
    local log_file="${LOGS_DIR}/${ticket_key}_$(date +%Y%m%d_%H%M%S).md"
    mkdir -p "$LOGS_DIR"

    local prompt
    prompt="You are resolving PR review bot comments for ticket ${ticket_key}.

The PR URL is: ${pr_url} (PR number: ${pr_number}).

Run /resolve-agent-reviews to fetch and address all unanswered bot comments on this PR.

**Severity filter — important:** Only address findings of **medium severity or above** (Medium, High, Critical/Blocker). For any finding labelled as Low severity (e.g. 🟢 Low, minor, suggestion, nitpick, or similar wording), treat it as a FALSE POSITIVE: reply \"Won't fix: below minimum severity threshold (low severity)\" and resolve the thread with --resolve. Do not make any code changes for low-severity findings.

If auto-detection of the PR number fails, pass \`-p ${pr_number}\` explicitly to agent-reviews commands.

**Before committing any fixes**, discover and run the project's lint and build checks (check for Makefile targets, CI config, or language-standard tools) and ensure they pass. Fix any errors introduced by your changes before committing."

    info "[$ticket_key] Invoking resolve-agent-reviews for $pr_url (log: $(basename "$log_file"))"

    local exit_code=0
    (cd "$worktree" && "$CLAUDE_CMD" -p "$prompt" "${CLAUDE_ARGS[@]}") > "$log_file" 2>&1 || exit_code=$?

    info "[$ticket_key] Claude exited $exit_code"
    echo "$log_file"
}

classify_claude_output() {
    local log_file="$1"
    if grep -q "No unanswered bot comments found" "$log_file" 2>/dev/null; then
        echo "no_qualifying_comments"
    elif grep -qiE "watch(er)? (complete|completed)|all (bot )?comments|all findings|comments? (have been )?addressed|findings (have been )?addressed" "$log_file" 2>/dev/null; then
        echo "comments_addressed"
    else
        echo "error"
    fi
}

run_claude_defensive_review() {
    local ticket_key="$1" pr_url="$2" worktree="$3"
    local pr_number; pr_number=$(get_pr_number "$pr_url")
    local log_file="${LOGS_DIR}/${ticket_key}_defensive_$(date +%Y%m%d_%H%M%S).md"
    mkdir -p "$LOGS_DIR"

    local prompt
    prompt="You are performing a defensive programming review for ticket ${ticket_key}.

The PR URL is: ${pr_url} (PR number: ${pr_number}).

Step 1 — Review: Run /defensive-programming:review-defensiveness on the code changes introduced by this PR. Focus only on files changed in this PR (use the git diff against the base branch to identify them).

Step 2 — Fix: For every issue found, run /defensive-programming:defend to apply the fix. Do not skip any findings, regardless of perceived severity.

Step 3 — Verify: Before committing any fixes, discover and run the project's lint and build checks (check for Makefile targets, CI config, or language-standard tools) and ensure they all pass. Fix any errors introduced by your changes before committing.

Step 4 — Commit: Commit all fixes together with a clear message referencing the ticket ${ticket_key}. If there are no issues to fix, say \"No defensive programming issues found\" and exit cleanly."

    info "[$ticket_key] Invoking defensive-programming review for $pr_url (log: $(basename "$log_file"))"

    local exit_code=0
    (cd "$worktree" && "$CLAUDE_CMD" -p "$prompt" "${CLAUDE_ARGS[@]}") > "$log_file" 2>&1 || exit_code=$?

    info "[$ticket_key] Defensive review Claude exited $exit_code"
    echo "$log_file"
}

classify_defensive_output() {
    local log_file="$1"
    if grep -q "No defensive programming issues found" "$log_file" 2>/dev/null; then
        echo "no_issues"
    elif grep -qiE "^(Done\.|Commit(ted)?\.|## (Defensive Review|Summary))" "$log_file" 2>/dev/null; then
        echo "fixed"
    else
        echo "error"
    fi
}

run_claude_lint_and_test() {
    local ticket_key="$1" pr_url="$2" worktree="$3"
    local pr_number; pr_number=$(get_pr_number "$pr_url")
    local log_file="${LOGS_DIR}/${ticket_key}_lint_$(date +%Y%m%d_%H%M%S).md"
    mkdir -p "$LOGS_DIR"

    local prompt
    prompt="You are running lint, vet, and test checks for ticket ${ticket_key}.

The PR URL is: ${pr_url} (PR number: ${pr_number}).
The worktree is already checked out on the correct branch.

Step 1 — Vet: Run \`go vet ./...\` (or scoped to the relevant package if the repo is a monorepo — check for a Makefile or CI config to find the right scope). Fix every issue reported.

Step 2 — Lint: Discover and run the project's lint tool (e.g. \`golangci-lint run\`, \`just lint\`, or a Makefile target). Fix every issue reported. Re-run until lint is clean.

Step 3 — Test: Run the full test suite (e.g. \`go test ./...\` or the appropriate Makefile/just target). Fix any failing tests. Re-run until all tests pass.

Step 4 — Commit and push: If any fixes were made in steps 1–3, commit them with a clear message referencing ticket ${ticket_key} and push to the remote branch. If everything was already clean, say \"All checks passed with no fixes needed\" and exit."

    info "[$ticket_key] Invoking lint/vet/test for $pr_url (log: $(basename "$log_file"))"

    local exit_code=0
    (cd "$worktree" && "$CLAUDE_CMD" -p "$prompt" "${CLAUDE_ARGS[@]}") > "$log_file" 2>&1 || exit_code=$?

    info "[$ticket_key] Lint/test Claude exited $exit_code"
    echo "$log_file"
}

classify_lint_output() {
    local log_file="$1"
    # Check for commits/pushes first — fixes were made
    if grep -qiE "^commit\b|pushed to (the )?(remote|branch)|^committed\b" "$log_file" 2>/dev/null \
       || grep -qiE "^(Done\.|Commit(ted)?\.|## )" "$log_file" 2>/dev/null; then
        echo "fixed"
    # No fixes needed / already clean
    elif grep -qiE "all checks (passed|pass) with no (fixes needed|issues)|no fixes (needed|were made|required)|already clean|everything (was already )?clean" "$log_file" 2>/dev/null; then
        echo "clean"
    else
        echo "error"
    fi
}

# ---------------------------------------------------------------------------
# Per-ticket processing
# ---------------------------------------------------------------------------
process_ticket() {
    local ticket_key="$1" pr_url="$2" worktree="$3"

    local iterations
    iterations=$(jq -r --arg k "$ticket_key" '.tickets[$k].iterations // 0' "$PR_STATE_FILE")

    if [[ "$iterations" -ge "$MAX_ITERATIONS" ]]; then
        warn "[$ticket_key] Already at iteration cap ($MAX_ITERATIONS); adding to skip list"
        locked_add_to_skip_list "$ticket_key"
        return
    fi

    local new_iterations=$(( iterations + 1 ))

    # Persist incremented count before running so a crash doesn't lose the tally
    locked_update_pr_state "$ticket_key" "$new_iterations" "running"

    local log_file
    log_file=$(run_claude_for_ticket "$ticket_key" "$pr_url" "$worktree")

    local outcome
    outcome=$(classify_claude_output "$log_file")
    info "[$ticket_key] Bot-review outcome: $outcome (iteration $new_iterations/$MAX_ITERATIONS)"

    # Defensive programming review — runs regardless of bot-review outcome
    local def_log_file def_outcome
    def_log_file=$(run_claude_defensive_review "$ticket_key" "$pr_url" "$worktree")
    def_outcome=$(classify_defensive_output "$def_log_file")
    info "[$ticket_key] Defensive-review outcome: $def_outcome"

    # Lint / vet / test — runs regardless of prior outcomes
    local lint_log_file lint_outcome
    lint_log_file=$(run_claude_lint_and_test "$ticket_key" "$pr_url" "$worktree")
    lint_outcome=$(classify_lint_output "$lint_log_file")
    info "[$ticket_key] Lint/test outcome: $lint_outcome"

    # Combined outcome recorded in state: all three outcomes joined
    local combined_outcome="${outcome}+defensive:${def_outcome}+lint:${lint_outcome}"
    locked_update_pr_state "$ticket_key" "$new_iterations" "$combined_outcome"

    if [[ "$outcome" == "no_qualifying_comments" ]]; then
        info "[$ticket_key] No qualifying bot comments remain; adding to skip list"
        locked_add_to_skip_list "$ticket_key"
    elif [[ "$new_iterations" -ge "$MAX_ITERATIONS" ]]; then
        warn "[$ticket_key] Reached max iterations ($MAX_ITERATIONS); adding to skip list"
        locked_add_to_skip_list "$ticket_key"
    fi
}

# ---------------------------------------------------------------------------
# Poll
# ---------------------------------------------------------------------------
run_poll() {
    local -a eligible_tickets
    mapfile -t eligible_tickets < <(get_eligible_tickets)

    if [[ ${#eligible_tickets[@]} -eq 0 ]]; then
        info "No eligible tickets with PR URLs found."
        return
    fi

    info "Found ${#eligible_tickets[@]} eligible ticket(s): ${eligible_tickets[*]}"
    locked_update_last_poll

    local -a pids=()
    for ticket_key in "${eligible_tickets[@]}"; do
        local pr_url worktree
        pr_url=$(get_ticket_pr_url "$ticket_key")
        worktree=$(get_ticket_worktree "$ticket_key")

        if [[ -z "$worktree" || ! -d "$worktree" ]]; then
            warn "[$ticket_key] Worktree '${worktree:-<unset>}' does not exist; skipping this poll"
            continue
        fi

        process_ticket "$ticket_key" "$pr_url" "$worktree" &
        pids+=($!)
        info "[$ticket_key] Started background process PID ${pids[-1]}"
    done

    local failed=0
    for pid in "${pids[@]}"; do
        wait "$pid" || { warn "Background process PID $pid exited non-zero"; failed=$(( failed + 1 )); }
    done

    if [[ $failed -gt 0 ]]; then warn "$failed ticket process(es) reported errors this poll."; fi
}

run_poll_loop() {
    info "PR review daemon starting (poll_interval=${POLL_INTERVAL}s, max_iterations=${MAX_ITERATIONS})"
    while true; do
        info "--- Poll start ---"
        run_poll
        info "--- Poll complete; sleeping ${POLL_INTERVAL}s ---"
        sleep "$POLL_INTERVAL"
    done
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
cmd_status() {
    if [[ ! -f "$PR_STATE_FILE" ]]; then
        echo "No pr-review-state.json found. Run ./pr-review.sh to start."
        exit 0
    fi

    echo "=== PR Review Daemon Status ==="
    echo ""
    echo "Last poll : $(jq -r '.last_poll // "never"' "$PR_STATE_FILE")"
    echo "Skip list : $(jq -r '(.skip_list // []) | if length == 0 then "(empty)" else join(", ") end' "$PR_STATE_FILE")"
    echo ""
    echo "Ticket history:"
    jq -r '
      .tickets // {}
      | to_entries[]
      | "  \(.key): \(.value.iterations)/'"$MAX_ITERATIONS"' iterations, outcome=\(.value.last_outcome // "?"), last_run=\(.value.last_run // "never")"
    ' "$PR_STATE_FILE" || echo "  (none)"
    echo ""
    echo "Eligible this poll (have pr_url, not skipped):"
    if [[ -s "$STATE_FILE" ]]; then
        local skip_list_json
        skip_list_json=$(jq -c '.skip_list // []' "$PR_STATE_FILE")
        local found=0
        while IFS= read -r line; do
            echo "  $line"
            found=1
        done < <(jq -r --argjson skip "$skip_list_json" '
          .tickets | to_entries[]
          | select(.value.pr_url != null and .value.pr_url != "")
          | select(.key as $k | $skip | index($k) == null)
          | "\(.key): \(.value.pr_url)"
        ' "$STATE_FILE" 2>/dev/null || true)
        [[ $found -eq 0 ]] && echo "  (none)"
    else
        echo "  (state.json not found or empty)"
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
STATUS=false
ONCE=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --status)
            STATUS=true
            shift
            ;;
        --once)
            ONCE=true
            shift
            ;;
        *)
            error "Unknown argument: $1"
            echo "Usage: $0 [--status] [--once]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if [[ "$STATUS" == "true" ]]; then
    cmd_status
    exit 0
fi

check_prerequisites
build_image_if_needed
read_agent_config
info "Agent command: $CLAUDE_CMD ${CLAUDE_ARGS[*]}"

init_pr_state

if [[ "$ONCE" == "true" ]]; then
    info "Running single poll (--once)"
    run_poll
else
    run_poll_loop
fi
