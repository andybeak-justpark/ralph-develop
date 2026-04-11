import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParseResult:
    passed: bool
    signal_found: bool
    failure_reason: str = ""
    issue_counts: dict = field(default_factory=dict)


_SEVERITY_PATTERNS = {
    "critical": re.compile(r"\bcritical\b", re.IGNORECASE),
    "high": re.compile(r"\bhigh\b", re.IGNORECASE),
    "medium": re.compile(r"\bmedium\b", re.IGNORECASE),
}


def _find_signal(text: str, signal: str) -> bool:
    """Two-pass signal search: exact standalone line, then relaxed case-insensitive."""
    # Pass 1: exact standalone line (stripped)
    for line in text.splitlines():
        if line.strip() == signal:
            return True
    # Pass 2: relaxed — signal appears anywhere, case-insensitive
    if signal.lower() in text.lower():
        return True
    return False


def _extract_failure_reason(text: str) -> str:
    """Extract a short failure reason from validator output.

    Looks for lines that describe a failing requirement.
    """
    failure_markers = [
        r"\bfail(?:ed|s|ing)?\b",
        r"\bmissing\b",
        r"\bnot\s+(?:found|implemented|present|met)\b",
        r"\brequirement.*not",
        r"\bdoes\s+not\s+(?:exist|pass|meet)\b",
    ]
    pattern = re.compile("|".join(failure_markers), re.IGNORECASE)
    reasons = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and pattern.search(stripped):
            reasons.append(stripped)
            if len(reasons) >= 3:
                break
    if reasons:
        return "; ".join(reasons)
    return "No clear failure reason found — see log file."


def _count_severities(text: str) -> dict:
    """Count occurrences of each severity label in text."""
    counts = {}
    for severity, pattern in _SEVERITY_PATTERNS.items():
        count = len(pattern.findall(text))
        if count:
            counts[severity] = count
    return counts


def parse_validation_output(text: str) -> ParseResult:
    """Parse Claude Code output from the validator agent."""
    signal = "VALIDATION PASSED"
    found = _find_signal(text, signal)
    if found:
        return ParseResult(passed=True, signal_found=True)
    reason = _extract_failure_reason(text)
    return ParseResult(passed=False, signal_found=False, failure_reason=reason)


def parse_review_output(text: str) -> ParseResult:
    """Parse Claude Code output from the reviewer agent."""
    signal = "REVIEW COMPLETE"
    found = _find_signal(text, signal)
    issue_counts = _count_severities(text)
    if found:
        return ParseResult(passed=True, signal_found=True, issue_counts=issue_counts)
    reason = (
        f"Review not complete. Issue counts: {issue_counts}"
        if issue_counts
        else "No clear signal in output — see log file."
    )
    return ParseResult(
        passed=False, signal_found=False, failure_reason=reason, issue_counts=issue_counts
    )
