import dataclasses
import json
import sys
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunAgent:
    prompt_file: str
    log_file: str
    workdir: str
    action: str = field(default="run_agent", init=False)


@dataclass
class GitWorktreeCreate:
    path: str
    branch: str
    base: str
    action: str = field(default="git_worktree_create", init=False)


@dataclass
class GitCommitAndPush:
    workdir: str
    message: str
    branch: str
    action: str = field(default="git_commit_and_push", init=False)


@dataclass
class CreatePR:
    base: str
    head: str
    title: str
    body: str
    action: str = field(default="create_pr", init=False)


@dataclass
class CleanupWorktree:
    path: str
    action: str = field(default="cleanup_worktree", init=False)


@dataclass
class Complete:
    exit_code: int = 0
    action: str = field(default="complete", init=False)


@dataclass
class Error:
    message: str
    exit_code: int = 2
    action: str = field(default="error", init=False)


Action = (
    RunAgent
    | GitWorktreeCreate
    | GitCommitAndPush
    | CreatePR
    | CleanupWorktree
    | Complete
    | Error
)


def emit(action) -> None:
    """Serialize action to JSON and write to stdout. This is the ONLY stdout writer."""
    print(json.dumps(dataclasses.asdict(action)), flush=True)
