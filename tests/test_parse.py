import pytest


# ---- Validation parsing ----

def test_validation_exact_match():
    from lib.parse import parse_validation_output
    r = parse_validation_output("Some output\nVALIDATION PASSED\nMore text")
    assert r.passed is True
    assert r.signal_found is True


def test_validation_exact_match_with_whitespace():
    from lib.parse import parse_validation_output
    r = parse_validation_output("  VALIDATION PASSED  ")
    assert r.passed is True


def test_validation_relaxed_match_case_insensitive():
    from lib.parse import parse_validation_output
    r = parse_validation_output("The result is: validation passed, congrats")
    assert r.passed is True
    assert r.signal_found is True


def test_validation_inside_markdown():
    from lib.parse import parse_validation_output
    r = parse_validation_output("**VALIDATION PASSED**")
    assert r.passed is True


def test_validation_inside_code_block():
    from lib.parse import parse_validation_output
    r = parse_validation_output("```\nVALIDATION PASSED\n```")
    assert r.passed is True


def test_validation_absent_signal():
    from lib.parse import parse_validation_output
    r = parse_validation_output("All requirements checked but issues found.\nTests are failing.")
    assert r.passed is False
    assert r.signal_found is False


def test_validation_failure_reason_extracted():
    from lib.parse import parse_validation_output
    text = "Requirement 3 is not implemented.\nMissing database migration."
    r = parse_validation_output(text)
    assert r.passed is False
    assert r.failure_reason != ""
    assert r.failure_reason != "No clear failure reason found — see log file."


def test_validation_no_signal_fallback_reason():
    from lib.parse import parse_validation_output
    r = parse_validation_output("The code looks good overall.")
    assert r.passed is False
    assert "log file" in r.failure_reason


# ---- Review parsing ----

def test_review_exact_match():
    from lib.parse import parse_review_output
    r = parse_review_output("All done.\n-=REVIEW COMPLETE=-\n")
    assert r.passed is True
    assert r.signal_found is True


def test_review_exact_match_with_whitespace():
    from lib.parse import parse_review_output
    r = parse_review_output("  -=REVIEW COMPLETE=-  ")
    assert r.passed is True


def test_review_old_signal_no_longer_matches():
    # Plain "REVIEW COMPLETE" without the distinctive punctuation must not pass.
    from lib.parse import parse_review_output
    r = parse_review_output("REVIEW COMPLETE")
    assert r.passed is False


def test_review_partial_punctuation_no_match():
    # Only one side of the punctuation must not match.
    from lib.parse import parse_review_output
    r = parse_review_output("-=REVIEW COMPLETE")
    assert r.passed is False


def test_review_embedded_in_prose_no_match():
    # Signal embedded in a sentence must not trigger a pass.
    from lib.parse import parse_review_output
    r = parse_review_output("The review is complete and the code looks good.")
    assert r.passed is False


def test_review_absent_signal():
    from lib.parse import parse_review_output
    r = parse_review_output("Found 2 critical issues and 1 high issue.")
    assert r.passed is False
    assert r.signal_found is False


def test_review_severity_counts():
    from lib.parse import parse_review_output
    text = "There is 1 critical issue, 2 high issues, and 3 medium issues."
    r = parse_review_output(text)
    assert r.issue_counts.get("critical", 0) >= 1
    assert r.issue_counts.get("high", 0) >= 1
    assert r.issue_counts.get("medium", 0) >= 1


def test_review_no_issues_signal_absent():
    from lib.parse import parse_review_output
    r = parse_review_output("I made some fixes but haven't finished yet.")
    assert r.passed is False
    assert "log file" in r.failure_reason
