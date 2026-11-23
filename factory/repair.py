"""Repair feedback generator for failed or rejected factory task attempts."""

from __future__ import annotations

import logging
from typing import Any

from factory.models import Review

logger = logging.getLogger(__name__)


def format_repair_feedback(
    outcome: Any,
    previous_findings: list[str] | None = None,
) -> str:
    """Prepare actionable repair instructions for a subsequent worker attempt.

    Args:
        outcome: Outcome of the previous attempt (Review object, Exception, error string, or dict).
        previous_findings: Optional list of findings accumulated from earlier attempts.

    Returns:
        Formatted markdown feedback note to be appended to worker instructions.
    """
    findings: list[str] = list(previous_findings or [])
    error_detail = ""

    if isinstance(outcome, Review):
        for f in outcome.findings:
            if f and f not in findings:
                findings.append(f)
        if not findings and not outcome.approved:
            findings.append("The diff review was rejected. Please verify all acceptance criteria.")
    elif isinstance(outcome, Exception):
        error_detail = f"{type(outcome).__name__}: {outcome}"
        output = getattr(outcome, "output", "")
        if output:
            error_detail += f"\nWorker Output:\n{output[-1000:]}"
    elif isinstance(outcome, dict):
        if "findings" in outcome and isinstance(outcome["findings"], list):
            for f in outcome["findings"]:
                if f and str(f) not in findings:
                    findings.append(str(f))
        if "error" in outcome and outcome["error"]:
            error_detail = str(outcome["error"])
    elif isinstance(outcome, str):
        error_detail = outcome
    elif outcome is not None:
        error_detail = str(outcome)

    lines = [
        "### REPAIR INSTRUCTIONS (Previous Attempt Failed)",
        "",
        "The previous attempt could not be merged into the project repository.",
    ]

    if error_detail:
        lines.append(f"**Previous Execution Issue:**\n```\n{error_detail.strip()}\n```")
        lines.append("")

    if findings:
        lines.append("**Specific Findings to Fix:**")
        for i, finding in enumerate(findings, 1):
            lines.append(f"{i}. {finding}")
        lines.append("")

    lines.extend([
        "**Required Actions:**",
        "- Resolve all reported issues and deficiencies noted above.",
        "- Ensure full compliance with all acceptance criteria.",
        "- Do not make out-of-scope edits outside the designated task.",
        "- Ensure all tests and builds succeed cleanly before finishing.",
    ])

    return "\n".join(lines)
