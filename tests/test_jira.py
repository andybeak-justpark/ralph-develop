import sys
from unittest.mock import MagicMock, patch

import pytest
import requests


def _make_config(todo_status="To Do", ac_field=None):
    cfg = MagicMock()
    cfg.jira.base_url = "https://test.atlassian.net"
    cfg.jira.email = "test@example.com"
    cfg.jira.api_token = "secret"
    cfg.jira.todo_status = todo_status
    cfg.jira.in_review_status = "In Review"
    cfg.jira.acceptance_criteria_field = ac_field
    return cfg


def _make_issue(key, summary, status="To Do"):
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": None,
            "status": {"name": status},
        },
    }


class TestGetEpicSubtasks:
    def test_returns_todo_tickets_only(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)
        issues = [
            _make_issue("DP-1", "Ticket 1", "To Do"),
            _make_issue("DP-2", "Ticket 2", "In Progress"),
            _make_issue("DP-3", "Ticket 3", "To Do"),
        ]
        response = MagicMock()
        response.status_code = 200
        response.content = b"x"
        response.json.return_value = {"issues": issues}
        response.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=response):
            result = client.get_epic_subtasks("DP-100")

        keys = [t["key"] for t in result]
        assert "DP-1" in keys
        assert "DP-3" in keys
        assert "DP-2" not in keys

    def test_falls_back_to_epic_link_jql(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.content = b"x"
        empty_response.json.return_value = {"issues": []}
        empty_response.raise_for_status = MagicMock()

        epic_link_response = MagicMock()
        epic_link_response.status_code = 200
        epic_link_response.content = b"x"
        epic_link_response.json.return_value = {"issues": [_make_issue("DP-1", "T1")]}
        epic_link_response.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request", side_effect=[empty_response, epic_link_response]
        ):
            result = client.get_epic_subtasks("DP-100")

        assert len(result) == 1
        assert result[0]["key"] == "DP-1"

    def test_auth_headers_are_set(self):
        from lib.jira import JiraClient
        from requests.auth import HTTPBasicAuth
        cfg = _make_config()
        client = JiraClient(cfg)
        assert client._session.auth.username == "test@example.com"
        assert client._session.auth.password == "secret"


class TestGetTicket:
    def test_returns_ticket_fields(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)
        response = MagicMock()
        response.status_code = 200
        response.content = b"x"
        response.json.return_value = {
            "fields": {
                "summary": "My ticket",
                "description": "Plain text description",
                "status": {"name": "To Do"},
            }
        }
        response.raise_for_status = MagicMock()
        with patch.object(client._session, "request", return_value=response):
            ticket = client.get_ticket("DP-1")
        assert ticket["summary"] == "My ticket"
        assert ticket["description"] == "Plain text description"
        assert ticket["status"] == "To Do"

    def test_extracts_adf_description(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello ADF"}],
                }
            ],
        }
        response = MagicMock()
        response.status_code = 200
        response.content = b"x"
        response.json.return_value = {"fields": {"summary": "T", "description": adf, "status": {"name": "To Do"}}}
        response.raise_for_status = MagicMock()
        with patch.object(client._session, "request", return_value=response):
            ticket = client.get_ticket("DP-1")
        assert "Hello ADF" in ticket["description"]


class TestTransitionTicket:
    def test_transitions_successfully(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        transitions_response = MagicMock()
        transitions_response.status_code = 200
        transitions_response.content = b"x"
        transitions_response.json.return_value = {
            "transitions": [
                {"id": "31", "name": "In Review"},
                {"id": "41", "name": "Done"},
            ]
        }
        transitions_response.raise_for_status = MagicMock()

        transition_response = MagicMock()
        transition_response.status_code = 204
        transition_response.content = b""
        transition_response.raise_for_status = MagicMock()

        with patch.object(
            client._session, "request",
            side_effect=[transitions_response, transition_response]
        ):
            result = client.transition_ticket("DP-1", "In Review")

        assert result is True

    def test_returns_false_when_post_fails(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        transitions_response = MagicMock()
        transitions_response.status_code = 200
        transitions_response.content = b"x"
        transitions_response.json.return_value = {
            "transitions": [{"id": "31", "name": "In Review"}]
        }
        transitions_response.raise_for_status = MagicMock()

        # POST returns a 503, causing _request_with_retry to return None after retries
        error_response = MagicMock()
        error_response.status_code = 503
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=error_response
        )

        with patch("lib.jira.time.sleep"), patch.object(
            client._session, "request",
            side_effect=[transitions_response] + [error_response] * 4,  # GET + 4 POST attempts
        ):
            result = client.transition_ticket("DP-1", "In Review")

        assert result is False

    def test_returns_false_when_no_matching_transition(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        transitions_response = MagicMock()
        transitions_response.status_code = 200
        transitions_response.content = b"x"
        transitions_response.json.return_value = {
            "transitions": [{"id": "41", "name": "Done"}]
        }
        transitions_response.raise_for_status = MagicMock()

        with patch.object(client._session, "request", return_value=transitions_response):
            result = client.transition_ticket("DP-1", "In Review")

        assert result is False


class TestRetryLogic:
    def test_retries_on_server_error(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        error_response = MagicMock()
        error_response.status_code = 500
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=error_response
        )

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.content = b"x"
        success_response.json.return_value = {"issues": []}
        success_response.raise_for_status = MagicMock()

        with patch("lib.jira.time.sleep"), patch.object(
            client._session, "request", side_effect=[error_response, success_response]
        ):
            result = client._request_with_retry("GET", "https://x/api", fatal=False)

        assert result == {"issues": []}

    def test_exits_3_after_all_retries_fatal(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        error_response = MagicMock()
        error_response.status_code = 503
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=error_response
        )

        with patch("lib.jira.time.sleep"), patch.object(
            client._session, "request", return_value=error_response
        ):
            with pytest.raises(SystemExit) as exc_info:
                client._request_with_retry("GET", "https://x/api", fatal=True)
        assert exc_info.value.code == 3

    def test_no_retry_on_4xx(self):
        from lib.jira import JiraClient
        cfg = _make_config()
        client = JiraClient(cfg)

        error_response = MagicMock()
        error_response.status_code = 403
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=error_response
        )

        call_count = 0

        def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return error_response

        with patch("lib.jira.time.sleep"), patch.object(
            client._session, "request", side_effect=fake_request
        ):
            with pytest.raises(SystemExit):
                client._request_with_retry("GET", "https://x/api", fatal=True)

        assert call_count == 1  # No retries for 4xx
