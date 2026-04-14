#!/usr/bin/env bash
# enrich-tickets.sh — Review Jira epic tickets for development readiness and enrich
# their descriptions using reference documentation.
#
# Usage:
#   ./enrich-tickets.sh                        Interactive: prompts for epic + docs
#   ./enrich-tickets.sh --epic DP-196          Skip epic prompt
#   ./enrich-tickets.sh --epic DP-196 --dry-run  Print proposed descriptions, no updates
#
# Documentation sources (prompted interactively) can be:
#   - Local file paths (read directly)
#   - URLs (fetched with curl)
#   - confluence:<Page Title> (fetched from Confluence by page title)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="epic-runner:latest"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
TMP_DIR="/tmp/epic-runner-tmp"
LOGS_DIR="${SCRIPT_DIR}/logs"
CLAUDE_CMD="claude"
CLAUDE_ARGS=("--dangerously-skip-permissions")
DRY_RUN=false
EPIC_KEY=""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }
info() { log "INFO  $*"; }
warn() { log "WARN  $*"; }
error() { log "ERROR $*"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --epic)    EPIC_KEY="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *)
            error "Unknown argument: $1"
            echo "Usage: $0 [--epic KEY] [--dry-run]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
check_prerequisites() {
    local missing=0
    for cmd in docker claude jq curl; do
        if ! command -v "$cmd" &>/dev/null; then
            error "Required command not found: $cmd"
            missing=1
        fi
    done
    if [[ $missing -ne 0 ]]; then exit 3; fi
}

# ---------------------------------------------------------------------------
# Docker image
# ---------------------------------------------------------------------------
build_image_if_needed() {
    info "Building Docker image $IMAGE_NAME..."
    docker build -t "$IMAGE_NAME" "$SCRIPT_DIR" >&2
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

    if [[ -n "$raw" ]]; then
        mapfile -t _lines <<< "$raw"
        CLAUDE_CMD="${_lines[0]}"
        CLAUDE_ARGS=("${_lines[@]:1}")
    fi
}

# ---------------------------------------------------------------------------
# Run a command inside the epic-runner container
# ---------------------------------------------------------------------------
run_container() {
    mkdir -p "$TMP_DIR"
    docker run --rm \
        -v "${CONFIG_FILE}:/app/config.yaml:ro" \
        -v "${TMP_DIR}:/app/tmp:rw" \
        -e "JIRA_API_TOKEN=${JIRA_API_TOKEN:-}" \
        "$IMAGE_NAME" \
        "$@"
}

