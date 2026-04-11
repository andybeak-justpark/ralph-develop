import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest


def _make_config(max_code=3, max_review=3, max_failures=2, base_branch="main"):
    cfg = MagicMock()
    cfg.epic_key = "DP-100"
    cfg.config_hash = "sha256:abc"
    cfg.git.base_branch = base_branch
    cfg.git.worktree_root = "/worktrees"
    cfg.git.branch_pattern = "{ticket_key}"
    cfg.git.commit_message_pattern = "{ticket_key}: {ticket_summary}"
    cfg.jira.in_review_status = "In Review"
    cfg.jira.todo_status = "To Do"
    cfg.limits.max_code_iterations = max_code
    cfg.limits.max_review_iterations = max_review
    cfg.limits.max_ticket_failures = max_failures
    return cfg


def _make_state_file(tmp_path, state: dict) -> str:
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return str(p)


def _fresh_state(tickets=None) -> dict:
    if tickets is None:
        tickets = {
            "DP-1": {
                "status": "in_progress",
                "summary": "Test ticket",
                "description": "desc",
                "acceptance_criteria": "",
                "branch": "DP-1",
                "worktree": "/worktrees/DP-1",
                "code_iteration": 1,
                "review_iteration": 1,
                "history": [],
            }
        }
    return {
        "epic_key": "DP-100",
        "started_at": "2026-01-01T00:00:00Z",
        "config_hash": "sha256:abc",
        "memory_file": "/tmp/MEMORY.md",
        "failed_ticket_count": 0,
        "current_ticket": "DP-1",
        "phase": "code_loop",
        "tickets": tickets,
    }


class TestStateLoading:
    def test_empty_state_file_gives_initialise(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("")
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, str(p))
        assert sm.get_state()["phase"] == "initialise"

    def test_missing_state_file_gives_initialise(self, tmp_path):
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, str(tmp_path / "nonexistent.json"))
        assert sm.get_state()["phase"] == "initialise"

    def test_valid_state_is_loaded(self, tmp_path):
        state = _fresh_state()
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        assert sm.get_state()["phase"] == "code_loop"
        assert sm.get_state()["current_ticket"] == "DP-1"

    def test_state_saved_atomically(self, tmp_path):
        state = _fresh_state()
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        # Force a save
        sm._state["phase"] = "validate"
        sm._save_state()
        with open(path) as f:
            saved = json.load(f)
        assert saved["phase"] == "validate"
        # No .tmp files left behind
        assert not list(tmp_path.glob("*.tmp"))


