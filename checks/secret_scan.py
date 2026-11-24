"""Security check: scan a directory for leaked credentials, tokens, and secrets.

Runs during verification against product source or build output.
Blocks publication if a provider credential, private key, or factory secret
appears in the committed codebase.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Anchored regex patterns for known credential shapes
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "opencode_key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "openai_key": re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "bearer_token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{40,}"),
}

# Environment variable secret names to match verbatim if present in env
ENV_SECRET_NAMES = (
    "OPENCODE_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "FACTORY_OPERATOR_TOKEN",
    "FACTORY_RELEASE_TOKEN",
)

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".mp3", ".mp4",
}
SKIP_DIRS = {
    ".git", ".venv", "node_modules", ".nuxt", ".output", "dist", "build",
    "__pycache__", ".pytest_cache", ".ruff_cache",
}
MAX_FILE_BYTES = 5_000_000


class SecretFinding(NamedTuple):
    file_path: str
    rule: str
    detail: str


def scan_directory(
    root: Path | str,
    extra_env_secrets: dict[str, str] | None = None,
) -> list[SecretFinding]:
    """Scan all files under root for leaked credentials and secrets.

    Args:
        root: Root directory to scan.
        extra_env_secrets: Optional mapping of secret_name -> secret_value to detect.

    Returns:
        List of SecretFinding tuples. Empty list if clean.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        return []

    # Collect known literal secrets to match against
    literal_secrets: dict[str, str] = {}
    for name in ENV_SECRET_NAMES:
        val = os.environ.get(name)
        if val and len(val.strip()) >= 16:
            literal_secrets[name] = val.strip()

    if extra_env_secrets:
        for k, v in extra_env_secrets.items():
            if v and len(v.strip()) >= 8:
                literal_secrets[k] = v.strip()

    findings: list[SecretFinding] = []

    for path in sorted(root_path.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue

        rel_parts = path.relative_to(root_path).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue

        if path.suffix.lower() in SKIP_SUFFIXES:
            continue

        try:
            stat = path.stat()
            if stat.st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue

        rel_name = path.relative_to(root_path).as_posix()

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.debug("Could not read %s for secret scan: %s", path, exc)
            continue

        # Check literal env secrets
        for sec_name, sec_val in literal_secrets.items():
            if sec_val in content:
                findings.append(
                    SecretFinding(
                        file_path=rel_name,
                        rule="literal_env_secret",
                        detail=f"Verbatim value of {sec_name} found in file",
                    )
                )

        # Check regex patterns
        for rule_name, pattern in SECRET_PATTERNS.items():
            match = pattern.search(content)
            if match:
                masked = match.group(0)[:8] + "..."
                findings.append(
                    SecretFinding(
                        file_path=rel_name,
                        rule=rule_name,
                        detail=f"Pattern match for {rule_name} ({masked})",
                    )
                )

    return findings


def main() -> int:
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 secret_scan.py <directory_to_scan>", file=sys.stderr)
        return 1

    target = Path(sys.argv[1]).resolve()
    findings = scan_directory(target)
    if findings:
        print(f"FAILED: Found {len(findings)} potential secret(s) in {target}:", file=sys.stderr)
        for f in findings:
            print(f"  - {f.file_path}: [{f.rule}] {f.detail}", file=sys.stderr)
        return 1

    print(f"OK: No secrets detected in {target}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
