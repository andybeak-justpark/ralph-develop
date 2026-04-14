#!/usr/bin/env bash
# supervisor.sh — Epic Runner host-side orchestrator.
#
# Runs the Python state machine in Docker, executes actions on the host,
# and loops until the epic is complete or a fatal error occurs.
#
# Usage:
#   ./supervisor.sh --repo /path/to/repo --epic DP-196   Start a new run
#   ./supervisor.sh --repo /path/to/repo --resume        Resume from state
#   ./supervisor.sh --repo /path/to/repo --fresh         Fresh run
#   ./supervisor.sh --status                             Show current status
#
# --repo can also be set via git.repo_dir in config.yaml, or the script
# will use the current directory if it is a git repository.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration defaults (overridden by config.yaml values via shell env)
# ---------------------------------------------------------------------------
MAX_CRASHES="${MAX_CRASHES:-5}"
COOLDOWN="${COOLDOWN:-10}"
IMAGE_NAME="epic-runner:latest"
CLAUDE_CMD="claude"
CLAUDE_ARGS=("--dangerously-skip-permissions")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR=""   # Set via --repo or git.repo_dir in config.yaml
GIT_CRYPT_KEY=""  # Set via git.git_crypt_key in config.yaml
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
STATE_FILE="${SCRIPT_DIR}/state.json"
MEMORY_FILE="${SCRIPT_DIR}/MEMORY.md"
LOGS_DIR="${SCRIPT_DIR}/logs"
PROMPTS_DIR="${SCRIPT_DIR}/prompts"
TMP_DIR="/tmp/epic-runner-tmp"

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
    for cmd in docker claude gh git jq; do
        if ! command -v "$cmd" &>/dev/null; then
            error "Required command not found: $cmd"
            missing=1
        fi
    done
    if [[ $missing -ne 0 ]]; then
        error "Please install the missing tools and try again."
        exit 3
    fi
    # Check Claude is authenticated
    if ! claude --version &>/dev/null; then
        error "Claude CLI is not working. Run 'claude' to authenticate."
        exit 3
    fi
    # Check gh is authenticated
    if ! gh auth status &>/dev/null; then
        error "gh CLI is not authenticated. Run 'gh auth login'."
        exit 3
    fi
    info "All prerequisites found."
}

