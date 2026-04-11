import os
import sys
import tempfile
import textwrap

import pytest
import yaml


def _write_config(tmp_path, extra=None):
    data = {
        "jira": {
            "base_url": "https://test.atlassian.net",
            "email": "test@example.com",
            "api_token_env": "TEST_JIRA_TOKEN",
            "todo_status": "To Do",
            "in_review_status": "In Review",
        },
        "git": {
            "base_branch": "main",
            "worktree_root": "/worktrees",
            "branch_pattern": "{ticket_key}",
        },
    }
    if extra:
        data.update(extra)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


def test_load_config_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "secret123")
    path = _write_config(tmp_path)

    from lib.config import load_config
    cfg = load_config(path)

    assert cfg.jira.base_url == "https://test.atlassian.net"
    assert cfg.jira.email == "test@example.com"
    assert cfg.jira.api_token == "secret123"
    assert cfg.jira.todo_status == "To Do"
    assert cfg.git.base_branch == "main"
    assert cfg.config_hash.startswith("sha256:")


def test_load_config_missing_token_exits(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_JIRA_TOKEN", raising=False)
    path = _write_config(tmp_path)

    from lib.config import load_config
    with pytest.raises(SystemExit) as exc_info:
        load_config(path)
    assert exc_info.value.code == 3


def test_load_config_missing_base_url_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "jira": {"email": "a@b.com", "api_token_env": "TEST_JIRA_TOKEN"},
    }))

    from lib.config import load_config
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(p))
    assert exc_info.value.code == 3


def test_load_config_missing_file_exits(tmp_path):
    from lib.config import load_config
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(tmp_path / "nonexistent.yaml"))
    assert exc_info.value.code == 3


def test_load_config_invalid_yaml_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
    p = tmp_path / "config.yaml"
    p.write_text(": : : invalid yaml {{{{")

    from lib.config import load_config
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(p))
    assert exc_info.value.code == 3


def test_config_hash_is_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
    path = _write_config(tmp_path)

    from lib.config import load_config
    cfg1 = load_config(path)
    cfg2 = load_config(path)
    assert cfg1.config_hash == cfg2.config_hash


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "jira": {
            "base_url": "https://x.atlassian.net",
            "email": "a@b.com",
            "api_token_env": "TEST_JIRA_TOKEN",
        },
    }))

    from lib.config import load_config
    cfg = load_config(str(p))
    assert cfg.limits.max_code_iterations == 5
    assert cfg.limits.max_review_iterations == 5
    assert cfg.limits.max_ticket_failures == 2
    assert cfg.supervisor.max_crashes == 5
    assert cfg.supervisor.cooldown_seconds == 10


def test_branch_name_and_commit_message(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
    path = _write_config(tmp_path)

    from lib.config import load_config, branch_name, commit_message
    cfg = load_config(path)

    assert branch_name(cfg, "DP-203") == "DP-203"
    assert commit_message(cfg, "DP-203", "Add endpoint") == "DP-203: Add endpoint"


def test_slugify():
    from lib.config import slugify
    assert slugify("Add shadow pricing endpoint") == "add-shadow-pricing-endpoint"
    assert slugify("Hello World!") == "hello-world"
    assert slugify("  spaces  ") == "spaces"


class TestConfigValidation:
    def _base_data(self, monkeypatch):
        monkeypatch.setenv("TEST_JIRA_TOKEN", "tok")
        return {
            "jira": {
                "base_url": "https://test.atlassian.net",
                "email": "test@example.com",
                "api_token_env": "TEST_JIRA_TOKEN",
            },
        }

    def _write_yaml(self, tmp_path, data):
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump(data))
        return str(p)

    def test_non_numeric_limit_exits_3(self, tmp_path, monkeypatch):
        data = self._base_data(monkeypatch)
        data["limits"] = {"max_code_iterations": "abc"}
        from lib.config import load_config
        with pytest.raises(SystemExit) as exc_info:
            load_config(self._write_yaml(tmp_path, data))
        assert exc_info.value.code == 3

    def test_zero_limit_exits_3(self, tmp_path, monkeypatch):
        data = self._base_data(monkeypatch)
        data["limits"] = {"max_code_iterations": 0}
        from lib.config import load_config
        with pytest.raises(SystemExit) as exc_info:
            load_config(self._write_yaml(tmp_path, data))
        assert exc_info.value.code == 3

    def test_negative_limit_exits_3(self, tmp_path, monkeypatch):
        data = self._base_data(monkeypatch)
        data["limits"] = {"max_code_iterations": -1}
        from lib.config import load_config
        with pytest.raises(SystemExit) as exc_info:
            load_config(self._write_yaml(tmp_path, data))
        assert exc_info.value.code == 3

    def test_claude_args_string_exits_3(self, tmp_path, monkeypatch):
        data = self._base_data(monkeypatch)
        data["agent"] = {"claude_args": "--dangerously-skip-permissions"}
        from lib.config import load_config
        with pytest.raises(SystemExit) as exc_info:
            load_config(self._write_yaml(tmp_path, data))
        assert exc_info.value.code == 3
