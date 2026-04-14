import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


def _validate_positive_int(value, field_name: str) -> int:
    """Cast value to int and ensure it is positive, exiting with code 3 on invalid input."""
    try:
        result = int(value)
    except (ValueError, TypeError):
        logger.error("Config field '%s' must be a valid integer, got: %r", field_name, value)
        sys.exit(3)
    if result <= 0:
        logger.error("Config field '%s' must be a positive integer, got: %d", field_name, result)
        sys.exit(3)
    return result


@dataclass
class JiraConfig:
    base_url: str
    email: str
    api_token: str  # resolved from env var
    todo_status: str = "To Do"
    in_progress_status: str = "In Progress"
    in_review_status: str = "In Review"
    acceptance_criteria_field: Optional[str] = None


@dataclass
class GitConfig:
    base_branch: str = "master"
    worktree_root: str = "/worktrees"
    branch_pattern: str = "{ticket_key}"
    commit_message_pattern: str = "{ticket_key}: {ticket_summary}"
    git_crypt_key: Optional[str] = None


@dataclass
class AgentConfig:
    claude_command: str = "claude"
    # Note: --print / -p is always passed by the supervisor when invoking the agent,
    # so it should not be included here.
    claude_args: list = field(default_factory=lambda: ["--dangerously-skip-permissions"])


@dataclass
class LimitsConfig:
    max_code_iterations: int = 5
    max_review_iterations: int = 5
    max_ticket_failures: int = 2


@dataclass
class SupervisorConfig:
    max_crashes: int = 5
    cooldown_seconds: int = 10


@dataclass
class Config:
    jira: JiraConfig
    git: GitConfig
    agent: AgentConfig
    limits: LimitsConfig
    supervisor: SupervisorConfig
    config_hash: str = ""
    epic_key: str = ""


def load_config(path: str = "/app/config.yaml") -> Config:
    try:
        with open(path, "r") as f:
            raw = f.read()
    except FileNotFoundError:
        logger.error("Config file not found: %s", path)
        sys.exit(3)
    except OSError as e:
        logger.error("Cannot read config file %s: %s", path, e)
        sys.exit(3)

    config_hash = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in config file: %s", e)
        sys.exit(3)

    if not isinstance(data, dict):
        logger.error("Config file must be a YAML mapping")
        sys.exit(3)

    # --- Jira ---
    jira_data = data.get("jira", {})
    api_token_env = jira_data.get("api_token_env", "JIRA_API_TOKEN")
    api_token = os.environ.get(api_token_env, "")
    if not api_token:
        logger.error(
            "Jira API token not found. Set the %s environment variable.", api_token_env
        )
        sys.exit(3)
    base_url = jira_data.get("base_url", "")
    if not base_url:
        logger.error("jira.base_url is required in config.yaml")
        sys.exit(3)
    email = jira_data.get("email", "")
    if not email:
        logger.error("jira.email is required in config.yaml")
        sys.exit(3)

    jira = JiraConfig(
        base_url=base_url.rstrip("/"),
        email=email,
        api_token=api_token,
        todo_status=jira_data.get("todo_status", "To Do"),
        in_progress_status=jira_data.get("in_progress_status", "In Progress"),
        in_review_status=jira_data.get("in_review_status", "In Review"),
        acceptance_criteria_field=jira_data.get("acceptance_criteria_field") or None,
    )

    # --- Git ---
    git_data = data.get("git", {})
    git = GitConfig(
        base_branch=git_data.get("base_branch", "master"),
        worktree_root=git_data.get("worktree_root", "/worktrees"),
        branch_pattern=git_data.get("branch_pattern", "{ticket_key}"),
        commit_message_pattern=git_data.get(
            "commit_message_pattern", "{ticket_key}: {ticket_summary}"
        ),
        git_crypt_key=git_data.get("git_crypt_key") or None,
    )

    # --- Agent ---
    agent_data = data.get("agent", {})
    agent = AgentConfig(
        claude_command=agent_data.get("claude_command", "claude"),
        claude_args=agent_data.get(
            "claude_args", ["--dangerously-skip-permissions"]
        ),
    )

    if not isinstance(agent.claude_args, list):
        logger.error(
            "agent.claude_args must be a list, got: %s", type(agent.claude_args).__name__
        )
        sys.exit(3)

    # --- Limits ---
    limits_data = data.get("limits", {})
    limits = LimitsConfig(
        max_code_iterations=_validate_positive_int(limits_data.get("max_code_iterations", 5), "limits.max_code_iterations"),
        max_review_iterations=_validate_positive_int(limits_data.get("max_review_iterations", 5), "limits.max_review_iterations"),
        max_ticket_failures=_validate_positive_int(limits_data.get("max_ticket_failures", 2), "limits.max_ticket_failures"),
    )

    # --- Supervisor ---
    supervisor_data = data.get("supervisor", {})
    supervisor = SupervisorConfig(
        max_crashes=_validate_positive_int(supervisor_data.get("max_crashes", 5), "supervisor.max_crashes"),
        cooldown_seconds=_validate_positive_int(supervisor_data.get("cooldown_seconds", 10), "supervisor.cooldown_seconds"),
    )

    # Epic key (may be set later via CLI)
    epic_key = data.get("epic_key", "")

    return Config(
        jira=jira,
        git=git,
        agent=agent,
        limits=limits,
        supervisor=supervisor,
        config_hash=config_hash,
        epic_key=epic_key,
    )


def branch_name(config: Config, ticket_key: str, summary_slug: str = "") -> str:
    return config.git.branch_pattern.format(
        ticket_key=ticket_key, summary_slug=summary_slug
    )


def commit_message(config: Config, ticket_key: str, ticket_summary: str) -> str:
    return config.git.commit_message_pattern.format(
        ticket_key=ticket_key, ticket_summary=ticket_summary
    )


def slugify(text: str) -> str:
    import re
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:50]
