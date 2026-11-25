import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Upper bound for one task's instructions.
INSTRUCTIONS_MAX_LENGTH = 24000


class Task(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    title: str = Field(min_length=3, max_length=200)
    instructions: str = Field(min_length=10, max_length=INSTRUCTIONS_MAX_LENGTH)
    acceptance: list[str] = Field(min_length=1, max_length=30)
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    # Vendored design skills this worker should load, by directory name under `skills/`.
    skills: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("skills")
    @classmethod
    def valid_skill_names(cls, value: list[str]) -> list[str]:
        for name in value:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
                raise ValueError(f"Invalid skill name: {name!r} (use lowercase kebab-case)")
        if len(set(value)) != len(value):
            raise ValueError("Skill names must be unique within a task")
        return value


# Upper bound for one acceptance flow's goal sentence.
GOAL_MAX_LENGTH = 8000


class AcceptanceFlow(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    kind: Literal["api", "browser"]
    goal: str = Field(min_length=10, max_length=GOAL_MAX_LENGTH)
    steps: list[str] = Field(min_length=1, max_length=20)
    selectors: dict[str, str] = Field(default_factory=dict, max_length=60)

    @model_validator(mode="before")
    @classmethod
    def normalize_selector_names(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("selectors"), dict):
            data = {
                **data,
                "selectors": {
                    re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower(): value
                    for name, value in data["selectors"].items()
                },
            }
        return data

    @model_validator(mode="after")
    def validate_selectors(self) -> "AcceptanceFlow":
        for name, selector in self.selectors.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
                raise ValueError(f"Invalid selector name: {name!r} (use lowercase snake_case)")
            if not selector or len(selector) > 300:
                raise ValueError(f"Invalid selector for {name}")
            if any(ord(c) < 32 for c in selector) or "url(" in selector.lower():
                raise ValueError(f"Invalid selector for {name}")
        if self.kind == "browser" and not self.selectors:
            raise ValueError("Browser flows must declare the selectors they rely on")
        return self


class Plan(StrictModel):
    summary: str = Field(min_length=10, max_length=12000)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    questions: list[str] = Field(default_factory=list, max_length=30)
    tasks: list[Task] = Field(min_length=1, max_length=30)
    pages: list[str] = Field(default_factory=list, max_length=20)
    test_flows: list[AcceptanceFlow] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_graph(self) -> "Plan":
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"Task {task.id} cannot depend on itself")
            unknown = [dep for dep in task.depends_on if dep not in ids]
            if unknown:
                raise ValueError(f"Task {task.id} depends on unknown tasks: {', '.join(unknown)}")
        if len({flow.id for flow in self.test_flows}) != len(self.test_flows):
            raise ValueError("Test flow IDs must be unique")
        self.execution_order()
        return self

    def _dependencies(self) -> dict[str, list[str]]:
        return {task.id: list(task.depends_on) for task in self.tasks}

    def execution_order(self) -> list[str]:
        """Topological order. Raises on a cycle instead of silently dropping tasks."""
        deps = self._dependencies()
        ordered: list[str] = []
        while deps:
            ready = sorted(name for name, needs in deps.items() if not any(d in deps for d in needs))
            if not ready:
                raise ValueError("Task dependencies contain a cycle")
            for name in ready:
                ordered.append(name)
                deps.pop(name)
        return ordered

    def ready_set(self, completed: Any) -> list[Task]:
        """Tasks whose dependencies are all satisfied and that are not yet completed."""
        done = set(completed)
        return [
            task
            for task in self.tasks
            if task.id not in done and all(dep in done for dep in task.depends_on)
        ]


class DomainCandidate(StrictModel):
    """A high-level domain/module candidate affected by an adjustment request."""

    domain_path: str = Field(min_length=2, max_length=160)
    reason: str = Field(default="", max_length=400)
    target_files: list[str] = Field(default_factory=list, max_length=15)


class AdjustScope(StrictModel):
    """Lightweight domain-hierarchy preview before planning an incremental adjustment."""

    summary: str = Field(default="", max_length=600)
    candidates: list[DomainCandidate] = Field(min_length=1, max_length=6)

    def ordered_tasks(self) -> list[Task]:
        by_id = {task.id: task for task in self.tasks}
        return [by_id[name] for name in self.execution_order()]

    def waves(self) -> list[list[str]]:
        """The dependency waves the builder will run, in order."""
        done: set[str] = set()
        waves: list[list[str]] = []
        while True:
            ready = [task.id for task in self.ready_set(done)]
            if not ready:
                break
            waves.append(sorted(ready))
            done.update(ready)
        return waves


class DeltaPlan(StrictModel):
    summary: str = Field(min_length=10, max_length=12000)
    assumptions: list[str] = Field(default_factory=list, max_length=50)
    questions: list[str] = Field(default_factory=list, max_length=30)
    tasks: list[Task] = Field(min_length=1, max_length=15)
    pages: list[str] = Field(default_factory=list, max_length=20)
    test_flows: list[AcceptanceFlow] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_ids(self) -> "DeltaPlan":
        ids = {task.id for task in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique within delta plan")
        if len({flow.id for flow in self.test_flows}) != len(self.test_flows):
            raise ValueError("Test flow IDs must be unique within delta plan")
        for task in self.tasks:
            if task.id in task.depends_on:
                raise ValueError(f"Task {task.id} cannot depend on itself")
        return self


ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def _is_real_env_file(part: str) -> bool:
    if part != ".env" and not part.startswith(".env."):
        return False
    return not part.endswith(ENV_TEMPLATE_SUFFIXES)


BLOCKED_PATH_PARTS = ("..", ".git", ".pi", ".memorix", ".ssh")


def safe_path(name: str, allow_git: bool = False) -> str:
    path = PurePosixPath(name)
    blocked = (
        BLOCKED_PATH_PARTS
        if not allow_git
        else tuple(part for part in BLOCKED_PATH_PARTS if part != ".git")
    )
    if (
        not name
        or name == "."
        or len(name) > 240
        or path.is_absolute()
        or "\\" in name
        or any(ord(c) < 32 for c in name)
        or str(path) != name
        or any(p in blocked for p in path.parts)
        or any(_is_real_env_file(p) for p in path.parts)
    ):
        raise ValueError(f"Disallowed file path: {name!r}")
    return name


class CompileClaim(StrictModel):
    finding_index: int = Field(ge=0, lt=100)
    scope: Literal["configured_build", "test_root"]
    file: str = ""

    @model_validator(mode="after")
    def valid_root(self) -> "CompileClaim":
        if self.scope == "test_root":
            safe_path(self.file)
            if not self.file.endswith(".zig"):
                raise ValueError("Test claims must name a Zig source root")
        return self


class Review(StrictModel):
    decision: Literal["approved", "rejected"] = "approved"
    findings: list[str] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    compile_claims: list[CompileClaim] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def handle_legacy_approved(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "decision" not in data and "approved" in data:
                data["decision"] = "approved" if data.pop("approved") else "rejected"
            elif "approved" in data:
                data.pop("approved")
        return data

    @property
    def approved(self) -> bool:
        return self.decision == "approved"

    @model_validator(mode="after")
    def valid_claims(self) -> "Review":
        indexes = [claim.finding_index for claim in self.compile_claims]
        if any(index >= len(self.findings) for index in indexes):
            raise ValueError("Compile claim refers to a missing finding")
        if len(indexes) != len(set(indexes)):
            raise ValueError("Only one compile scope per finding is allowed")
        return self


class FeatureInput(StrictModel):
    brief: str = Field(min_length=10, max_length=10000)
    mode: Literal["add", "adjust"] = "add"


class ProjectInput(StrictModel):
    name: str = Field(min_length=3, max_length=100)
    brief: str = Field(min_length=20, max_length=20000)
    profile: str = Field(default="static", pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    client_id: str = Field(default="", max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$|^$")
