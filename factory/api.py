"""FastAPI application for ACP Software Factory."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from factory.config import Settings
from factory.db import Store
from factory.engine import Engine

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


class BuildRequest(BaseModel):
    brief: str = Field(..., description="Product specification brief")
    product: str | None = Field(None, description="Product identifier / name")
    profile: str = Field("static", description="Product profile type (static, go-htmx, etc.)")


class AdjustPreviewRequest(BaseModel):
    instruction: str = Field(..., min_length=3, description="Requested adjustment instruction")


class AdjustRequest(BaseModel):
    instruction: str = Field(..., min_length=3, description="Requested adjustment instruction")
    selected_domains: list[str] = Field(default_factory=list, description="Confirmed domain paths")
    target_files: list[str] = Field(default_factory=list, description="Target files from preview")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()

    # Ensure directories exist
    settings.data.mkdir(parents=True, exist_ok=True)
    settings.products_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path)
    engine = Engine(settings=settings, store=store)

    app = FastAPI(
        title="ACP Software Factory API",
        version="0.1.0",
        description="REST API for autonomous software factory operations",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # In-memory tracking of active background tasks: job_id -> asyncio.Task
    active_tasks: dict[str, asyncio.Task[Any]] = {}

    def require_operator_auth(
        credentials: HTTPAuthorizationCredentials | None = Security(security),
    ) -> str:
        """Validate operator bearer token."""
        if not settings.operator_token:
            return ""
        if not credentials or credentials.credentials != settings.operator_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing operator authorization token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials.credentials

    @app.get("/")
    def dashboard() -> FileResponse:
        """Serve the built-in operator web dashboard."""
        index_path = Path(__file__).resolve().parent / "static" / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=404, detail="Dashboard UI not found")
        return FileResponse(path=str(index_path), media_type="text/html")

    @app.get("/health")
    def health_check() -> dict[str, Any]:
        """Health check endpoint (no auth required for monitoring)."""
        return {"status": "ok", "time": time.time(), "version": "0.1.0"}

    @app.get("/api/doctor")
    def doctor_check(_auth: str = Depends(require_operator_auth)) -> dict[str, Any]:
        """System diagnostic endpoint."""
        import shutil
        import sys

        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        harness_path = Path(settings.agy)
        harness_ok = (harness_path.is_file() and os.access(harness_path, os.X_OK)) or bool(
            shutil.which(settings.harness)
        )

        mem_total_mb: int | None = None
        mem_used_mb: int | None = None
        try:
            meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
            vals: dict[str, int] = {}
            for line in meminfo.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    vals[parts[0].rstrip(":")] = int(parts[1])
            if "MemTotal" in vals and "MemAvailable" in vals:
                mem_total_mb = vals["MemTotal"] // 1024
                mem_used_mb = (vals["MemTotal"] - vals["MemAvailable"]) // 1024
        except OSError:
            pass

        return {
            "status": "ready" if harness_ok else "warning",
            "python": py_ver,
            "git": bool(shutil.which("git")),
            "harness": settings.harness,
            "harness_ready": harness_ok,
            "data_dir": str(settings.data),
            "memory_used_mb": mem_used_mb,
            "memory_total_mb": mem_total_mb,
        }

    async def _run_build_in_background(project_id: str, job_id: str) -> None:
        """Execute build in background thread without blocking event loop."""
        try:
            proj = store.project(project_id)
            job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))

            def is_cancelled() -> bool:
                row = store.one("SELECT cancel FROM jobs WHERE id=?", (job_id,))
                return bool(row.get("cancel", 0) == 1)

            # Mark job running and started
            now = time.time()
            with store.tx() as db:
                db.execute(
                    "UPDATE jobs SET state='running', started=?, heartbeat=? WHERE id=?",
                    (now, now, job_id),
                )
                db.execute("UPDATE projects SET state='building' WHERE id=?", (project_id,))
                store.event_tx(db, project_id, job_id, "job.started", {"lane": "build"})

            logger.info("Starting background build for project %s (job %s)", project_id, job_id)
            await asyncio.to_thread(
                engine.build,
                project=proj,
                job=job,
                cancelled=is_cancelled,
            )
            logger.info("Background build completed successfully for job %s", job_id)
        except Exception as exc:
            logger.exception("Background build failed for job %s: %s", job_id, exc)
        finally:
            active_tasks.pop(job_id, None)

    @app.post("/api/build", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_build(
        req: BuildRequest,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Trigger an autonomous product build in the background."""
        product_id = req.product or f"product-{secrets.token_hex(4)}"

        # Get or create project
        try:
            store.project(product_id)
        except KeyError:
            with store.tx() as db:
                db.execute(
                    "INSERT INTO projects(id, name, brief, profile, created) VALUES(?,?,?,?,?)",
                    (product_id, product_id, req.brief, req.profile, time.time()),
                )
                store.event_tx(db, product_id, None, "project.created", {"name": product_id})

        # Enqueue job
        job = store.enqueue(
            product_id,
            kind="build",
            payload={"brief": req.brief, "profile": req.profile},
        )
        job_id = job["id"]

        # Launch background execution task
        task = asyncio.create_task(_run_build_in_background(product_id, job_id))
        active_tasks[job_id] = task

        return {
            "status": "queued",
            "job_id": job_id,
            "project_id": product_id,
            "brief": req.brief,
            "profile": req.profile,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, _auth: str = Depends(require_operator_auth)) -> dict[str, Any]:
        """Get the current state and details of a build job."""
        row = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

        result_data = None
        raw_result = row.get("result") or row.get("artifact")
        if raw_result:
            try:
                result_data = json.loads(raw_result)
            except Exception:
                result_data = raw_result

        payload_data = None
        if row.get("payload"):
            try:
                payload_data = json.loads(row["payload"])
            except Exception:
                payload_data = row["payload"]

        tasks = store.tasks_for_job(job_id)

        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "state": row["state"],
            "created": row["created"],
            "started": row.get("started"),
            "finished": row.get("finished"),
            "cancel": bool(row.get("cancel", 0)),
            "error": row.get("error"),
            "payload": payload_data,
            "result": result_data,
            "artifact": result_data,
            "tasks": tasks,
        }

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, _auth: str = Depends(require_operator_auth)) -> dict[str, Any]:
        """Cancel an in-progress or queued job."""
        try:
            store.cancel(job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # If in active tasks, request cancellation
        task = active_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        return {"job_id": job_id, "status": "cancelling"}

    @app.get("/api/jobs")
    def list_jobs(
        limit: int = 30,
        _auth: str = Depends(require_operator_auth),
    ) -> list[dict[str, Any]]:
        """List recent build jobs with token usage summary."""
        rows = store.all(
            "SELECT id, project_id, kind, state, created, started, finished, error, result FROM jobs ORDER BY created DESC LIMIT ?",
            (limit,),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            token_usage: dict[str, Any] = {}
            raw_res = row.get("result")
            if raw_res:
                try:
                    parsed = json.loads(raw_res) if isinstance(raw_res, str) else raw_res
                    if isinstance(parsed, dict) and isinstance(parsed.get("token_usage"), dict):
                        token_usage = parsed["token_usage"]
                except Exception:
                    pass
            items.append(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "created": row["created"],
                    "started": row.get("started"),
                    "finished": row.get("finished"),
                    "error": row.get("error"),
                    "token_usage": token_usage,
                }
            )
        return items

    @app.get("/api/projects")
    def list_projects(_auth: str = Depends(require_operator_auth)) -> list[dict[str, Any]]:
        """List all registered projects."""
        return store.all(
            "SELECT id, name, profile, state, created FROM projects ORDER BY created DESC LIMIT 100"
        )

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, _auth: str = Depends(require_operator_auth)) -> dict[str, Any]:
        """Get product details, plan, and recent jobs."""
        try:
            proj = store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        jobs = store.all(
            "SELECT id, kind, state, created, finished, error FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 20",
            (project_id,),
        )

        plan_data = None
        if proj.get("plan"):
            try:
                plan_data = json.loads(proj["plan"])
            except Exception:
                plan_data = proj["plan"]

        artifact_data = None
        if proj.get("artifact"):
            try:
                artifact_data = json.loads(proj["artifact"])
            except Exception:
                artifact_data = proj["artifact"]

        return {
            "id": proj["id"],
            "name": proj["name"],
            "brief": proj["brief"],
            "profile": proj["profile"],
            "state": proj["state"],
            "created": proj["created"],
            "plan": plan_data,
            "artifact": artifact_data,
            "recent_jobs": jobs,
        }

    @app.post("/api/projects/{project_id}/adjust/preview")
    async def preview_adjust(
        project_id: str,
        req: AdjustPreviewRequest,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Classify affected domain hierarchy paths before planning an adjustment."""
        try:
            proj = store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        scope = await asyncio.to_thread(engine.preview_adjust_scope, proj, req.instruction)
        return scope.model_dump()

    @app.post("/api/projects/{project_id}/adjust", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_adjust(
        project_id: str,
        req: AdjustRequest,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Queue an incremental adjustment build scoped to confirmed domain paths."""
        try:
            proj = store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        payload = {
            "mode": "adjust",
            "instruction": req.instruction,
            "selected_domains": req.selected_domains,
            "target_files": req.target_files,
            "profile": proj.get("profile", "static"),
        }
        job = store.enqueue(project_id, kind="build", payload=payload)
        job_id = job["id"]

        task = asyncio.create_task(_run_build_in_background(project_id, job_id))
        active_tasks[job_id] = task

        return {
            "status": "queued",
            "mode": "adjust",
            "job_id": job_id,
            "project_id": project_id,
            "selected_domains": req.selected_domains,
        }

    @app.get("/api/artifacts/{project_id}/{job_id}")
    def download_artifact(
        project_id: str,
        job_id: str,
        _auth: str = Depends(require_operator_auth),
    ) -> FileResponse:
        """Download the release tarball artifact."""
        tarball_path = settings.data / "artifacts" / project_id / f"{job_id}.tar.gz"
        if not tarball_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Artifact not found for project {project_id} (job {job_id})",
            )

        return FileResponse(
            path=str(tarball_path),
            media_type="application/gzip",
            filename=f"{project_id}-{job_id[:8]}.tar.gz",
        )

    @app.get("/api/events")
    def list_events(
        project_id: str | None = None,
        limit: int = 50,
        _auth: str = Depends(require_operator_auth),
    ) -> list[dict[str, Any]]:
        """List recent event ledger records."""
        if project_id:
            rows = store.all(
                "SELECT * FROM events WHERE project_id=? ORDER BY seq DESC LIMIT ?",
                (project_id, limit),
            )
        else:
            rows = store.all("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))

        # Parse JSON detail where applicable
        for r in rows:
            if "detail" in r and isinstance(r["detail"], str):
                try:
                    r["detail"] = json.loads(r["detail"])
                except Exception:
                    pass
        return rows

    return app