class TestPhaseTransitions:
    def test_code_loop_transitions_to_validate(self, tmp_path):
        state = _fresh_state()
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        # Simulate code loop result
        log_file = tmp_path / "log.md"
        log_file.write_text("Code written.")
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.process_result(log_file=str(log_file))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "validate"

    def test_validate_log_filename_matches_code_iteration(self, tmp_path):
        """validate_N.md must be paired with code_N.md (same N), not N+1."""
        state = _fresh_state()
        # code_iteration=1 at start; after code_loop it becomes 2
        state["tickets"]["DP-1"]["code_iteration"] = 2
        state["phase"] = "validate"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.next_action()
        assert action.action == "run_agent"
        # Should be validate_1, not validate_2
        assert action.log_file.endswith("DP-1_validate_1.md")

    def test_validate_pass_transitions_to_review(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "validate"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        log_file = tmp_path / "validate.md"
        log_file.write_text("All checks passed.\nVALIDATION PASSED\n")
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.process_result(log_file=str(log_file))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "review_loop"

    def test_validate_fail_loops_back_to_code(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "validate"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        log_file = tmp_path / "validate.md"
        log_file.write_text("The migration is missing.")
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.process_result(log_file=str(log_file))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "code_loop"

    def test_review_pass_transitions_to_ship(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "review_loop"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        log_file = tmp_path / "review.md"
        log_file.write_text("Everything looks great.\nREVIEW COMPLETE\n")
        action = sm.process_result(log_file=str(log_file))
        assert action.action == "git_commit_and_push"
        assert sm.get_state()["phase"] == "ship"

    def test_review_fail_loops_back_to_review(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "review_loop"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        cfg = _make_config()
        sm = StateMachine(cfg, path)
        log_file = tmp_path / "review.md"
        log_file.write_text("Found 2 critical issues that need fixing.")
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.process_result(log_file=str(log_file))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "review_loop"
        assert sm.get_state()["tickets"]["DP-1"]["review_iteration"] == 2


class TestShipPhase:
    def _ship_state(self, tmp_path, ship_step="git"):
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = ship_step
        return _make_state_file(tmp_path, state)

    def test_ship_git_emits_commit_action(self, tmp_path):
        path = self._ship_state(tmp_path, "git")
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.next_action()
        assert action.action == "git_commit_and_push"

    def test_ship_git_success_moves_to_pr(self, tmp_path):
        path = self._ship_state(tmp_path, "git")
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.process_result(success=True)
        assert action.action == "create_pr"
        assert sm.get_state()["tickets"]["DP-1"]["ship_step"] == "pr"

    def test_ship_git_retry_on_failure(self, tmp_path):
        path = self._ship_state(tmp_path, "git")
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.process_result(success=False, failure_reason="push rejected")
        assert action.action == "git_commit_and_push"  # Retry
        assert sm.get_state()["tickets"]["DP-1"].get("ship_git_retries", 0) == 1

    def test_ship_git_stuck_after_2_retries(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "git"
        state["tickets"]["DP-1"]["ship_git_retries"] = 2
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.process_result(success=False, failure_reason="push rejected")
        assert sm.get_state()["tickets"]["DP-1"]["status"] == "stuck_ship"

    def test_ship_pr_success_triggers_jira_and_completes(self, tmp_path):
        path = self._ship_state(tmp_path, "pr")
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch.object(sm, "_do_jira_transition"):
            action = sm.process_result(success=True, output="https://github.com/pr/1")
        assert sm.get_state()["tickets"]["DP-1"].get("pr_url") == "https://github.com/pr/1"

    def test_shipped_ticket_moves_to_select_ticket(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "done"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        state["tickets"]["DP-2"] = {
            "status": "pending",
            "summary": "Second ticket",
        }
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.next_action()
        # Should create worktree for DP-2
        assert action.action == "git_worktree_create"
        assert action.branch == "DP-2"

    def test_stacked_branches_base_off_previous_ticket(self, tmp_path):
        """Second ticket must branch off the first ticket's branch, not master."""
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "done"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        state["tickets"]["DP-2"] = {"status": "pending", "summary": "Second ticket"}
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(base_branch="main"), path)
        action = sm.next_action()
        assert action.action == "git_worktree_create"
        assert action.base == "DP-1"  # stacked off DP-1, not main

    def test_pr_base_is_previous_ticket_branch(self, tmp_path):
        """PR for a stacked ticket must target the previous ticket's branch."""
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "pr"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        state["tickets"]["DP-1"]["base"] = "DP-0"  # stacked off DP-0
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(base_branch="main"), path)
        action = sm.next_action()
        assert action.action == "create_pr"
        assert action.base == "DP-0"

    def test_first_ticket_pr_base_is_config_base_branch(self, tmp_path):
        """First ticket's PR must target the config base branch (nothing shipped yet)."""
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "pr"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        state["tickets"]["DP-1"]["base"] = "main"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(base_branch="main"), path)
        action = sm.next_action()
        assert action.action == "create_pr"
        assert action.base == "main"


class TestStuckStates:
    def test_stuck_validation_after_max_iterations(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "code_loop"
        state["tickets"]["DP-1"]["code_iteration"] = 4  # max is 3
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_code=3), path)
        action = sm.next_action()
        assert sm.get_state()["tickets"]["DP-1"]["status"] == "stuck_validation"

    def test_stuck_review_after_max_iterations(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "review_loop"
        state["tickets"]["DP-1"]["review_iteration"] = 4  # max is 3
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_review=3), path)
        action = sm.next_action()
        assert sm.get_state()["tickets"]["DP-1"]["status"] == "stuck_review"

    def test_stuck_increments_failed_count(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "code_loop"
        state["tickets"]["DP-1"]["code_iteration"] = 4
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_code=3), path)
        sm.next_action()
        assert sm.get_state()["failed_ticket_count"] == 1

    def test_epic_fails_when_too_many_stuck(self, tmp_path):
        state = _fresh_state()
        state["failed_ticket_count"] = 2  # already at max
        state["phase"] = "code_loop"
        state["tickets"]["DP-1"]["code_iteration"] = 4
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_code=3, max_failures=2), path)
        action = sm.next_action()
        assert action.action == "error"
        assert action.exit_code == 2
        assert sm.get_state()["phase"] == "epic_failed"

    def test_stuck_ticket_cleans_up_worktree(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "code_loop"
        state["tickets"]["DP-1"]["code_iteration"] = 4
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_code=3), path)
        action = sm.next_action()
        assert action.action == "cleanup_worktree"
        assert action.path == "/worktrees/DP-1"

    def test_process_result_after_cleanup_worktree_advances_to_next_ticket(self, tmp_path):
        """After cleanup_worktree the supervisor calls process-result --action-success.
        current_ticket is None at this point; the state machine must not crash and
        should advance to select_ticket (epic_complete when no more tickets remain)."""
        state = {
            "epic_key": "DP-100",
            "started_at": "2026-01-01T00:00:00Z",
            "config_hash": "sha256:abc",
            "memory_file": "/tmp/MEMORY.md",
            "failed_ticket_count": 1,
            "current_ticket": None,
            "phase": "select_ticket",
            "tickets": {
                "DP-1": {"status": "stuck_validation", "summary": "done"},
            },
        }
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(max_code=3), path)
        # Simulate supervisor calling process-result --action-success after cleanup_worktree
        action = sm.process_result(success=True)
        assert action.action == "complete"