# ---------------------------------------------------------------------------
# Docker image build
# ---------------------------------------------------------------------------
build_image_if_needed() {
    if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
        info "Building Docker image $IMAGE_NAME..."
        docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
    else
        # Rebuild if Dockerfile is newer than the image
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
# Read git config (git_crypt_key) from config.yaml
# ---------------------------------------------------------------------------
read_git_config() {
    local raw
    raw=$(docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        --entrypoint python3 \
        "$IMAGE_NAME" \
        -c "
import yaml
with open('/app/config.yaml') as f:
    d = yaml.safe_load(f) or {}
print(d.get('git', {}).get('git_crypt_key', '') or '')
" 2>/dev/null) || true
    # Expand ~ on the host (not inside Docker where HOME is different)
    GIT_CRYPT_KEY="${raw:-}"
    if [[ -n "$GIT_CRYPT_KEY" ]]; then
        GIT_CRYPT_KEY="${GIT_CRYPT_KEY/#\~/$HOME}"
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
# Run the Python state machine in Docker
# ---------------------------------------------------------------------------
run_container() {
    local args=("$@")
    mkdir -p "$LOGS_DIR" "$TMP_DIR"
    touch "$STATE_FILE" "$MEMORY_FILE"

    docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        -v "${STATE_FILE}:/app/state.json:rw" \
        -v "${MEMORY_FILE}:/app/MEMORY.md:rw" \
        -v "${LOGS_DIR}:/app/logs:rw" \
        -v "${PROMPTS_DIR}:/app/prompts:ro" \
        -v "${TMP_DIR}:/app/tmp:rw" \
        -e "JIRA_API_TOKEN=${JIRA_API_TOKEN:-}" \
        "$IMAGE_NAME" \
        "${args[@]}"
}

# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

handle_run_agent() {
    local prompt_file workdir log_file
    prompt_file=$(echo "$1" | jq -r '.prompt_file')
    log_file=$(echo "$1" | jq -r '.log_file')
    workdir=$(echo "$1" | jq -r '.workdir')

    # Prompt file is inside the container's /app/tmp — map to host path
    local host_prompt_file host_log_file
    if [[ "$prompt_file" == /app/tmp/* ]]; then
        host_prompt_file="${TMP_DIR}/${prompt_file#/app/tmp/}"
    else
        warn "Unexpected prompt_file path (expected /app/tmp/ prefix): $prompt_file"
        host_prompt_file="${TMP_DIR}/$(basename "$prompt_file")"
    fi
    if [[ "$log_file" == /app/logs/* ]]; then
        host_log_file="${LOGS_DIR}/${log_file#/app/logs/}"
    else
        warn "Unexpected log_file path (expected /app/logs/ prefix): $log_file"
        host_log_file="${LOGS_DIR}/$(basename "$log_file")"
    fi

    mkdir -p "$(dirname "$host_log_file")"

    info "Running agent for prompt: ${prompt_file##*/}"
    info "Log: $host_log_file"
    info "Workdir: $workdir"

    # Translate container paths in the prompt to their host equivalents so the
    # agent writes MEMORY.md to the ralph-develop dir, not the repo worktree.
    local prompt_content
    prompt_content=$(sed "s|/app/MEMORY\.md|${MEMORY_FILE}|g" "$host_prompt_file")

    if cd "$workdir" && "$CLAUDE_CMD" -p "$prompt_content" "${CLAUDE_ARGS[@]}" > "$host_log_file" 2>&1; then
        info "Agent run complete."
        # Pass the log file path back to the container using the container's path
        run_container process-result --log "$log_file"
    else
        local exit_code=$?
        warn "Agent run failed (exit $exit_code)"
        run_container process-result --log "$log_file"
    fi
}

handle_git_worktree_create() {
    local path branch base
    path=$(echo "$1" | jq -r '.path')
    branch=$(echo "$1" | jq -r '.branch')
    base=$(echo "$1" | jq -r '.base')

    info "Creating worktree: $path (branch: $branch, base: $base)"
    mkdir -p "$(dirname "$path")"
    cd "$REPO_DIR"

    local worktree_created=false
    if git worktree list --porcelain | grep -q "^worktree $path$"; then
        info "Worktree already exists at $path"
        worktree_created=true
    elif git branch --list "$branch" | grep -q "$branch"; then
        info "Branch $branch already exists, checking out existing"
        # Use --no-checkout so the smudge filter isn't invoked before git-crypt
        # has been unlocked inside the worktree.
        if git worktree add --no-checkout "$path" "$branch"; then
            worktree_created=true
        else
            local reason="git worktree add failed for existing branch '$branch' at '$path'"
            warn "$reason"
            run_container process-result --action-failure --reason "$reason"
            return
        fi
    else
        # Use --no-checkout so the smudge filter isn't invoked before git-crypt
        # has been unlocked inside the worktree.
        if git worktree add --no-checkout -b "$branch" "$path" "$base"; then
            worktree_created=true
        else
            local reason="git worktree add -b failed: branch='$branch' path='$path' base='$base'"
            warn "$reason"
            run_container process-result --action-failure --reason "$reason"
            return
        fi
    fi

    if [[ "$worktree_created" == "true" ]]; then
        # Check out files now that the smudge filter is in place.
        # git-crypt does not need to be re-unlocked per-worktree: all worktrees share
        # the same .git directory (including .git/git-crypt/keys/), so the smudge
        # filter configured when the main repo was unlocked at startup applies here too.
        info "Checking out files in worktree: $path"
        if ! (cd "$path" && git checkout HEAD -- .); then
            warn "git checkout in worktree failed — files may be missing"
        fi
        run_container process-result --action-success
    fi
}

handle_git_commit_and_push() {
    local workdir message branch
    workdir=$(echo "$1" | jq -r '.workdir')
    message=$(echo "$1" | jq -r '.message')
    branch=$(echo "$1" | jq -r '.branch')

    info "Committing and pushing: $branch"
    cd "$workdir"
    git add -A

    # Commit only if there are staged changes (allows safe retries after a failed push)
    if ! git diff --cached --quiet; then
        if ! git commit -m "$message"; then
            local reason="git commit failed in $workdir"
            warn "$reason"
            run_container process-result --action-failure --reason "$reason"
            return
        fi
    fi

    # Push; on non-fast-forward rejection rebase onto remote and retry once
    if git push origin "$branch"; then
        run_container process-result --action-success
    else
        info "Push rejected; attempting rebase onto remote $branch..."
        if git pull --rebase origin "$branch" && git push origin "$branch"; then
            run_container process-result --action-success
        else
            local reason="git push failed in $workdir (even after rebase)"
            warn "$reason"
            run_container process-result --action-failure --reason "$reason"
        fi
    fi
}

handle_create_pr() {
    local base head title body
    base=$(echo "$1" | jq -r '.base')
    head=$(echo "$1" | jq -r '.head')
    title=$(echo "$1" | jq -r '.title')
    body=$(echo "$1" | jq -r '.body')

    info "Creating PR: $title"
    cd "$REPO_DIR"
    local pr_url
    if pr_url=$(gh pr create --base "$base" --head "$head" --title "$title" --body "$body" 2>&1); then
        info "PR created: $pr_url"
        run_container process-result --action-success --output "$pr_url"
    else
        warn "PR creation failed: $pr_url"
        run_container process-result --action-failure --reason "$pr_url"
    fi
}

cleanup_previous_run() {
    if [[ ! -f "$STATE_FILE" ]] || [[ ! -s "$STATE_FILE" ]]; then
        return
    fi

    info "Cleaning up artifacts from previous run..."
    cd "$REPO_DIR"

    # Remove any worktrees recorded in state
    local worktrees
    worktrees=$(jq -r '.tickets // {} | to_entries[].value.worktree // empty' "$STATE_FILE" 2>/dev/null || true)
    while IFS= read -r wt; do
        [[ -z "$wt" ]] && continue
        if git worktree list --porcelain | grep -q "^worktree $wt$"; then
            info "Removing worktree: $wt"
            git worktree remove "$wt" --force 2>/dev/null || true
        fi
        rm -rf "$wt"
    done <<< "$worktrees"

}

handle_cleanup_worktree() {
    local path
    path=$(echo "$1" | jq -r '.path')
    info "Cleaning up worktree: $path"
    cd "$REPO_DIR"
    git worktree remove "$path" --force 2>/dev/null || true
    run_container process-result --action-success
}

# ---------------------------------------------------------------------------
# Main action dispatch loop
# ---------------------------------------------------------------------------
run_action_loop() {
    local action_json action_type

    while true; do
        info "--- Requesting next action ---"
        local container_exit=0
        action_json=$(run_container next-action 2>/tmp/epic-runner-stderr.log) || container_exit=$?

        if [[ $container_exit -ne 0 ]]; then
            error "Container exited with code $container_exit"
            return $container_exit
        fi

        action_type=$(echo "$action_json" | jq -r '.action' 2>/dev/null || echo "")
        if [[ -z "$action_type" ]]; then
            error "Container produced no valid JSON action. Output: $action_json"
            return 1
        fi

        info "Action: $action_type"

        case "$action_type" in
            run_agent)
                handle_run_agent "$action_json"
                ;;
            git_worktree_create)
                handle_git_worktree_create "$action_json"
                ;;
            git_commit_and_push)
                handle_git_commit_and_push "$action_json"
                ;;
            create_pr)
                handle_create_pr "$action_json"
                ;;
            cleanup_worktree)
                handle_cleanup_worktree "$action_json"
                ;;
            complete)
                local exit_code
                exit_code=$(echo "$action_json" | jq -r '.exit_code')
                info "Epic complete (exit $exit_code)."
                return "${exit_code:-0}"
                ;;
            error)
                local msg ec
                msg=$(echo "$action_json" | jq -r '.message')
                ec=$(echo "$action_json" | jq -r '.exit_code')
                error "Epic runner error: $msg"
                return "${ec:-2}"
                ;;
            *)
                error "Unknown action type: $action_type"
                return 1
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# Crash recovery loop
# ---------------------------------------------------------------------------
run_with_crash_recovery() {
    local crash_count=0

    while true; do
        run_action_loop
        local exit_code=$?

        case $exit_code in
            0)
                info "Completed successfully."
                exit 0
                ;;
            2)
                error "Epic failed (too many ticket failures)."
                exit 2
                ;;
            3)
                error "Config/auth error. Not retrying."
                exit 3
                ;;
            1)
                crash_count=$((crash_count + 1))
                if [[ $crash_count -gt $MAX_CRASHES ]]; then
                    error "Max crash restarts ($MAX_CRASHES) exceeded. Giving up."
                    exit 1
                fi
                warn "Crash #$crash_count. Restarting in ${COOLDOWN}s..."
                sleep "$COOLDOWN"
                ;;
            *)
                error "Unexpected exit code: $exit_code"
                exit "$exit_code"
                ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# CLI: --status command
