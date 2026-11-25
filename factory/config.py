"""Runtime configuration settings for ACP Software Factory."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets


def _default_token(data_dir: Path) -> str:
    env_token = os.getenv("FACTORY_OPERATOR_TOKEN")
    if env_token:
        return env_token
    token_file = data_dir / ".operator_token"
    if token_file.is_file():
        try:
            stored = token_file.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except OSError:
            pass
    return secrets.token_hex(24)


@dataclass
class Settings:
    """Factory global configuration settings.

    All options can be configured explicitly or read from environment variables.
    Defaults are tuned to be safe for VPS environments with low RAM (2 GB).
    """

    data: Path = field(
        default_factory=lambda: Path(os.getenv("FACTORY_DATA", "./data")).resolve()
    )
    agy: str = field(
        default_factory=lambda: os.getenv("FACTORY_AGY", "/home/ubuntu/.local/bin/agy")
    )
    harness: str = field(
        default_factory=lambda: os.getenv("FACTORY_HARNESS", "agy")
    )
    worker_concurrency: int = field(
        default_factory=lambda: int(os.getenv("FACTORY_WORKER_CONCURRENCY", "1"))
    )
    max_repairs: int = field(
        default_factory=lambda: int(os.getenv("FACTORY_MAX_REPAIRS", "5"))
    )
    agent_timeout: int = field(
        default_factory=lambda: int(os.getenv("FACTORY_AGENT_TIMEOUT", "600"))
    )
    worker_timeout: int = field(
        default_factory=lambda: int(os.getenv("FACTORY_WORKER_TIMEOUT", "1800"))
    )
    review_timeout: int = field(
        default_factory=lambda: int(os.getenv("FACTORY_REVIEW_TIMEOUT", "600"))
    )
    operator_token: str | None = None

    def __post_init__(self) -> None:
        self.data = Path(self.data)
        if self.operator_token is None:
            self.operator_token = _default_token(self.data)

    @property
    def data_dir(self) -> Path:
        """Alias for compatibility with ParallelBuilder and product repositories."""
        return self.data

    @property
    def db_path(self) -> Path:
        return self.data / "factory.db"

    @property
    def products_dir(self) -> Path:
        return self.data / "products"

    @property
    def skills_dir(self) -> Path:
        env_skills = os.getenv("FACTORY_SKILLS_DIR")
        if env_skills:
            return Path(env_skills).resolve()
        return (Path(__file__).parent.parent / "skills").resolve()

    @property
    def starters_dir(self) -> Path:
        return (Path(__file__).parent / "starters").resolve()
