# ACP Software Factory (`acp-factory`)

An autonomous software factory powered by the **Agent Client Protocol (ACP)** and local authenticated CLI harnesses (**`agy`**, `codex`, `pi`, `claude`), completely free from direct API keys.

Adopts the battle-tested architecture of `caca` (*Anton Software Factory*):
- **DAG Task Planner**: Structured planning decomposing briefs into directed acyclic graphs of tasks with explicit dependencies and acceptance criteria.
- **Parallel Git Worktrees**: Isolated worktree per worker (`products.py`), preventing working tree collisions and enabling true concurrency.
- **Wave Barrier & Automated Diff Review**: Waves synchronize at barriers; code diffs are evaluated by `ReviewGuard` before sequential merging into the product repository.
- **Repair Feedback Loop**: Automatically retries rejected tasks or merge conflicts with structured findings.
- **Independent Verifier & Tester**: Executes build commands and acceptance flows (API & Playwright browser tests) before publishing artifacts.

---

## Architecture Overview

```
Operator Brief → AcpAgent Planner → Plan DAG → Parallel Git Worktrees (AcpWorker with agy/acp)
               → Diff Review (ReviewGuard) → Merge Barrier → Verifier & Tester → Artifact
```

---

## Quickstart

### 1. Requirements
- Python 3.12+ and `uv`
- Git 2.40+
- Authenticated local harness: `agy` at `/home/ubuntu/.local/bin/agy` (or `codex`, `pi`, `claude`)

### 2. Diagnostics
```bash
uv run factory doctor
```

### 3. Initialize Factory Data
```bash
uv run factory init --data ./data
```

### 4. Run an Autonomous Build
```bash
uv run factory build \
  --brief "Build a responsive static landing page with an interactive pricing calculator" \
  --product "my-app" \
  --profile "static"
```

### 5. Running Tests
```bash
uv run pytest -v
uv run ruff check acp factory tests
```

---

## Modules

- `acp/protocol.py`: Pydantic models for JSON-RPC 2.0 and ACP specification.
- `acp/client.py`: Asynchronous stdio JSON-RPC 2.0 client managing subprocess agents.
- `acp/harness.py`: Local harness resolution (`agy`, `codex`, `pi`, `claude`), command builders, and safe environment isolation.
- `acp/agent.py`: `AcpAgent` for structured, schema-validated single-turn calls (Planner, Reviewer).
- `acp/worker.py`: `AcpWorker` running code implementations confined to Git worktrees.
- `factory/models.py`: DAG `Task`, `Plan`, `AcceptanceFlow`, and `Review` models.
- `factory/products.py`: Git repository initialization, worktree lifecycle, commit, diff, and merge operations.
- `factory/parallel.py`: Wave scheduling, parallel frontier execution, wave barrier, and sequential merging.
- `factory/reviewguard.py`: Objective diff-only review evaluation.
- `factory/repair.py`: Deduplicated repair feedback synthesis for retries.
- `factory/verifier.py`: Build and syntax verification runner.
- `factory/testing.py`: HTTP API & Playwright acceptance flow tester.
- `factory/db.py`: SQLite ledger for durable job and task tracking.
- `factory/engine.py`: End-to-end orchestration pipeline.
- `factory/cli.py`: Command-line interface.