class TestEpicComplete:
    def test_all_tickets_done_emits_complete(self, tmp_path):
        state = {
            "epic_key": "DP-100",
            "started_at": "2026-01-01T00:00:00Z",
            "config_hash": "sha256:abc",
            "memory_file": "/tmp/MEMORY.md",
            "failed_ticket_count": 0,
            "current_ticket": None,
            "phase": "select_ticket",
            "tickets": {
                "DP-1": {"status": "shipped", "summary": "done"},
                "DP-2": {"status": "stuck_review", "summary": "stuck"},
            },
        }
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.next_action()
        assert action.action == "complete"
        assert action.exit_code == 0
        assert sm.get_state()["phase"] == "epic_complete"


class TestManualOverrides:
    def test_skip_ticket(self, tmp_path):
        state = _fresh_state()
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        sm.skip_ticket("DP-1")
        assert sm.get_state()["tickets"]["DP-1"]["status"] == "skipped"
        assert sm.get_state()["phase"] == "select_ticket"

    def test_retry_ticket(self, tmp_path):
        state = _fresh_state()
        state["tickets"]["DP-1"]["status"] = "stuck_validation"
        state["failed_ticket_count"] = 1
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        sm.retry_ticket("DP-1", "code_loop")
        assert sm.get_state()["tickets"]["DP-1"]["status"] == "in_progress"
        assert sm.get_state()["phase"] == "code_loop"
        assert sm.get_state()["failed_ticket_count"] == 0

    def test_retry_in_progress_ticket_does_not_decrement_count(self, tmp_path):
        """Retrying a ticket that is not stuck must not lower the failure counter."""
        state = _fresh_state()
        state["tickets"]["DP-1"]["status"] = "in_progress"
        state["failed_ticket_count"] = 1
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        sm.retry_ticket("DP-1", "code_loop")
        assert sm.get_state()["failed_ticket_count"] == 1

    def test_retry_twice_decrements_once(self, tmp_path):
        """Double-retrying the same ticket must decrement exactly once."""
        state = _fresh_state()
        state["tickets"]["DP-1"]["status"] = "stuck_validation"
        state["failed_ticket_count"] = 2
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        sm.retry_ticket("DP-1", "code_loop")  # stuck -> in_progress, count 2->1
        assert sm.get_state()["failed_ticket_count"] == 1
        sm.retry_ticket("DP-1", "code_loop")  # already in_progress, count unchanged
        assert sm.get_state()["failed_ticket_count"] == 1


class TestGuardedTicketAccess:
    def test_get_current_ticket_raises_when_none(self, tmp_path):
        state = _fresh_state()
        state["current_ticket"] = None
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with pytest.raises(RuntimeError, match="current_ticket is None"):
            sm._get_current_ticket()

    def test_get_current_ticket_raises_when_key_missing(self, tmp_path):
        state = _fresh_state()
        state["current_ticket"] = "DP-999"  # not in tickets
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with pytest.raises(RuntimeError, match="not found in tickets"):
            sm._get_current_ticket()

    def test_process_result_returns_error_when_no_current_ticket(self, tmp_path):
        state = _fresh_state()
        state["current_ticket"] = None
        state["phase"] = "code_loop"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        action = sm.process_result()
        assert action.action == "error"
        assert action.exit_code == 1


