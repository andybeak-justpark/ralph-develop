import logging
import re
import sys
import time
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_RETRY_DELAYS = [2, 4, 8]


class JiraError(Exception):
    pass


class JiraClient:
    def __init__(self, config):
        self._base_url = config.jira.base_url
        self._auth = HTTPBasicAuth(config.jira.email, config.jira.api_token)
        self._todo_status = config.jira.todo_status
        self._in_progress_status = config.jira.in_progress_status
        self._in_review_status = config.jira.in_review_status
        self._ac_field = config.jira.acceptance_criteria_field
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_epic_subtasks(self, epic_key: str) -> list[dict]:
        """Return sub-ticket dicts for the epic with todo_status or in_progress_status."""
        tickets = self._fetch_subtasks(epic_key)
        return [t for t in tickets if t["status"] in (self._todo_status, self._in_progress_status)]

    def get_all_epic_tickets(self, epic_key: str) -> list[dict]:
        """Return all sub-ticket dicts for the epic (all statuses)."""
        return self._fetch_subtasks(epic_key)

    def update_description(self, ticket_key: str, new_description: str) -> bool:
        """Update the description of a ticket. Returns True on success."""
        url = f"{self._base_url}/rest/api/3/issue/{ticket_key}"
        body = {"fields": {"description": _text_to_adf(new_description)}}
        result = self._request_with_retry("PUT", url, json=body, fatal=False)
        return result is not None

    def get_ticket(self, ticket_key: str) -> dict:
        """Return a dict with summary, description, acceptance_criteria, status."""
        url = f"{self._base_url}/rest/api/3/issue/{ticket_key}"
        fields = ["summary", "description", "status"]
        if self._ac_field:
            fields.append(self._ac_field)
        params = {"fields": ",".join(fields)}
        data = self._request_with_retry("GET", url, params=params, fatal=True)
        fields_data = data.get("fields", {})
        description = self._extract_text(fields_data.get("description"))
        ac = ""
        if self._ac_field:
            ac = self._extract_text(fields_data.get(self._ac_field))
        return {
            "key": ticket_key,
            "summary": fields_data.get("summary", ""),
            "description": description,
            "acceptance_criteria": ac,
            "status": fields_data.get("status", {}).get("name", ""),
        }

    def fetch_confluence_page(self, title: str) -> Optional[str]:
        """Fetch a Confluence page by title. Returns plain text content or None."""
        url = f"{self._base_url}/wiki/rest/api/content"
        params = {"title": title, "type": "page", "expand": "body.view", "limit": 1}
        data = self._request_with_retry("GET", url, params=params, fatal=False)
        if data is None:
            return None
        results = data.get("results", [])
        if not results:
            return None
        html = results[0].get("body", {}).get("view", {}).get("value", "")
        return _html_to_text(html)

    def transition_ticket(self, ticket_key: str, target_status: str) -> bool:
        """Transition a ticket to the target status. Returns True on success."""
        transitions = self._get_transitions(ticket_key)
        transition_id = None
        for t in transitions:
            if t.get("name", "").lower() == target_status.lower():
                transition_id = t["id"]
                break
        if not transition_id:
            logger.warning(
                "No transition to '%s' found for %s. Available: %s",
                target_status,
                ticket_key,
                [t.get("name") for t in transitions],
            )
            return False
        url = f"{self._base_url}/rest/api/3/issue/{ticket_key}/transitions"
        result = self._request_with_retry(
            "POST", url, json={"transition": {"id": transition_id}}, fatal=False
        )
        if result is None:
            logger.warning("Jira transition POST failed for %s", ticket_key)
            return False
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_subtasks(self, epic_key: str) -> list[dict]:
        """Try parent= JQL first, fall back to Epic Link."""
        tickets = self._jql_search(f'parent = "{epic_key}" ORDER BY created ASC')
        if not tickets:
            tickets = self._jql_search(f'"Epic Link" = "{epic_key}" ORDER BY created ASC')
        return tickets

    def _jql_search(self, jql: str) -> list[dict]:
        url = f"{self._base_url}/rest/api/3/search/jql"
        fields = ["summary", "description", "status"]
        if self._ac_field:
            fields.append(self._ac_field)
        body = {"jql": jql, "fields": fields, "maxResults": 100}
        try:
            data = self._request_with_retry("POST", url, json=body, fatal=True)
        except SystemExit:
            raise
        except Exception:
            return []
        issues = data.get("issues", [])
        result = []
        for issue in issues:
            f = issue.get("fields", {})
            result.append({
                "key": issue["key"],
                "summary": f.get("summary", ""),
                "description": self._extract_text(f.get("description")),
                "acceptance_criteria": self._extract_text(f.get(self._ac_field)) if self._ac_field else "",
                "status": f.get("status", {}).get("name", ""),
            })
        return result

    def _get_transitions(self, ticket_key: str) -> list[dict]:
        url = f"{self._base_url}/rest/api/3/issue/{ticket_key}/transitions"
        data = self._request_with_retry("GET", url, fatal=False)
        if data is None:
            return []
        return data.get("transitions", [])

    def _request_with_retry(self, method: str, url: str, fatal: bool = True, **kwargs):
        """Make an HTTP request, retrying on failure with exponential backoff."""
        last_exc = None
        for attempt, delay in enumerate([0] + _RETRY_DELAYS):
            if delay:
                logger.info("Retrying in %ds (attempt %d)...", delay, attempt)
                time.sleep(delay)
            try:
                response = self._session.request(method, url, timeout=30, **kwargs)
                response.raise_for_status()
                if response.content:
                    return response.json()
                return {}
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 0
                # 4xx errors (except 429) are not retryable
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    logger.error("Jira API error %s: %s", status_code, url)
                    if fatal:
                        sys.exit(3)
                    return None
                last_exc = e
                logger.warning("Jira request failed (%s): %s", status_code, e)
            except requests.exceptions.RequestException as e:
                last_exc = e
                logger.warning("Jira request exception: %s", e)

        logger.error("Jira API call failed after retries: %s %s", method, url)
        if fatal:
            sys.exit(3)
        return None

    @staticmethod
    def _extract_text(field_value) -> str:
        """Extract plain text from Atlassian Document Format or plain string."""
        if field_value is None:
            return ""
        if isinstance(field_value, str):
            return field_value
        if isinstance(field_value, dict):
            # Atlassian Document Format
            return _adf_to_text(field_value)
        return str(field_value)


def _text_to_adf(text: str) -> dict:
    """Convert plain text to Atlassian Document Format."""
    content = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        para_nodes: list = []
        for i, line in enumerate(lines):
            if line:
                para_nodes.append({"type": "text", "text": line})
            if i < len(lines) - 1:
                para_nodes.append({"type": "hardBreak"})
        if para_nodes:
            content.append({"type": "paragraph", "content": para_nodes})
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"type": "doc", "version": 1, "content": content}


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text by stripping tags."""
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</(?:p|div|h[1-6]|li|tr|td|th)>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', '', html)
    html = re.sub(r'&nbsp;', ' ', html)
    html = re.sub(r'&amp;', '&', html)
    html = re.sub(r'&lt;', '<', html)
    html = re.sub(r'&gt;', '>', html)
    html = re.sub(r'&quot;', '"', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _adf_to_text(node: dict) -> str:
    """Recursively extract text from an Atlassian Document Format node."""
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    parts = []
    for child in node.get("content", []):
        parts.append(_adf_to_text(child))
    separator = "\n" if node_type in ("paragraph", "bulletList", "orderedList", "listItem", "doc") else ""
    return separator.join(p for p in parts if p)
