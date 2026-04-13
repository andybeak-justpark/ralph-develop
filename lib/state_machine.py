import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from lib.actions import (
    Action,
    CleanupWorktree,
    Complete,
    CreatePR,
    Error,
    GitCommitAndPush,
    GitWorktreeCreate,
    RunAgent,
)
from lib.config import Config, branch_name, commit_message
from lib.jira import JiraClient
from lib.parse import parse_review_output, parse_validation_output
from lib.template import compose_prompt

logger = logging.getLogger(__name__)

_TMP_DIR = "/app/tmp"
_PROMPTS_DIR = "/app/prompts"
_LOGS_DIR = "/app/logs"


class StateMachine:
    def __init__(self, config: Config, state_path: str = "/app/state.json"):
        self._config = config
        self._state_path = state_path
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def next_action(self) -> Action:
        """Return the next action to perform based on current state."""
        phase = self._state.get("phase", "initialise")
        handlers = {
            "initialise": self._handle_initialise,
            "select_ticket": self._handle_select_ticket,
            "code_loop": self._handle_code_loop,
            "validate": self._handle_validate,
            "review_loop": self._handle_review_loop,
            "ship": self._handle_ship,
            "epic_complete": self._handle_epic_complete,
            "epic_failed": self._handle_epic_failed,
        }
        handler = handlers.get(phase)
        if handler is None:
            logger.error("Unknown phase: %s", phase)
            return Error(message=f"Unknown phase: {phase}", exit_code=1)
        return handler()

    def process_result(
        self,
        log_file: Optional[str] = None,
        success: Optional[bool] = None,
        failure_reason: str = "",
        output: str = "",
    ) -> Action:
        """Process the result of a previously emitted action and return the next one."""
        phase = self._state.get("phase", "initialise")
        ticket_key = self._state.get("current_ticket")

        if phase in ("code_loop", "validate", "review_loop", "ship") and ticket_key is None:
            logger.error("process_result called in phase '%s' with no current_ticket", phase)
            return Error(message=f"No current ticket set in phase '{phase}'", exit_code=1)

        if phase == "initialise":
            return self._process_initialise_result(success, failure_reason)
        if phase == "select_ticket":
            return self._process_select_ticket_result(success, failure_reason)
        if phase == "code_loop":
            return self._process_code_loop_result(log_file, ticket_key)
        if phase == "validate":
            return self._process_validate_result(log_file, ticket_key)
        if phase == "review_loop":
            return self._process_review_loop_result(log_file, ticket_key)
        if phase == "ship":
            return self._process_ship_result(success, failure_reason, output, ticket_key)

        logger.error("process_result called in unexpected phase: %s", phase)
        return Error(message=f"Unexpected phase for process_result: {phase}", exit_code=1)

    # ------------------------------------------------------------------
    # Phase handlers — next_action
    # ------------------------------------------------------------------

    def _handle_initialise(self) -> Action:
        """Initialise the epic: fetch tickets from Jira, write initial state."""
        epic_key = self._config.epic_key
        if not epic_key:
            return Error(message="epic_key not set in config", exit_code=3)

        logger.info("Initialising epic %s", epic_key)
        client = JiraClient(self._config)
        tickets_raw = client.get_epic_subtasks(epic_key)

        tickets = {}
        for t in tickets_raw:
            tickets[t["key"]] = {
                "status": "pending",
                "summary": t["summary"],
                "description": t.get("description", ""),
                "acceptance_criteria": t.get("acceptance_criteria", ""),
            }

        if not tickets:
            logger.warning("No pending tickets found for epic %s", epic_key)

        self._state = {
            "epic_key": epic_key,
            "started_at": _now(),
            "config_hash": self._config.config_hash,
            "memory_file": "/app/MEMORY.md",
            "failed_ticket_count": 0,
            "current_ticket": None,
            "phase": "select_ticket",
            "stack_tip": self._config.git.base_branch,
            "tickets": tickets,
        }
        self._save_state()
        self._reset_memory(epic_key)
        return self._handle_select_ticket()

    def _handle_select_ticket(self) -> Action:
        """Select the next pending ticket and set up a worktree."""
        tickets = self._state.get("tickets", {})

        # Resume a partially-initialised ticket if one is already current.
        # This handles crash recovery and the case where the supervisor discards the
        # GitWorktreeCreate that process-result emits inline from _handle_ship, causing
        # the next next-action call to see the ticket already in_progress and skip it.
        current_key = self._state.get("current_ticket")
        if current_key and current_key in tickets:
            current = tickets[current_key]
            if current.get("status") == "in_progress" and current.get("code_iteration", 1) == 0:
                self._state["phase"] = "select_ticket"
                self._save_state()
                return GitWorktreeCreate(
                    path=current["worktree"],
                    branch=current["branch"],
                    base=current["base"],
                )

        next_key = None
        for key, info in tickets.items():
            if info.get("status") == "pending":
                next_key = key
                break

        # Fallback: recover any in_progress ticket whose worktree was never created
        # (code_iteration == 0 means worktree creation hasn't completed yet).
        if next_key is None:
            for key, info in tickets.items():
                if info.get("status") == "in_progress" and info.get("code_iteration", 1) == 0:
                    next_key = key
                    break

        if next_key is None:
            self._state["phase"] = "epic_complete"
            self._save_state()
            return self._handle_epic_complete()

        ticket = tickets[next_key]

        # Only initialise fields when selecting a fresh pending ticket.
        # For recovery of an already-initialised in_progress ticket, keep its
        # existing base/branch/worktree so it branches from the correct point.
        if ticket.get("status") != "in_progress":
            branch = branch_name(self._config, next_key)
            worktree_path = os.path.join(self._config.git.worktree_root, next_key)
            base = self._state.get("stack_tip", self._config.git.base_branch)

            ticket["status"] = "in_progress"
            ticket["branch"] = branch
            ticket["base"] = base
            ticket["worktree"] = worktree_path
            ticket["code_iteration"] = 0
            ticket["review_iteration"] = 1
            ticket.setdefault("history", [])

        self._state["current_ticket"] = next_key
        self._state["phase"] = "select_ticket"  # Will move to code_loop after worktree
        self._save_state()

        return GitWorktreeCreate(
            path=ticket["worktree"],
            branch=ticket["branch"],
            base=ticket["base"],
        )

    def _handle_code_loop(self) -> Action:
        """Emit a run_agent action for the coder."""
        ticket_key, ticket = self._get_current_ticket()
        iteration = ticket.get("code_iteration", 1)

        if iteration > self._config.limits.max_code_iterations:
            return self._handle_stuck(ticket_key, "stuck_validation", "Exceeded max code iterations")

        log_file = _log_path(ticket_key, "code", iteration)
        prompt_file = _prompt_path(ticket_key, "code", iteration)
        variables = self._build_variables(ticket_key, "coder", iteration)
        prompt = compose_prompt("coder", variables, _PROMPTS_DIR)
        _write_temp(prompt_file, prompt)

        return RunAgent(
            prompt_file=prompt_file,
            log_file=log_file,
            workdir=ticket["worktree"],
        )

    def _handle_validate(self) -> Action:
        """Emit a run_agent action for the validator."""
        ticket_key, ticket = self._get_current_ticket()
        iteration = self._completed_code_iteration(ticket)
        log_file = _log_path(ticket_key, "validate", iteration)
        prompt_file = _prompt_path(ticket_key, "validate", iteration)
        variables = self._build_variables(ticket_key, "validator", iteration)
        prompt = compose_prompt("validator", variables, _PROMPTS_DIR)
        _write_temp(prompt_file, prompt)

        return RunAgent(
            prompt_file=prompt_file,
            log_file=log_file,
            workdir=ticket["worktree"],
        )

    def _handle_review_loop(self) -> Action:
        """Emit a run_agent action for the reviewer."""
        ticket_key, ticket = self._get_current_ticket()
        iteration = ticket.get("review_iteration", 1)

        if iteration > self._config.limits.max_review_iterations:
            return self._handle_stuck(ticket_key, "stuck_review", "Exceeded max review iterations")

        log_file = _log_path(ticket_key, "review", iteration)
        prompt_file = _prompt_path(ticket_key, "review", iteration)
        variables = self._build_variables(ticket_key, "reviewer", iteration)
        prompt = compose_prompt("reviewer", variables, _PROMPTS_DIR)
        _write_temp(prompt_file, prompt)

        return RunAgent(
            prompt_file=prompt_file,
            log_file=log_file,
            workdir=ticket["worktree"],
        )

    def _handle_ship(self) -> Action:
        """Handle the ship phase: git commit, create PR, Jira transition."""
        ticket_key, ticket = self._get_current_ticket()
        ship_step = ticket.get("ship_step", "git")

        if ship_step == "git":
            message = commit_message(self._config, ticket_key, ticket["summary"])
            return GitCommitAndPush(
                workdir=ticket["worktree"],
                message=message,
                branch=ticket["branch"],
            )
        if ship_step == "pr":
            pr_body = self._build_pr_body(ticket)
            title = f"{ticket_key}: {ticket['summary']}"
            return CreatePR(
                base=ticket.get("base", self._config.git.base_branch),
                head=ticket["branch"],
                title=title,
                body=pr_body,
            )
        if ship_step == "done":
            # Advance the stack tip so the next ticket branches off this one.
            self._state["stack_tip"] = ticket["branch"]
            ticket["status"] = "shipped"
            ticket.pop("ship_step", None)
            self._state["phase"] = "select_ticket"
            self._save_state()
            return self._handle_select_ticket()

        if ship_step == "jira":
            # Crash-recovery path: process crashed after saving "jira" but before "done".
            # Retry the Jira transition (idempotent) and advance.
            logger.warning("Recovering from interrupted ship for %s — retrying Jira transition", ticket_key)
            self._do_jira_transition(ticket_key)
            ticket["ship_step"] = "done"
            self._save_state()
            return self._handle_ship()

        return Error(message=f"Unknown ship_step: {ship_step}", exit_code=1)

    def _handle_epic_complete(self) -> Action:
        logger.info("Epic complete.")
        return Complete(exit_code=0)

    def _handle_epic_failed(self) -> Action:
        reason = self._state.get("epic_failed_reason", "Too many tickets failed")
        logger.error("Epic failed: %s", reason)
        return Error(message=reason, exit_code=2)

    # ------------------------------------------------------------------
    # Phase handlers — process_result
    # ------------------------------------------------------------------

    def _process_initialise_result(self, success, failure_reason) -> Action:
        if success is False:
            return Error(message=f"Initialise failed: {failure_reason}", exit_code=3)
        return self.next_action()

    def _process_select_ticket_result(self, success, failure_reason) -> Action:
        """After worktree creation (or cleanup_worktree), move to code_loop or next ticket."""
        if success is False:
            ticket_key = self._state.get("current_ticket")
            if ticket_key:
                # Worktree creation failed — skip this ticket
                logger.error("Worktree creation failed for %s: %s", ticket_key, failure_reason)
                return self._handle_stuck(ticket_key, "stuck_ship", f"Worktree creation failed: {failure_reason}")
            return Error(message=f"select_ticket failed: {failure_reason}", exit_code=1)

        ticket_key = self._state.get("current_ticket")
        if ticket_key is None:
            # Called after cleanup_worktree for a stuck ticket — advance to next ticket.
            return self._handle_select_ticket()

        ticket = self._state["tickets"][ticket_key]
        ticket["code_iteration"] = 1
        self._state["phase"] = "code_loop"
        self._save_state()
        return self._handle_code_loop()

    def _process_code_loop_result(self, log_file, ticket_key) -> Action:
        if ticket_key is None or ticket_key not in self._state.get("tickets", {}):
            return Error(message=f"No current ticket in phase 'code_loop'", exit_code=1)
        ticket = self._state["tickets"][ticket_key]
        iteration = ticket.get("code_iteration", 1)
        _append_history(ticket, "code_loop", iteration, "code_ran", log_file=log_file)
        ticket["code_iteration"] = iteration + 1
        self._state["phase"] = "validate"
        self._save_state()
        return self._handle_validate()

    def _process_validate_result(self, log_file, ticket_key) -> Action:
        if ticket_key is None or ticket_key not in self._state.get("tickets", {}):
            return Error(message=f"No current ticket in phase 'validate'", exit_code=1)
        ticket = self._state["tickets"][ticket_key]
        iteration = self._completed_code_iteration(ticket)

        text = _read_log(log_file)
        if not text:
            logger.warning("Validator produced no output (log_file=%s)", log_file)
            _append_history(ticket, "validate", iteration, "validation_failed",
                            reason="Agent produced no output", log_file=log_file or "")
            self._state["phase"] = "code_loop"
            self._save_state()
            return self._handle_code_loop()

        result = parse_validation_output(text)

        if result.passed:
            _append_history(ticket, "validate", iteration, "validation_passed", log_file=log_file)
            self._state["phase"] = "review_loop"
            self._save_state()
            return self._handle_review_loop()
        else:
            _append_history(
                ticket, "validate", iteration, "validation_failed",
                reason=result.failure_reason, log_file=log_file,
            )
            self._state["phase"] = "code_loop"
            self._save_state()
            return self._handle_code_loop()

    def _process_review_loop_result(self, log_file, ticket_key) -> Action:
        if ticket_key is None or ticket_key not in self._state.get("tickets", {}):
            return Error(message=f"No current ticket in phase 'review_loop'", exit_code=1)
        ticket = self._state["tickets"][ticket_key]
        iteration = ticket.get("review_iteration", 1)

        text = _read_log(log_file)
        if not text:
            logger.warning("Reviewer produced no output (log_file=%s)", log_file)
            _append_history(ticket, "review_loop", iteration, "review_issues_found",
                            reason="Agent produced no output", log_file=log_file or "")
            ticket["review_iteration"] = iteration + 1
            self._state["phase"] = "review_loop"
            self._save_state()
            return self._handle_review_loop()

        result = parse_review_output(text)

        if result.passed:
            _append_history(ticket, "review_loop", iteration, "review_passed", log_file=log_file)
            ticket["ship_step"] = "git"
            self._state["phase"] = "ship"
            self._save_state()
            return self._handle_ship()
        else:
            _append_history(
                ticket, "review_loop", iteration, "review_issues_found",
                reason=result.failure_reason, log_file=log_file,
            )
            ticket["review_iteration"] = iteration + 1
            self._state["phase"] = "validate"
            self._save_state()
            return self._handle_validate()

    def _process_ship_result(self, success, failure_reason, output, ticket_key) -> Action:
        if ticket_key is None or ticket_key not in self._state.get("tickets", {}):
            return Error(message=f"No current ticket in phase 'ship'", exit_code=1)
        ticket = self._state["tickets"][ticket_key]
        ship_step = ticket.get("ship_step", "git")
        git_retries = ticket.get("ship_git_retries", 0)
        pr_retries = ticket.get("ship_pr_retries", 0)

        if success is False:
            if ship_step == "git":
                if git_retries < 2:
                    ticket["ship_git_retries"] = git_retries + 1
                    self._save_state()
                    return self._handle_ship()
                return self._handle_stuck(ticket_key, "stuck_ship", f"Git push failed: {failure_reason}")
            if ship_step == "pr":
                if pr_retries < 2:
                    ticket["ship_pr_retries"] = pr_retries + 1
                    self._save_state()
                    return self._handle_ship()
                return self._handle_stuck(ticket_key, "stuck_ship", f"PR creation failed: {failure_reason}")
            # Jira transition failure — log warning but continue
            logger.warning("Jira transition failed for %s: %s", ticket_key, failure_reason)

        if ship_step == "git":
            ticket["ship_step"] = "pr"
            ticket.pop("ship_git_retries", None)
            self._save_state()
            return self._handle_ship()

        if ship_step == "pr":
            # Record PR URL from output
            if output:
                ticket["pr_url"] = output.strip()
            ticket.pop("ship_pr_retries", None)
            # Attempt Jira transition (non-fatal) before the single save so the
            # on-disk state goes directly from "pr" to "done" with no intermediate
            # "jira" step that could leave the machine unrecoverable on a crash.
            jira_ok = self._do_jira_transition(ticket_key)
            if not jira_ok:
                ticket["jira_transition_failed"] = True
                logger.error(
                    "Jira transition to '%s' failed for %s (non-fatal)",
                    self._config.jira.in_review_status, ticket_key,
                )
                _append_history(ticket, "ship", 0, "jira_transition_failed",
                                reason=f"Failed to transition to '{self._config.jira.in_review_status}'")
            else:
                _append_history(ticket, "ship", 0, "jira_transition_ok")
            ticket["ship_step"] = "done"
            self._save_state()
            return self._handle_ship()

        if ship_step == "jira":
            # Kept as a safety net for any state.json already on disk with this value.
            ticket["ship_step"] = "done"
            self._save_state()
            return self._handle_ship()

        return Error(message=f"Unexpected ship_step in process_result: {ship_step}", exit_code=1)

    # ------------------------------------------------------------------
    # Stuck handling
    # ------------------------------------------------------------------

    def _handle_stuck(self, ticket_key: str, stuck_status: str, reason: str) -> Action:
        if ticket_key not in self._state.get("tickets", {}):
            raise RuntimeError(f"_handle_stuck called with unknown ticket '{ticket_key}'")
        ticket = self._state["tickets"][ticket_key]
        ticket["status"] = stuck_status
        ticket["stuck_reason"] = reason
        self._state["failed_ticket_count"] = self._state.get("failed_ticket_count", 0) + 1
        logger.warning("Ticket %s stuck (%s): %s", ticket_key, stuck_status, reason)

        max_failures = self._config.limits.max_ticket_failures
        if self._state["failed_ticket_count"] > max_failures:
            self._state["phase"] = "epic_failed"
            self._state["epic_failed_reason"] = (
                f"Too many failed tickets ({self._state['failed_ticket_count']})"
            )
            self._save_state()
            return self._handle_epic_failed()

        self._state["phase"] = "select_ticket"
        self._state["current_ticket"] = None
        self._save_state()

        worktree = ticket.get("worktree")
        if worktree:
            return CleanupWorktree(path=worktree)
        return self._handle_select_ticket()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_variables(self, ticket_key: str, agent_role: str, iteration: int) -> dict:
        if ticket_key not in self._state.get("tickets", {}):
            raise RuntimeError(f"_build_variables called with unknown ticket '{ticket_key}'")
        ticket = self._state["tickets"][ticket_key]
        memory = _read_memory(self._state.get("memory_file", "/app/MEMORY.md"))
        return {
            "TICKET": ticket_key,
            "TICKET_SUMMARY": ticket.get("summary", ""),
            "TICKET_DESCRIPTION": ticket.get("description", ""),
            "TICKET_ACCEPTANCE_CRITERIA": ticket.get("acceptance_criteria", ""),
            "BRANCH": ticket.get("branch", ""),
            "BASE_REF": ticket.get("base", self._config.git.base_branch),
            "WORKTREE_PATH": ticket.get("worktree", ""),
            "MEMORY": memory,
            "MEMORY_PATH": self._state.get("memory_file", "/app/MEMORY.md"),
            "AGENT_ROLE": agent_role,
            "ITERATION": str(iteration),
            "MAX_ITERATIONS": str(
                self._config.limits.max_code_iterations
                if agent_role in ("coder", "validator")
                else self._config.limits.max_review_iterations
            ),
            "EPIC_KEY": self._state.get("epic_key", ""),
        }

    def _build_pr_body(self, ticket: dict) -> str:
        lines = []
        description = ticket.get("description", "")
        if description:
            lines.append(description)
            lines.append("")
        history = ticket.get("history", [])
        if history:
            lines.append("## Work summary")
            for entry in history:
                phase = entry.get("phase", "unknown")
                iteration = entry.get("iteration", "?")
                result = entry.get("result", "no result recorded")
                lines.append(f"- {phase} iteration {iteration}: {result}")
        return "\n".join(lines)

    def _do_jira_transition(self, ticket_key: str) -> bool:
        """Attempt Jira transition from within the container. Returns True on success."""
        client = JiraClient(self._config)
        ok = client.transition_ticket(ticket_key, self._config.jira.in_review_status)
        return ok

    def _reset_memory(self, epic_key: str) -> None:
        memory_path = self._state.get("memory_file", "/app/MEMORY.md")
        content = (
            f"# Epic: {epic_key} — Agent Memory\n\n"
            "This file contains discoveries and learnings from agents working on this epic.\n\n"
            "---\n"
        )
        try:
            os.makedirs(os.path.dirname(memory_path), exist_ok=True)
        except OSError:
            pass
        try:
            with open(memory_path, "w") as f:
                f.write(content)
        except OSError as e:
            logger.warning("Could not reset MEMORY.md: %s", e)

    def _load_state(self) -> dict:
        if not os.path.exists(self._state_path):
            return {"phase": "initialise"}
        try:
            with open(self._state_path, "r") as f:
                content = f.read().strip()
            if not content:
                return {"phase": "initialise"}
            return json.loads(content)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load state file: %s", e)
            return {"phase": "initialise"}

    def _save_state(self) -> None:
        try:
            with open(self._state_path, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            logger.error("Failed to save state: %s", e)
            raise

    def get_state(self) -> dict:
        return self._state

    # ------------------------------------------------------------------
    # Internal ticket access helpers
    # ------------------------------------------------------------------

    def _get_current_ticket(self) -> tuple[str, dict]:
        """Return (ticket_key, ticket) for the current ticket.

        Raises RuntimeError with a clear message if current_ticket is not set
        or not present in the tickets dict.
        """
        ticket_key = self._state.get("current_ticket")
        if ticket_key is None:
            raise RuntimeError(
                f"current_ticket is None in phase '{self._state.get('phase')}'"
            )
        tickets = self._state.get("tickets", {})
        if ticket_key not in tickets:
            raise RuntimeError(
                f"current_ticket '{ticket_key}' not found in tickets dict"
            )
        return ticket_key, tickets[ticket_key]

    @staticmethod
    def _completed_code_iteration(ticket: dict) -> int:
        """Return the code iteration that most recently completed.

        _process_code_loop_result increments code_iteration for the *next* run
        before validate is called.  Subtract 1 to pair validate_N with code_N
        so both log files share the same N.
        """
        return ticket.get("code_iteration", 2) - 1

    def skip_ticket(self, ticket_key: str) -> None:
        if ticket_key not in self._state.get("tickets", {}):
            raise ValueError(f"Ticket {ticket_key} not found in state")
        ticket = self._state["tickets"][ticket_key]
        ticket["status"] = "skipped"
        if self._state.get("current_ticket") == ticket_key:
            self._state["current_ticket"] = None
            self._state["phase"] = "select_ticket"
        self._save_state()

    def retry_ticket(self, ticket_key: str, phase: str) -> None:
        if ticket_key not in self._state.get("tickets", {}):
            raise ValueError(f"Ticket {ticket_key} not found in state")
        ticket = self._state["tickets"][ticket_key]
        # Only undo the failure count when the ticket was actually counted as failed.
        # Check before overwriting status so we don't double-decrement on repeated retries.
        if ticket.get("status", "").startswith("stuck"):
            self._state["failed_ticket_count"] = max(
                0, self._state.get("failed_ticket_count", 1) - 1
            )
        ticket["status"] = "in_progress"
        ticket.pop("stuck_reason", None)
        if phase == "code_loop":
            ticket["code_iteration"] = 1
        elif phase == "review_loop":
            ticket["review_iteration"] = 1
        self._state["current_ticket"] = ticket_key
        self._state["phase"] = phase
        self._save_state()


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_path(ticket_key: str, phase: str, iteration: int) -> str:
    return os.path.join(_LOGS_DIR, f"{ticket_key}_{phase}_{iteration}.md")


def _prompt_path(ticket_key: str, phase: str, iteration: int) -> str:
    os.makedirs(_TMP_DIR, exist_ok=True)
    return os.path.join(_TMP_DIR, f"{ticket_key}_{phase}_{iteration}_prompt.md")


def _write_temp(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _read_log(log_file: Optional[str]) -> str:
    if not log_file:
        return ""
    try:
        with open(log_file, "r") as f:
            return f.read()
    except OSError:
        logger.warning("Could not read log file: %s", log_file)
        return ""


def _read_memory(memory_path: str) -> str:
    try:
        with open(memory_path, "r") as f:
            return f.read()
    except OSError:
        return ""


def _append_history(
    ticket: dict,
    phase: str,
    iteration: int,
    result: str,
    reason: str = "",
    log_file: str = "",
) -> None:
    entry = {
        "phase": phase,
        "iteration": iteration,
        "result": result,
        "timestamp": _now(),
    }
    if reason:
        entry["reason"] = reason
    if log_file:
        entry["log_file"] = log_file
    ticket.setdefault("history", []).append(entry)
