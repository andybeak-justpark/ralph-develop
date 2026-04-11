import os
import pytest


def test_substitute_simple():
    from lib.template import substitute
    result = substitute("Hello {{NAME}}!", {"NAME": "World"})
    assert result == "Hello World!"


def test_substitute_multiple():
    from lib.template import substitute
    result = substitute("{{A}} and {{B}}", {"A": "foo", "B": "bar"})
    assert result == "foo and bar"


def test_substitute_undefined_gives_empty_string(caplog):
    import logging
    from lib.template import substitute
    with caplog.at_level(logging.WARNING):
        result = substitute("Hello {{UNDEFINED}}!", {})
    assert result == "Hello !"
    assert "UNDEFINED" in caplog.text


def test_substitute_none_value():
    from lib.template import substitute
    result = substitute("{{VAR}}", {"VAR": None})
    assert result == ""


def test_substitute_no_placeholders():
    from lib.template import substitute
    result = substitute("plain text", {"A": "b"})
    assert result == "plain text"


def test_resolve_partials(tmp_path):
    from lib.template import resolve_partials
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    (partials_dir / "greeting.md").write_text("Hello from partial!")
    template = "Start\n{{> greeting}}\nEnd"
    result = resolve_partials(template, str(partials_dir))
    assert "Hello from partial!" in result
    assert "{{> greeting}}" not in result


def test_resolve_partials_missing_gives_empty(tmp_path, caplog):
    import logging
    from lib.template import resolve_partials
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    with caplog.at_level(logging.WARNING):
        result = resolve_partials("{{> nonexistent}}", str(partials_dir))
    assert result == ""
    assert "nonexistent" in caplog.text


def test_resolve_nested_partials(tmp_path):
    from lib.template import resolve_partials
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    (partials_dir / "inner.md").write_text("inner content")
    (partials_dir / "outer.md").write_text("outer: {{> inner}}")
    result = resolve_partials("{{> outer}}", str(partials_dir))
    assert "inner content" in result


def test_compose_prompt_assembly_order(tmp_path):
    from lib.template import compose_prompt
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    (partials_dir / "memory_header.md").write_text("HEADER {{MEMORY}}")
    (partials_dir / "memory_footer.md").write_text("FOOTER {{MEMORY_PATH}}")
    (tmp_path / "coder.md").write_text("BODY for {{TICKET}}")

    result = compose_prompt(
        "coder",
        {"MEMORY": "mem content", "MEMORY_PATH": "/app/MEMORY.md", "TICKET": "DP-1"},
        str(tmp_path),
    )

    header_pos = result.index("HEADER")
    body_pos = result.index("BODY")
    footer_pos = result.index("FOOTER")
    assert header_pos < body_pos < footer_pos
    assert "mem content" in result
    assert "/app/MEMORY.md" in result
    assert "DP-1" in result


def test_compose_prompt_missing_role_file(tmp_path):
    from lib.template import compose_prompt
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    (partials_dir / "memory_header.md").write_text("HEADER")
    (partials_dir / "memory_footer.md").write_text("FOOTER")
    # No coder.md — should not crash
    result = compose_prompt("coder", {}, str(tmp_path))
    assert "HEADER" in result
    assert "FOOTER" in result


def test_compose_prompt_variable_substitution(tmp_path):
    from lib.template import compose_prompt
    partials_dir = tmp_path / "partials"
    partials_dir.mkdir()
    (partials_dir / "memory_header.md").write_text("")
    (partials_dir / "memory_footer.md").write_text("")
    (tmp_path / "validator.md").write_text("Ticket: {{TICKET}}, Branch: {{BRANCH}}")

    result = compose_prompt(
        "validator",
        {"TICKET": "DP-5", "BRANCH": "DP-5"},
        str(tmp_path),
    )
    assert "DP-5" in result
