import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")
_PARTIAL_RE = re.compile(r"\{\{>\s*([a-z_][a-z0-9_]*)\s*\}\}")


def resolve_partials(template: str, partials_dir: str) -> str:
    """Replace {{> name}} includes with the content of partials_dir/name.md."""

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        path = os.path.join(partials_dir, f"{name}.md")
        try:
            with open(path, "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning("Partial not found: %s", path)
            return ""

    # Resolve up to two levels of nesting (partials that include other partials)
    for _ in range(2):
        expanded = _PARTIAL_RE.sub(_replace, template)
        if expanded == template:
            break
        template = expanded
    return template


def substitute(template: str, variables: dict) -> str:
    """Replace {{VAR}} placeholders with values from the variables dict.

    Undefined variables are replaced with an empty string and a warning is logged.
    """

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in variables:
            value = variables[name]
            return str(value) if value is not None else ""
        logger.warning("Template variable {{%s}} is not defined; substituting empty string", name)
        return ""

    return _VAR_RE.sub(_replace, template)


def compose_prompt(
    agent_role: str,
    variables: dict,
    prompts_dir: str,
) -> str:
    """Assemble the full prompt: memory_header + agent template + memory_footer.

    Partials are resolved first, then variable substitution is applied to the
    entire composed string.
    """
    partials_dir = os.path.join(prompts_dir, "partials")

    def _load(path: str) -> str:
        try:
            with open(path, "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning("Prompt file not found: %s", path)
            return ""

    header = _load(os.path.join(partials_dir, "memory_header.md"))
    body = _load(os.path.join(prompts_dir, f"{agent_role}.md"))
    footer = _load(os.path.join(partials_dir, "memory_footer.md"))

    combined = "\n\n".join(part for part in [header, body, footer] if part)
    combined = resolve_partials(combined, partials_dir)
    combined = substitute(combined, variables)
    return combined
