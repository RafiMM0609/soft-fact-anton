"""Assemble published release artifacts: clean tarballs, README, and cryptographic digests."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import tarfile
from typing import Any

from .models import Plan

logger = logging.getLogger(__name__)

# Build caches and temporary dirs to exclude from distribution artifacts
EXCLUDED_DIRS = {
    ".git", ".venv", "node_modules", "dist", "build", "release",
    ".nuxt", ".output", ".data", "__pycache__", ".pytest_cache", ".ruff_cache",
}

EXCLUDED_FILES = {
    ".env", ".env.local", ".env.production", ".DS_Store", "Thumbs.db",
}


def filter_tar_info(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Filter out excluded files and normalize ownership for reproducible tarballs."""
    parts = Path(tarinfo.name).parts
    if any(p in EXCLUDED_DIRS for p in parts):
        return None
    if Path(tarinfo.name).name in EXCLUDED_FILES:
        return None

    # Reset user/group for reproducible builds
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = "root"
    tarinfo.gname = "root"
    return tarinfo


def calculate_sha256(file_path: Path) -> str:
    """Calculate the sha256 hex digest of a file."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def render_readme(
    project_name: str,
    brief: str,
    plan: Plan | None = None,
    profile: str = "static",
) -> str:
    """Generate a clean markdown README for the released artifact hand-off."""
    task_lines = ""
    if plan and plan.tasks:
        task_lines = "## Completed Tasks\n\n" + "\n".join(
            f"- **{t.id}**: {t.title}" for t in plan.tasks
        ) + "\n\n"

    summary_text = plan.summary if plan and plan.summary else brief

    return f"""# {project_name}

{summary_text}

---

## Overview & Architecture
- **Profile**: `{profile}`
- **Build Engine**: ACP Software Factory
- **Distribution**: Self-contained clean hand-off codebase

{task_lines}## How to Run

### Development & Preview
To run or preview this project locally:

```bash
# Using Python built-in static server
python3 -m http.server 8080

# Or using the factory CLI
factory run {project_name} --port 8080
```
"""


def assemble_artifact(
    source_root: Path | str,
    output_tarball: Path | str,
    project: dict[str, Any] | Any,
    plan: Plan | None = None,
    profile: str = "static",
) -> dict[str, Any]:
    """Package the project repository into a clean distribution tar.gz archive.

    Args:
        source_root: Path to the clean project root directory.
        output_tarball: Target path for the generated .tar.gz archive.
        project: Project record dict or model.
        plan: Approved Plan model (optional).
        profile: Profile type string.

    Returns:
        Dict with artifact_path, digest, size_bytes, and file_count.
    """
    root = Path(source_root).resolve()
    tar_path = Path(output_tarball).resolve()
    tar_path.parent.mkdir(parents=True, exist_ok=True)

    project_id = project["id"] if isinstance(project, dict) else getattr(project, "id", "product")
    project_name = project.get("name", project_id) if isinstance(project, dict) else getattr(project, "name", project_id)
    brief = project.get("brief", "") if isinstance(project, dict) else getattr(project, "brief", "")

    # Auto-generate README.md if not already present in the source root
    readme_path = root / "README.md"
    if not readme_path.is_file():
        readme_content = render_readme(project_name, brief, plan=plan, profile=profile)
        readme_path.write_text(readme_content, encoding="utf-8")

    file_count = 0
    with tarfile.open(tar_path, "w:gz") as tar:
        for item in sorted(root.iterdir()):
            if item.name in EXCLUDED_DIRS or item.name in EXCLUDED_FILES:
                continue
            tar.add(item, arcname=item.name, filter=filter_tar_info)
            file_count += 1

    digest = f"sha256:{calculate_sha256(tar_path)}"
    size = tar_path.stat().st_size

    logger.info("Assembled release artifact %s (size: %d bytes, %s)", tar_path.name, size, digest)

    return {
        "artifact_path": str(tar_path),
        "digest": digest,
        "size_bytes": size,
        "file_count": file_count,
    }