# ---------------------------------------------------------------------------
# Write enrichment prompt to a file
# ---------------------------------------------------------------------------
write_prompt() {
    local prompt_file="$1" key="$2" summary="$3" description="$4" ac="$5" docs="$6" doc_instructions="$7"

    # Static preamble (no variable expansion)
    cat > "$prompt_file" << 'STATIC'
You are reviewing a Jira ticket to assess whether it has sufficient context for a
developer to implement it without needing to ask clarifying questions.

A ticket is ready for development when the description covers:
- What needs to be implemented (not just repeating the title)
- Which specific files, components, endpoints, or APIs are involved
- Any business rules, constraints, or edge cases the developer needs to handle

STATIC

    # Dynamic ticket content
    printf '## Ticket: %s\n\n' "$key" >> "$prompt_file"
    printf '**Summary:** %s\n\n' "$summary" >> "$prompt_file"
    printf '### Current Description\n\n%s\n\n' "${description:-(no description)}" >> "$prompt_file"
    printf '### Acceptance Criteria\n\n%s\n\n' "${ac:-(none)}" >> "$prompt_file"

    # Free-form documentation instructions (Claude will use its tools to follow these)
    if [[ -n "$doc_instructions" ]]; then
        printf '## Documentation Instructions\n\n' >> "$prompt_file"
        printf 'Before writing the description, follow these instructions to gather relevant reference documentation:\n\n' >> "$prompt_file"
        printf '%s\n\n' "$doc_instructions" >> "$prompt_file"
    fi

    # Pre-fetched static documentation content
    if [[ -n "$docs" ]]; then
        printf '## Reference Documentation\n\n%s\n\n' "$docs" >> "$prompt_file"
    fi

    # Static instructions (no variable expansion)
    cat >> "$prompt_file" << 'STATIC'
---

Your task:

If the ticket already has sufficient context for development, output only the single word:
SKIP

Otherwise, write an improved description for this ticket using relevant information from
the documentation above. Output ONLY the description text — no preamble, no explanation,
no headers like "Here is the description:". The text you output will be written directly
to the Jira ticket description field.

Write in plain text. Use blank lines to separate sections. Do not use markdown formatting.
STATIC
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    check_prerequisites
    build_image_if_needed
    read_agent_config

    # Prompt for epic key if not supplied
    if [[ -z "$EPIC_KEY" ]]; then
        read -r -p "Enter epic key (e.g. DP-196): " EPIC_KEY
    fi
    if [[ -z "$EPIC_KEY" ]]; then
        error "No epic key provided."
        exit 1
    fi

    # Collect documentation sources interactively
    echo ""
    echo "Enter documentation sources. Accepted formats:"
    echo "  - Local file path"
    echo "  - https://... URL"
    echo "  - confluence:<Page Title>"
    echo "  - Any free-form instruction (Claude will follow it using its tools)"
    echo "Press Enter with an empty line when done."
    echo ""
    declare -a DOC_SOURCES=()
    while true; do
        read -r -p "Doc source (or empty to finish): " source
        [[ -z "$source" ]] && break
        DOC_SOURCES+=("$source")
    done

    if [[ ${#DOC_SOURCES[@]} -eq 0 ]]; then
        error "No documentation sources provided."
        exit 1
    fi

    # Read / fetch documentation content; collect free-form instructions separately
    DOC_CONTENT=""
    DOC_INSTRUCTIONS=""
    for source in "${DOC_SOURCES[@]}"; do
        if [[ -f "$source" ]]; then
            info "Reading: $source"
            DOC_CONTENT+=$'\n\n'"=== $(basename "$source") ==="$'\n'"$(cat "$source")"
        elif [[ "$source" =~ ^https?:// ]]; then
            info "Fetching: $source"
            fetched=$(curl -s --max-time 30 "$source" 2>/dev/null || true)
            if [[ -n "$fetched" ]]; then
                DOC_CONTENT+=$'\n\n'"=== $source ==="$'\n'"$fetched"
            else
                warn "Could not fetch: $source"
            fi
        elif [[ "$source" =~ ^confluence: ]]; then
            title="${source#confluence:}"
            info "Fetching Confluence page: $title"
            fetched=$(run_container fetch-confluence --title "$title" || true)
            if [[ -n "$fetched" ]]; then
                DOC_CONTENT+=$'\n\n'"=== Confluence: $title ==="$'\n'"$fetched"
            else
                warn "Could not fetch Confluence page: $title"
            fi
        else
            info "Treating as documentation instruction for Claude: $source"
            DOC_INSTRUCTIONS+=$'\n'"$source"
        fi
    done

    if [[ -z "$DOC_CONTENT" && -z "$DOC_INSTRUCTIONS" ]]; then
        error "Could not read any documentation content."
        exit 1
    fi

    # Fetch all tickets for the epic
    info "Fetching tickets for epic: $EPIC_KEY"
    tickets_json=$(run_container list-tickets --epic "$EPIC_KEY")
    ticket_count=$(echo "$tickets_json" | jq 'length')
    info "Found $ticket_count ticket(s)."

    if [[ "$ticket_count" -eq 0 ]]; then
        info "No tickets found — nothing to do."
        exit 0
    fi

    mkdir -p "$TMP_DIR"

    updated=0
    skipped=0
    failed=0

    for i in $(seq 0 $((ticket_count - 1))); do
        ticket=$(echo "$tickets_json" | jq ".[$i]")
        key=$(echo "$ticket" | jq -r '.key')
        summary=$(echo "$ticket" | jq -r '.summary')
        description=$(echo "$ticket" | jq -r '.description')
        ac=$(echo "$ticket" | jq -r '.acceptance_criteria')
        status=$(echo "$ticket" | jq -r '.status')

        info "[$((i+1))/$ticket_count] $key ($status): $summary"

        prompt_file="${TMP_DIR}/${key}_enrich_prompt.md"
        write_prompt "$prompt_file" "$key" "$summary" "$description" "$ac" "$DOC_CONTENT" "$DOC_INSTRUCTIONS"

        # Call Claude
        new_description=$("$CLAUDE_CMD" -p "$(cat "$prompt_file")" "${CLAUDE_ARGS[@]}" 2>/dev/null || true)

        if [[ -z "$new_description" ]]; then
            warn "$key: Claude returned empty response — skipping."
            failed=$((failed + 1))
            continue
        fi

        # Check for SKIP signal (first non-blank line is exactly "SKIP")
        first_line=$(echo "$new_description" | grep -m1 '.' || true)
        if [[ "$first_line" == "SKIP" ]]; then
            info "$key: Already has sufficient context — no update needed."
            skipped=$((skipped + 1))
            continue
        fi

        if [[ "$DRY_RUN" == "true" ]]; then
            echo ""
            echo "========================================"
            echo "PROPOSED UPDATE: $key — $summary"
            echo "========================================"
            echo "$new_description"
            echo ""
            updated=$((updated + 1))
        else
            desc_file="${TMP_DIR}/${key}_description.txt"
            printf '%s' "$new_description" > "$desc_file"

            if run_container update-description \
                    --ticket "$key" \
                    --description-file "/app/tmp/${key}_description.txt"; then
                info "$key: Description updated."
                updated=$((updated + 1))
            else
                warn "$key: Failed to update description."
                failed=$((failed + 1))
            fi
        fi
    done

    echo ""
    if [[ "$DRY_RUN" == "true" ]]; then
        info "Dry run complete. Tickets that would be updated: $updated, already sufficient: $skipped, errors: $failed"
    else
        info "Done. Updated: $updated, already sufficient: $skipped, errors: $failed"
    fi
}

main "$@"