# ---------------------------------------------------------------------------
cmd_status() {
    if [[ ! -f "$STATE_FILE" ]]; then
        echo "No state.json found. Run ./supervisor.sh --epic KEY to start."
        exit 0
    fi
    run_container status
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
EPIC_KEY=""
RESUME=false
FRESH=false
STATUS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --epic)
            EPIC_KEY="$2"
            shift 2
            ;;
        --repo)
            REPO_DIR="$(cd "$2" && pwd)"
            shift 2
            ;;
        --resume)
            RESUME=true
            shift
            ;;
        --fresh)
            FRESH=true
            shift
            ;;
        --status)
            STATUS=true
            shift
            ;;
        *)
            error "Unknown argument: $1"
            echo "Usage: $0 --repo PATH [--epic KEY] [--resume] [--fresh] [--status]" >&2
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

if [[ -z "$EPIC_KEY" && "$RESUME" == "false" ]]; then
    error "Either --epic KEY or --resume is required."
    echo "Usage: $0 --epic DP-196 | --resume | --fresh | --status" >&2
    exit 1
fi

check_prerequisites
build_image_if_needed
read_agent_config
read_git_config
info "Agent command: $CLAUDE_CMD ${CLAUDE_ARGS[*]}"

# ---------------------------------------------------------------------------
# Resolve REPO_DIR: --repo flag > git.repo_dir in config > current directory
# ---------------------------------------------------------------------------
if [[ -z "$REPO_DIR" ]]; then
    # Try to read git.repo_dir from config.yaml using Docker
    REPO_DIR=$(docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        --entrypoint python3 \
        "$IMAGE_NAME" \
        -c "
import yaml, sys
with open('/app/config.yaml') as f:
    d = yaml.safe_load(f) or {}
print(d.get('git', {}).get('repo_dir', ''))
" 2>/dev/null || echo "")
fi

if [[ -z "$REPO_DIR" ]]; then
    # Fall back to current directory if it is a git repo
    if git -C "$(pwd)" rev-parse --git-dir &>/dev/null; then
        REPO_DIR="$(pwd)"
        warn "No --repo specified; using current directory: $REPO_DIR"
    else
        error "Cannot determine repository directory."
        error "Pass --repo /path/to/repo, or set git.repo_dir in config.yaml, or run from inside the repo."
        exit 3
    fi
fi

if ! git -C "$REPO_DIR" rev-parse --git-dir &>/dev/null; then
    error "$REPO_DIR is not a git repository."
    exit 3
fi

info "Repository: $REPO_DIR"

# Unlock git-crypt in the main repo so the smudge filter works for new worktrees.
# Must run on the main repo (not individual worktrees) so the key is stored in
# .git/git-crypt/keys/ where all worktrees can find it.
if [[ -n "$GIT_CRYPT_KEY" && -f "$GIT_CRYPT_KEY" ]]; then
    if git -C "$REPO_DIR" config filter.git-crypt.smudge &>/dev/null; then
        info "Unlocking git-crypt in repository"
        if ! (cd "$REPO_DIR" && git-crypt unlock "$GIT_CRYPT_KEY"); then
            warn "git-crypt unlock failed — encrypted files may be unreadable in worktrees"
        fi
    fi
elif [[ -n "$GIT_CRYPT_KEY" && ! -f "$GIT_CRYPT_KEY" ]]; then
    warn "git_crypt_key configured but file not found: $GIT_CRYPT_KEY"
fi

# Inject epic key into config if provided (write a temp config with epic_key set)
if [[ -n "$EPIC_KEY" ]]; then
    # Write epic_key into a separate env var so the container can pick it up.
    # The state machine reads epic_key from config; we patch the YAML temporarily.
    PATCHED_CONFIG="${TMP_DIR}/config_patched.yaml"
    mkdir -p "$TMP_DIR"
    # Use Python (inside Docker) to patch the config — avoids requiring yq on host.
    docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        -v "${TMP_DIR}:/app/tmp:rw" \
        --entrypoint python3 \
        "$IMAGE_NAME" \
        -c "
import yaml, sys
with open('/app/config.yaml') as f:
    d = yaml.safe_load(f)
d['epic_key'] = '${EPIC_KEY}'
with open('/app/tmp/config_patched.yaml', 'w') as f:
    yaml.dump(d, f)
print('Config patched with epic_key=${EPIC_KEY}', file=sys.stderr)
"
    CONFIG_FILE="$PATCHED_CONFIG"
fi

# Auto-fresh: when --epic KEY is given without --resume or --fresh, check if the
# existing state is stale (different epic or already complete/failed) and reset it
# automatically rather than immediately exiting as "complete".
if [[ -n "$EPIC_KEY" && "$RESUME" == "false" && "$FRESH" == "false" ]]; then
    if [[ -f "$STATE_FILE" && -s "$STATE_FILE" ]]; then
        state_epic=$(jq -r '.epic_key // ""' "$STATE_FILE" 2>/dev/null || echo "")
        state_phase=$(jq -r '.phase // "initialise"' "$STATE_FILE" 2>/dev/null || echo "")
        if [[ "$state_epic" != "$EPIC_KEY" || "$state_phase" == "epic_complete" || "$state_phase" == "epic_failed" ]]; then
            info "State is stale (epic='$state_epic', phase='$state_phase') — resetting for $EPIC_KEY."
            FRESH=true
        fi
    fi
fi

if [[ "$FRESH" == "true" ]]; then
    info "Fresh run requested — clearing state and logs."
    cleanup_previous_run
    rm -f "$STATE_FILE"
    rm -rf "${LOGS_DIR:?}/"*
    touch "$STATE_FILE"
fi

if [[ ! -f "$STATE_FILE" ]]; then
    touch "$STATE_FILE"
fi

info "Starting epic runner..."
if [[ -n "$EPIC_KEY" ]]; then
    info "Epic: $EPIC_KEY"
fi

run_with_crash_recovery
