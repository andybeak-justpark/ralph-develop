#!/usr/bin/env python3
"""Epic Runner — Python state machine entry point.

This process is run inside Docker. It reads state.json, decides the next
action, and emits a single JSON object to stdout. All logging goes to stderr.

Exit codes:
  0 — Action emitted successfully (normal loop)
  1 — Unhandled crash (supervisor will restart)
  2 — Max ticket failures exceeded (supervisor stops)
  3 — Config/auth error (supervisor stops, no restart)
"""

import argparse
import json
import logging
import os
import sys

# Configure logging to stderr before any imports that might log
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("EPIC_RUNNER_CONFIG", "/app/config.yaml")
STATE_PATH = os.environ.get("EPIC_RUNNER_STATE", "/app/state.json")


def cmd_next_action(args) -> int:
    from lib.config import load_config
    from lib.actions import emit
    from lib.state_machine import StateMachine

    config = load_config(CONFIG_PATH)
    sm = StateMachine(config, STATE_PATH)
    action = sm.next_action()
    emit(action)
    return 0


def cmd_process_result(args) -> int:
    from lib.config import load_config
    from lib.actions import emit
    from lib.state_machine import StateMachine

    config = load_config(CONFIG_PATH)
    sm = StateMachine(config, STATE_PATH)

    log_file = getattr(args, "log", None)
    success = None
    failure_reason = ""
    output = getattr(args, "output", "") or ""

    if getattr(args, "action_success", False):
        success = True
    elif getattr(args, "action_failure", False):
        success = False
        failure_reason = getattr(args, "reason", "") or ""

    action = sm.process_result(
        log_file=log_file,
        success=success,
        failure_reason=failure_reason,
        output=output,
    )
    emit(action)
    return 0


def cmd_skip(args) -> int:
    from lib.config import load_config
    from lib.state_machine import StateMachine

    config = load_config(CONFIG_PATH)
    sm = StateMachine(config, STATE_PATH)
    sm.skip_ticket(args.ticket)
    logger.info("Ticket %s marked as skipped", args.ticket)
    return 0


def cmd_retry(args) -> int:
    from lib.config import load_config
    from lib.state_machine import StateMachine

    config = load_config(CONFIG_PATH)
    sm = StateMachine(config, STATE_PATH)
    sm.retry_ticket(args.ticket, args.phase)
    logger.info("Ticket %s queued for retry at phase %s", args.ticket, args.phase)
    return 0


def cmd_list_tickets(args) -> int:
    from lib.config import load_config
    from lib.jira import JiraClient

    config = load_config(CONFIG_PATH)
    jira = JiraClient(config)
    tickets = jira.get_all_epic_tickets(args.epic)
    print(json.dumps(tickets))
    return 0


def cmd_update_description(args) -> int:
    from lib.config import load_config
    from lib.jira import JiraClient

    config = load_config(CONFIG_PATH)
    jira = JiraClient(config)

    if args.description_file:
        with open(args.description_file) as f:
            description = f.read().strip()
    else:
        description = (args.description or "").strip()

    success = jira.update_description(args.ticket, description)
    if success:
        logger.info("Updated description for %s", args.ticket)
        return 0
    else:
        logger.error("Failed to update description for %s", args.ticket)
        return 1


def cmd_fetch_confluence(args) -> int:
    from lib.config import load_config
    from lib.jira import JiraClient

    config = load_config(CONFIG_PATH)
    jira = JiraClient(config)
    content = jira.fetch_confluence_page(args.title)
    if content is None:
        logger.error("Confluence page not found: %s", args.title)
        return 1
    print(content)
    return 0


def cmd_status(args) -> int:
    from lib.config import load_config
    from lib.state_machine import StateMachine

    config = load_config(CONFIG_PATH)
    sm = StateMachine(config, STATE_PATH)
    state = sm.get_state()

    print(f"Epic: {state.get('epic_key', 'unknown')}", file=sys.stderr)
    print(f"Phase: {state.get('phase', 'unknown')}", file=sys.stderr)
    print(f"Current ticket: {state.get('current_ticket', 'none')}", file=sys.stderr)
    print(f"Failed tickets: {state.get('failed_ticket_count', 0)}", file=sys.stderr)
    tickets = state.get("tickets", {})
    for key, info in tickets.items():
        print(f"  {key}: {info.get('status', '?')} — {info.get('summary', '')}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Epic Runner state machine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("next-action", help="Emit the next action to perform")

    pr = sub.add_parser("process-result", help="Process the result of an action")
    pr.add_argument("--log", metavar="FILE", help="Log file from agent run")
    pr.add_argument("--action-success", action="store_true", help="Non-agent action succeeded")
    pr.add_argument("--action-failure", action="store_true", help="Non-agent action failed")
    pr.add_argument("--reason", metavar="TEXT", help="Failure reason")
    pr.add_argument("--output", metavar="TEXT", help="Output from action (e.g. PR URL)")

    lt = sub.add_parser("list-tickets", help="List all tickets in an epic as JSON")
    lt.add_argument("--epic", required=True, metavar="KEY")

    fc = sub.add_parser("fetch-confluence", help="Fetch a Confluence page by title and print its text")
    fc.add_argument("--title", required=True, metavar="TITLE")

    ud = sub.add_parser("update-description", help="Update a ticket's description")
    ud.add_argument("--ticket", required=True, metavar="KEY")
    ud.add_argument("--description", metavar="TEXT", help="New description text")
    ud.add_argument("--description-file", metavar="FILE", help="File containing new description")

    sk = sub.add_parser("skip", help="Skip a ticket")
    sk.add_argument("--ticket", required=True, metavar="KEY")

    rt = sub.add_parser("retry", help="Retry a ticket from a given phase")
    rt.add_argument("--ticket", required=True, metavar="KEY")
    rt.add_argument("--phase", required=True, metavar="PHASE",
                    choices=["code_loop", "validate", "review_loop", "ship"])

    sub.add_parser("status", help="Print current status")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "next-action": cmd_next_action,
        "process-result": cmd_process_result,
        "list-tickets": cmd_list_tickets,
        "update-description": cmd_update_description,
        "fetch-confluence": cmd_fetch_confluence,
        "skip": cmd_skip,
        "retry": cmd_retry,
        "status": cmd_status,
    }

    if args.command is None:
        parser.print_help(sys.stderr)
        return 1

    handler = handlers.get(args.command)
    if handler is None:
        logger.error("Unknown command: %s", args.command)
        return 1

    return handler(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        logger.exception("Unhandled exception")
        sys.exit(1)