class TestEmptyLogHandling:
    def test_validate_empty_log_records_no_output_reason(self, tmp_path):
        """A missing/empty agent log must record 'Agent produced no output', not a parse fallback."""
        state = _fresh_state()
        state["phase"] = "validate"
        state["tickets"]["DP-1"]["code_iteration"] = 2
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            # Pass a nonexistent log file so _read_log returns ""
            action = sm.process_result(log_file=str(tmp_path / "nonexistent.md"))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "code_loop"
        history = sm.get_state()["tickets"]["DP-1"]["history"]
        assert any(
            e.get("result") == "validation_failed" and e.get("reason") == "Agent produced no output"
            for e in history
        )

    def test_review_empty_log_records_no_output_reason(self, tmp_path):
        """A missing reviewer log must record 'Agent produced no output' and loop back."""
        state = _fresh_state()
        state["phase"] = "review_loop"
        state["tickets"]["DP-1"]["review_iteration"] = 1
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.process_result(log_file=str(tmp_path / "nonexistent.md"))
        assert action.action == "run_agent"
        assert sm.get_state()["phase"] == "review_loop"
        assert sm.get_state()["tickets"]["DP-1"]["review_iteration"] == 2
        history = sm.get_state()["tickets"]["DP-1"]["history"]
        assert any(
            e.get("result") == "review_issues_found" and e.get("reason") == "Agent produced no output"
            for e in history
        )


class TestJiraTransitionOutcome:
    def test_ship_jira_failure_sets_flag_but_advances_to_done(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "pr"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch.object(sm, "_do_jira_transition", return_value=False), \
             patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            # The "done" step leads to select_ticket which needs pending tickets
            # So we check mid-state: after process_result for "pr" step
            sm.process_result(success=True, output="https://github.com/pr/1")
        assert sm.get_state()["tickets"]["DP-1"].get("jira_transition_failed") is True
        history = sm.get_state()["tickets"]["DP-1"]["history"]
        assert any(e.get("result") == "jira_transition_failed" for e in history)

    def test_ship_jira_success_no_failure_flag(self, tmp_path):
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "pr"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch.object(sm, "_do_jira_transition", return_value=True), \
             patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            sm.process_result(success=True, output="https://github.com/pr/1")
        assert "jira_transition_failed" not in sm.get_state()["tickets"]["DP-1"]
        history = sm.get_state()["tickets"]["DP-1"]["history"]
        assert any(e.get("result") == "jira_transition_ok" for e in history)

    def test_handle_ship_recovers_from_jira_state(self, tmp_path):
        """ship_step='jira' on disk (crash recovery) must not return an Error."""
        state = _fresh_state()
        state["phase"] = "ship"
        state["tickets"]["DP-1"]["ship_step"] = "jira"
        state["tickets"]["DP-1"]["branch"] = "DP-1"
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        with patch.object(sm, "_do_jira_transition", return_value=True), \
             patch("lib.state_machine._PROMPTS_DIR", str(tmp_path)), \
             patch("lib.state_machine._TMP_DIR", str(tmp_path)), \
             patch("lib.state_machine.compose_prompt", return_value="prompt"):
            action = sm.next_action()
        # Should not be an error — must advance past "jira" to "done" then to next phase
        assert action.action != "error"


class TestBuildPrBody:
    def test_malformed_history_entry_no_crash(self, tmp_path):
        """An empty or partial history entry must not raise KeyError."""
        state = _fresh_state()
        state["tickets"]["DP-1"]["history"] = [
            {},  # completely empty
            {"phase": "code_loop"},  # missing iteration and result
        ]
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        ticket = sm.get_state()["tickets"]["DP-1"]
        body = sm._build_pr_body(ticket)
        assert "unknown" in body
        assert "?" in body
        assert "no result recorded" in body

    def test_well_formed_history_renders_correctly(self, tmp_path):
        state = _fresh_state()
        state["tickets"]["DP-1"]["history"] = [
            {"phase": "code_loop", "iteration": 1, "result": "code_ran"},
        ]
        path = _make_state_file(tmp_path, state)
        from lib.state_machine import StateMachine
        sm = StateMachine(_make_config(), path)
        ticket = sm.get_state()["tickets"]["DP-1"]
        body = sm._build_pr_body(ticket)
        assert "code_loop iteration 1: code_ran" in body
