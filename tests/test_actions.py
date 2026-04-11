import io
import json
import sys

import pytest


def test_run_agent_serialization():
    from lib.actions import RunAgent, emit
    a = RunAgent(prompt_file="/tmp/prompt.md", log_file="logs/x.md", workdir="/worktrees/DP-1")
    assert a.action == "run_agent"
    import dataclasses
    d = dataclasses.asdict(a)
    assert d["action"] == "run_agent"
    assert d["prompt_file"] == "/tmp/prompt.md"
    assert d["log_file"] == "logs/x.md"
    assert d["workdir"] == "/worktrees/DP-1"


def test_git_worktree_create_serialization():
    from lib.actions import GitWorktreeCreate
    a = GitWorktreeCreate(path="/worktrees/DP-1", branch="DP-1", base="main")
    assert a.action == "git_worktree_create"
    import dataclasses
    d = dataclasses.asdict(a)
    assert d["path"] == "/worktrees/DP-1"
    assert d["branch"] == "DP-1"
    assert d["base"] == "main"


def test_git_commit_and_push_serialization():
    from lib.actions import GitCommitAndPush
    a = GitCommitAndPush(workdir="/worktrees/DP-1", message="DP-1: msg", branch="DP-1")
    assert a.action == "git_commit_and_push"


def test_create_pr_serialization():
    from lib.actions import CreatePR
    a = CreatePR(base="main", head="DP-1", title="DP-1: title", body="body text")
    assert a.action == "create_pr"
    import dataclasses
    d = dataclasses.asdict(a)
    assert d["base"] == "main"
    assert d["head"] == "DP-1"
    assert d["body"] == "body text"


def test_cleanup_worktree_serialization():
    from lib.actions import CleanupWorktree
    a = CleanupWorktree(path="/worktrees/DP-1")
    assert a.action == "cleanup_worktree"


def test_complete_serialization():
    from lib.actions import Complete
    a = Complete(exit_code=0)
    assert a.action == "complete"
    assert a.exit_code == 0


def test_error_serialization():
    from lib.actions import Error
    a = Error(message="something broke", exit_code=2)
    assert a.action == "error"
    assert a.exit_code == 2


def test_emit_writes_to_stdout(capsys):
    from lib.actions import Complete, emit
    emit(Complete(exit_code=0))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["action"] == "complete"
    assert data["exit_code"] == 0
    assert captured.err == ""


def test_emit_run_agent_json(capsys):
    from lib.actions import RunAgent, emit
    emit(RunAgent(prompt_file="/p.md", log_file="logs/a.md", workdir="/w"))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["action"] == "run_agent"
    assert data["prompt_file"] == "/p.md"


def test_emit_error_json(capsys):
    from lib.actions import Error, emit
    emit(Error(message="oops", exit_code=3))
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["action"] == "error"
    assert data["exit_code"] == 3
    assert data["message"] == "oops"
