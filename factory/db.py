import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any


def uid() -> str:
    return uuid.uuid4().hex


class Conflict(ValueError):
    pass


LANE_KINDS = {
    "build": ("build", "plan", "verify"),
    "deploy": ("deploy",),
    "serve": ("serve",),
}


def lane_predicate(lane: str) -> str:
    kinds = LANE_KINDS.get(lane)
    if kinds is None:
        raise ValueError(f"Unknown lane: {lane}")
    return "kind IN (" + ",".join("'" + kind + "'" for kind in kinds) + ")"


class Store:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);

                CREATE TABLE IF NOT EXISTS projects(
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  brief TEXT NOT NULL,
                  profile TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'draft',
                  revision INTEGER NOT NULL DEFAULT 0,
                  plan TEXT,
                  artifact TEXT,
                  created REAL NOT NULL,
                  client_id TEXT NOT NULL DEFAULT '',
                  pending_edit TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs(
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL REFERENCES projects(id),
                  kind TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'queued',
                  payload TEXT NOT NULL DEFAULT '{}',
                  result TEXT,
                  error TEXT,
                  created REAL NOT NULL,
                  started REAL,
                  finished REAL,
                  heartbeat REAL,
                  cancel INTEGER NOT NULL DEFAULT 0,
                  reserved_usd REAL NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_project ON jobs(project_id)
                  WHERE state IN ('queued','running');

                CREATE TABLE IF NOT EXISTS tasks(
                  id TEXT NOT NULL,
                  job_id TEXT NOT NULL,
                  project_id TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'queued',
                  title TEXT NOT NULL DEFAULT '',
                  instructions TEXT NOT NULL DEFAULT '',
                  acceptance TEXT NOT NULL DEFAULT '[]',
                  depends_on TEXT NOT NULL DEFAULT '[]',
                  detail TEXT NOT NULL DEFAULT '{}',
                  created REAL NOT NULL,
                  started REAL,
                  finished REAL,
                  PRIMARY KEY (job_id, id)
                );
                CREATE INDEX IF NOT EXISTS tasks_by_job ON tasks(job_id, id);
                CREATE INDEX IF NOT EXISTS tasks_by_project ON tasks(project_id);

                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id TEXT,
                  job_id TEXT,
                  kind TEXT NOT NULL,
                  detail TEXT NOT NULL,
                  created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_project ON events(project_id, seq);
                CREATE INDEX IF NOT EXISTS jobs_by_project ON jobs(project_id, created);

                CREATE TABLE IF NOT EXISTS workers(
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL REFERENCES projects(id),
                  job_id TEXT NOT NULL,
                  task_id TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'queued',
                  branch TEXT NOT NULL,
                  worktree TEXT,
                  base_commit TEXT,
                  commit_sha TEXT,
                  depends_on TEXT NOT NULL DEFAULT '[]',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  detail TEXT NOT NULL DEFAULT '{}',
                  created REAL NOT NULL,
                  started REAL,
                  finished REAL,
                  revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS workers_by_job ON workers(job_id, task_id);
                CREATE INDEX IF NOT EXISTS workers_by_project ON workers(project_id, created);

                CREATE TABLE IF NOT EXISTS runtime(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def tx(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, args)]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any]:
        rows = self.all(sql, args)
        if not rows:
            raise KeyError("Record not found")
        return rows[0]

    @staticmethod
    def event_tx(
        db: sqlite3.Connection,
        project_id: str | None,
        job_id: str | None,
        kind: str,
        detail: dict[str, Any] | str,
    ) -> None:
        detail_json = json.dumps(detail) if isinstance(detail, dict) else (detail or "{}")
        db.execute(
            "INSERT INTO events(project_id, job_id, kind, detail, created) VALUES(?,?,?,?,?)",
            (project_id, job_id, kind, detail_json, time.time()),
        )

    def event(
        self,
        project_id: str | None,
        job_id: str | None,
        kind: str,
        detail: dict[str, Any] | str,
    ) -> None:
        with self.tx() as db:
            self.event_tx(db, project_id, job_id, kind, detail)

    def record_acp_error(
        self, project_id: str, job_id: str, stage: str, agent: Any, error: Exception, **context: Any
    ) -> None:
        self.event(project_id, job_id, "acp.error", {
            "stage": stage,
            "harness": getattr(agent, "agent_name", None),
            "model_id": getattr(agent, "model", None),
            "error": str(error)[-10000:],
            **context,
        })

    def create_project(self, payload: Any) -> dict[str, Any]:
        project = uid()
        name = getattr(payload, "name", "") or payload.get("name", "")
        brief = getattr(payload, "brief", "") or payload.get("brief", "")
        profile = getattr(payload, "profile", "static") or payload.get("profile", "static")
        client_id = getattr(payload, "client_id", "") or payload.get("client_id", "")

        with self.tx() as db:
            db.execute(
                "INSERT INTO projects(id, name, brief, profile, created, client_id) "
                "VALUES(?,?,?,?,?,?)",
                (project, name, brief, profile, time.time(), client_id),
            )
            self.event_tx(
                db,
                project,
                None,
                "project.created",
                {"name": name, "client_id": client_id},
            )
        return self.project(project)

    def project(self, project_id: str) -> dict[str, Any]:
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def client_projects(self, client_id: str) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM projects WHERE client_id=? ORDER BY created", (client_id,))

    def enqueue(
        self,
        project_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        allowed: list[str] | set[str] | None = None,
        next_state: str | None = None,
    ) -> dict[str, Any]:
        job_id = uid()
        payload = payload or {}
        try:
            with self.tx() as db:
                row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                if row is None:
                    raise KeyError("Project not found")
                if allowed and row["state"] not in allowed:
                    raise Conflict(f"Project is {row['state']}")

                db.execute(
                    "INSERT INTO jobs(id, project_id, kind, payload, created) VALUES(?,?,?,?,?)",
                    (job_id, project_id, kind, json.dumps(payload), time.time()),
                )
                if next_state:
                    db.execute("UPDATE projects SET state=? WHERE id=?", (next_state, project_id))
                self.event_tx(db, project_id, job_id, f"{kind}.queued", payload)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Project already has an active job") from exc
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def claim(self, lane: str = "build") -> dict[str, Any] | None:
        with self.tx() as db:
            predicate = lane_predicate(lane)
            row = db.execute(
                "SELECT * FROM jobs WHERE state='queued' AND " + predicate + " ORDER BY created, id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = time.time()
            db.execute(
                "UPDATE jobs SET state='running', started=?, heartbeat=? WHERE id=?",
                (now, now, row["id"]),
            )
            self.event_tx(db, row["project_id"], row["id"], "job.started", {})
            claimed = dict(row)
            claimed.update({"state": "running", "started": now, "heartbeat": now})
            return claimed

    def finish(
        self,
        job_id: str,
        state: str,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Job not found")
            now = time.time()
            db.execute(
                "UPDATE jobs SET state=?, result=?, error=?, finished=? WHERE id=?",
                (state, json.dumps(result) if result is not None else None, error, now, job_id),
            )
            if row["kind"] != "deploy" and state != "succeeded":
                db.execute("UPDATE projects SET state=? WHERE id=?", (state, row["project_id"]))
            self.event_tx(db, row["project_id"], job_id, f"job.{state}", {"error": error})

    def cancel(self, job_id: str) -> None:
        with self.tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Job not found")
            if row["state"] not in ("queued", "running"):
                raise Conflict("Job is already terminal")
            now = time.time()
            if row["state"] == "queued":
                db.execute(
                    "UPDATE jobs SET state='cancelled', cancel=1, finished=? WHERE id=?",
                    (now, job_id),
                )
                if row["kind"] != "deploy":
                    db.execute("UPDATE projects SET state='cancelled' WHERE id=?", (row["project_id"],))
            else:
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
            self.event_tx(db, row["project_id"], job_id, "job.cancel_requested", {})

    def heartbeat(self, job_id: str | None = None, lane: str = "build") -> None:
        with self.tx() as db:
            now = time.time()
            db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)", (f"{lane}_heartbeat", str(now)))
            if job_id:
                db.execute("UPDATE jobs SET heartbeat=? WHERE id=? AND state='running'", (now, job_id))

    # --- tasks -----------------------------------------------------------------------

    def set_task_state(
        self,
        job_id: str,
        task_id: str,
        state: str,
        detail: dict[str, Any] | str | None = None,
        **kwargs: Any,
    ) -> None:
        now = time.time()
        detail_str = json.dumps(detail) if isinstance(detail, dict) else (detail or "{}")
        with self.tx() as db:
            project_id = ""
            job_row = db.execute("SELECT project_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job_row:
                project_id = job_row["project_id"]
            elif "project_id" in kwargs:
                project_id = kwargs["project_id"]

            task_row = db.execute(
                "SELECT * FROM tasks WHERE job_id=? AND id=?", (job_id, task_id)
            ).fetchone()
            if task_row:
                db.execute(
                    """
                    UPDATE tasks
                    SET state=?,
                        detail=CASE WHEN ? != '{}' THEN ? ELSE detail END,
                        started=CASE WHEN ? = 'running' AND started IS NULL THEN ? ELSE started END,
                        finished=CASE WHEN ? IN ('completed', 'merged', 'failed', 'cancelled')
                                      THEN ? ELSE finished END
                    WHERE job_id=? AND id=?
                    """,
                    (state, detail_str, detail_str, state, now, state, now, job_id, task_id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO tasks(id, job_id, project_id, state, detail, created, started, finished)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        job_id,
                        project_id,
                        state,
                        detail_str,
                        now,
                        now if state == "running" else None,
                        now if state in ("completed", "merged", "failed", "cancelled") else None,
                    ),
                )

            # Mirror to workers table if corresponding record exists
            db.execute(
                """
                UPDATE workers
                SET state=?,
                    detail=CASE WHEN ? != '{}' THEN ? ELSE detail END,
                    started=CASE WHEN ? = 'running' AND started IS NULL THEN ? ELSE started END,
                    finished=CASE WHEN ? IN ('completed', 'merged', 'failed', 'cancelled')
                                  THEN ? ELSE finished END
                WHERE job_id=? AND task_id=?
                """,
                (state, detail_str, detail_str, state, now, state, now, job_id, task_id),
            )

    def task(self, job_id: str, task_id: str) -> dict[str, Any]:
        return self.one("SELECT * FROM tasks WHERE job_id=? AND id=?", (job_id, task_id))

    def tasks_for_job(self, job_id: str) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM tasks WHERE job_id=? ORDER BY created", (job_id,))

    # --- workers ---------------------------------------------------------------------

    def create_worker(
        self,
        project_id: str,
        job_id: str,
        task_id: str,
        branch: str,
        base_commit: str,
        depends_on: list[str] | tuple = (),
        worktree: Path | str | None = None,
        revision: int = 0,
    ) -> dict[str, Any]:
        worker_id = uid()
        now = time.time()
        with self.tx() as db:
            db.execute(
                "INSERT INTO workers(id, project_id, job_id, task_id, branch, worktree, base_commit,"
                "depends_on, created, revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    worker_id,
                    project_id,
                    job_id,
                    task_id,
                    branch,
                    str(worktree) if worktree else None,
                    base_commit,
                    json.dumps(list(depends_on)),
                    now,
                    revision,
                ),
            )
            # Also ensure a task entry exists
            db.execute(
                "INSERT OR IGNORE INTO tasks(id, job_id, project_id, state, depends_on, created) "
                "VALUES(?,?,?,?,?,?)",
                (task_id, job_id, project_id, "queued", json.dumps(list(depends_on)), now),
            )
            self.event_tx(
                db,
                project_id,
                job_id,
                "worker.created",
                {"worker": worker_id, "task": task_id, "branch": branch},
            )
        return self.one("SELECT * FROM workers WHERE id=?", (worker_id,))

    def worker(self, worker_id: str) -> dict[str, Any]:
        return self.one("SELECT * FROM workers WHERE id=?", (worker_id,))

    def workers_for(self, job_id: str) -> list[dict[str, Any]]:
        return self.all("SELECT * FROM workers WHERE job_id=? ORDER BY created", (job_id,))

    def set_worker(self, worker_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "worktree",
            "commit_sha",
            "attempts",
            "detail",
            "started",
            "finished",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown worker fields: {', '.join(sorted(unknown))}")
        payload = dict(fields)
        if "detail" in payload and not isinstance(payload["detail"], str):
            payload["detail"] = json.dumps(payload["detail"])
        for name in ("worktree", "commit_sha", "state"):
            if name in payload and payload[name] is not None:
                payload[name] = str(payload[name])
        assignments = ", ".join(f"{name}=?" for name in payload)
        with self.tx() as db:
            db.execute(
                f"UPDATE workers SET {assignments} WHERE id=?",
                (*payload.values(), worker_id),
            )
            # Mirror state back to tasks table if state or detail changed
            w_row = db.execute("SELECT job_id, task_id FROM workers WHERE id=?", (worker_id,)).fetchone()
            if w_row and "state" in payload:
                db.execute(
                    "UPDATE tasks SET state=? WHERE job_id=? AND id=?",
                    (payload["state"], w_row["job_id"], w_row["task_id"]),
                )

    def worker_states(self, job_id: str) -> dict[str, str]:
        rows = self.all("SELECT task_id, state FROM workers WHERE job_id=?", (job_id,))
        return {row["task_id"]: row["state"] for row in rows}

    def completed_tasks(self, job_id: str) -> set[str]:
        rows = self.all("SELECT task_id FROM workers WHERE job_id=? AND state='merged'", (job_id,))
        return {row["task_id"] for row in rows}

    def tasks_merged_for_project(
        self,
        project_id: str,
        revision: int | None = None,
        present_commits: set[str] | list[str] = (),
    ) -> set[str]:
        """Tasks whose merged commit is still reachable on the product branch."""
        if revision is not None:
            rows = self.all(
                "SELECT task_id, commit_sha FROM workers "
                "WHERE project_id=? AND revision=? AND state='merged' AND commit_sha IS NOT NULL "
                "ORDER BY finished",
                (project_id, revision),
            )
        else:
            rows = self.all(
                "SELECT task_id, commit_sha FROM workers "
                "WHERE project_id=? AND state='merged' AND commit_sha IS NOT NULL "
                "ORDER BY finished",
                (project_id,),
            )
        reachable = set(present_commits)
        done = set()
        for row in rows:
            if row["commit_sha"] in reachable:
                done.add(row["task_id"])
        return done
