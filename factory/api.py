"""FastAPI application for ACP Software Factory."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import dataclasses
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from acp.harness import detect_available_harnesses
from factory.config import Settings
from factory.db import Store
from factory.engine import Engine
from factory.products import product_root
from factory.testing import _find_free_port, _wait_for_port

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

DEFAULT_UI_SETTINGS: dict[str, Any] = {
    "brand_name": "acp-factory",
    "brand_subtitle": "AUTONOMOUS STUDIO",
    "client_label": "",
    "accent_color": "#3b82f6",
    "default_lang": "id",
}

SKIP_FILE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        "dist",
        "build",
        "vendor",
        ".zig-cache",
        "zig-out",
    }
)
MAX_FILE_PREVIEW_BYTES = 256_000
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "content-encoding",
    }
)


class BuildRequest(BaseModel):
    brief: str = Field(..., description="Product specification brief")
    product: str | None = Field(None, description="Product identifier / name")
    profile: str = Field("static", description="Product profile type (static, go-htmx, etc.)")
    harness: str | None = Field(
        default=None,
        pattern=r"^(agy|opencode|codex|pi|claude)$",
        description="Optional ACP harness override for this build",
    )
    model_id: str | None = None


class AdjustPreviewRequest(BaseModel):
    instruction: str = Field(..., min_length=3, description="Requested adjustment instruction")


class AdjustRequest(BaseModel):
    instruction: str = Field(..., min_length=3, description="Requested adjustment instruction")
    selected_domains: list[str] = Field(default_factory=list, description="Confirmed domain paths")
    target_files: list[str] = Field(default_factory=list, description="Target files from preview")


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    brief: str | None = Field(default=None, min_length=1, max_length=24000)
    profile: str | None = Field(default=None, pattern=r"^(static|go-htmx|node|zig)$")


class UiSettingsUpdate(BaseModel):
    brand_name: str = Field(default="acp-factory", min_length=1, max_length=60)
    brand_subtitle: str = Field(default="AUTONOMOUS STUDIO", max_length=60)
    client_label: str = Field(default="", max_length=80)
    accent_color: str = Field(default="#3b82f6", pattern=r"^#[0-9a-fA-F]{6}$")
    default_lang: str = Field(default="id", pattern=r"^(id|en)$")


def _parse_event_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        if "detail" in r and isinstance(r["detail"], str):
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
    return rows


def _start_backend_preview(
    project_dir: Path,
) -> tuple[subprocess.Popen[bytes], tempfile.TemporaryDirectory[str] | None, int]:
    host = "127.0.0.1"
    port = _find_free_port()
    env = os.environ.copy()
    env.update({"HOST": host, "PORT": str(port)})

    proc: subprocess.Popen[bytes] | None = None
    tmpdir_obj: tempfile.TemporaryDirectory[str] | None = None

    try:
        if (project_dir / "go.mod").is_file():
            target = (
                "./cmd/server"
                if (project_dir / "cmd" / "server" / "main.go").is_file()
                and not (project_dir / "main.go").is_file()
                else "."
            )
            tmpdir_obj = tempfile.TemporaryDirectory(prefix="factory_preview_go_")
            bin_path = Path(tmpdir_obj.name) / "preview_server"
            build_res = subprocess.run(
                ["go", "build", "-o", str(bin_path), target],
                cwd=project_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if build_res.returncode != 0:
                raise RuntimeError(f"Go build failed: {build_res.stderr[-500:]}")
            proc = subprocess.Popen(
                [str(bin_path)],
                cwd=project_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        elif (project_dir / "build.zig").is_file():
            proc = subprocess.Popen(
                ["zig", "build", "run"],
                cwd=project_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        elif (project_dir / "package.json").is_file():
            proc = subprocess.Popen(
                ["npm", "start"],
                cwd=project_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            raise RuntimeError("No backend entrypoint detected")

        if not _wait_for_port(host, port, timeout=20.0):
            stderr_tail = ""
            if proc.poll() is not None and proc.stderr:
                stderr_tail = proc.stderr.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"Preview backend failed to listen on port {port}. {stderr_tail}".strip())

        return proc, tmpdir_obj, port
    except Exception:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if tmpdir_obj is not None:
            tmpdir_obj.cleanup()
        raise


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()

    # Ensure directories exist
    settings.data.mkdir(parents=True, exist_ok=True)
    settings.products_dir.mkdir(parents=True, exist_ok=True)

    store = Store(settings.db_path)
    engine = Engine(settings=settings, store=store)

    # In-memory tracking of active background tasks and live preview sessions
    active_tasks: dict[str, asyncio.Task[Any]] = {}
    preview_sessions: dict[str, dict[str, Any]] = {}

    def _stop_preview_session(project_id: str) -> bool:
        sess = preview_sessions.pop(project_id, None)
        if not sess:
            return False
        proc = sess.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        tmpdir = sess.get("tmpdir")
        if tmpdir is not None:
            try:
                tmpdir.cleanup()
            except OSError:
                pass
        return True

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            for pid in list(preview_sessions):
                _stop_preview_session(pid)

    app = FastAPI(
        title="ACP Software Factory API",
        version="0.1.0",
        description="REST API for autonomous software factory operations",
        lifespan=lifespan,
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

    def _safe_product_dir(project_id: str) -> Path:
        if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
            raise HTTPException(status_code=400, detail="Invalid project identifier")
        base_products = settings.products_dir.resolve()
        candidate = product_root(settings.data, project_id).resolve()
        if not candidate.is_relative_to(base_products) or candidate == base_products:
            raise HTTPException(status_code=400, detail="Invalid project path")
        return candidate

    def _is_blocked_rel_part(part: str) -> bool:
        return part in ("", ".", "..", ".git") or part.startswith(".env")

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
        import sys

        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        resolved_harness = settings.resolve_harness_path()
        harness_ok = bool(
            resolved_harness
            and Path(resolved_harness).is_file()
            and os.access(resolved_harness, os.X_OK)
        )
        available_harnesses = detect_available_harnesses()

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
            "harness_path": resolved_harness,
            "available_harnesses": available_harnesses,
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

            job_engine = engine
            raw_payload = job.get("payload")
            if raw_payload:
                try:
                    parsed_payload = (
                        json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                    )
                except json.JSONDecodeError:
                    parsed_payload = None
                if isinstance(parsed_payload, dict) and parsed_payload.get("harness"):
                    job_harness = str(parsed_payload["harness"]).strip().lower()
                    job_model = parsed_payload.get("model_id") if job_harness == "opencode" else None
                    if job_harness and (job_harness != settings.harness.strip().lower() or job_model):
                        job_settings = dataclasses.replace(
                            settings, harness=job_harness, opencode_model=job_model
                        )
                        job_engine = Engine(settings=job_settings, store=store)

            logger.info("Starting background build for project %s (job %s)", project_id, job_id)
            await asyncio.to_thread(
                job_engine.build,
                project=proj,
                job=job,
                cancelled=is_cancelled,
            )
            logger.info("Background build completed successfully for job %s", job_id)
        except Exception as exc:
            logger.exception("Background build failed for job %s: %s", job_id, exc)
            unfinished = store.all("SELECT id FROM jobs WHERE id=? AND state IN ('queued', 'running')", (job_id,))
            if unfinished:
                store.finish(job_id, state="failed", error=str(exc))
        finally:
            active_tasks.pop(job_id, None)

    @app.post("/api/build", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_build(
        req: BuildRequest,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Trigger an autonomous product build in the background."""
        active_harness = (req.harness or settings.harness).strip().lower()
        model_id = (req.model_id or "").strip()
        if model_id:
            if active_harness != "opencode":
                raise HTTPException(status_code=422, detail="Model ID is only supported for OpenCode")
            if not re.fullmatch(r"[\w.+:@-]+(?:/[\w.+:@-]+)+", model_id, flags=re.ASCII):
                raise HTTPException(status_code=422, detail="Use provider/model format for OpenCode Model ID")
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
        payload = {"brief": req.brief, "profile": req.profile, "harness": active_harness}
        if model_id:
            payload["model_id"] = model_id
        job = store.enqueue(
            product_id,
            kind="build",
            payload=payload,
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
            "harness": active_harness,
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
        acp_errors = store.all(
            "SELECT seq, created, detail FROM events WHERE project_id=? AND job_id=? "
            "AND kind='acp.error' ORDER BY seq DESC",
            (row["project_id"], job_id),
        )
        for event in acp_errors:
            event["detail"] = json.loads(event["detail"])

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
            "acp_errors": acp_errors,
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
            "SELECT id, name, brief, profile, state, created FROM projects ORDER BY created DESC LIMIT 100"
        )

    @app.get("/api/settings/ui")
    def get_ui_settings() -> dict[str, Any]:
        """Return UI white-label branding settings (public so login screen can render brand)."""
        rows = store.all("SELECT value FROM runtime WHERE key='ui_branding'")
        merged = dict(DEFAULT_UI_SETTINGS)
        if rows:
            try:
                stored = json.loads(rows[0]["value"])
                if isinstance(stored, dict):
                    for k in DEFAULT_UI_SETTINGS:
                        if k in stored and stored[k] is not None:
                            merged[k] = stored[k]
            except Exception:
                pass
        return merged

    @app.put("/api/settings/ui")
    def update_ui_settings(
        req: UiSettingsUpdate,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Persist white-label branding settings."""
        data = req.model_dump()
        with store.tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO runtime(key, value) VALUES(?, ?)",
                ("ui_branding", json.dumps(data)),
            )
        return data

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, _auth: str = Depends(require_operator_auth)) -> dict[str, Any]:
        """Get product details, plan, recent jobs, events, and task states."""
        try:
            proj = store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        jobs = store.all(
            "SELECT id, kind, state, created, started, finished, error FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 20",
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

        recent_events = _parse_event_rows(
            store.all(
                "SELECT * FROM events WHERE project_id=? ORDER BY seq DESC LIMIT 50",
                (project_id,),
            )
        )
        latest_job_tasks = store.tasks_for_job(jobs[0]["id"]) if jobs else []

        has_files = False
        try:
            pdir = _safe_product_dir(project_id)
            if pdir.is_dir():
                for child in pdir.iterdir():
                    if child.name not in SKIP_FILE_DIRS and not child.name.startswith(".env"):
                        has_files = True
                        break
        except HTTPException:
            has_files = False

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
            "recent_events": recent_events,
            "latest_job_tasks": latest_job_tasks,
            "has_files": has_files,
        }

    @app.patch("/api/projects/{project_id}")
    def update_project(
        project_id: str,
        req: UpdateProjectRequest,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Update project name, brief, or architecture profile."""
        try:
            store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        updates: list[str] = []
        params: list[Any] = []
        changed_fields: list[str] = []
        if req.name is not None:
            updates.append("name=?")
            params.append(req.name.strip())
            changed_fields.append("name")
        if req.brief is not None:
            updates.append("brief=?")
            params.append(req.brief.strip())
            changed_fields.append("brief")
        if req.profile is not None:
            updates.append("profile=?")
            params.append(req.profile)
            changed_fields.append("profile")

        if updates:
            params.append(project_id)
            with store.tx() as db:
                db.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id=?", tuple(params))
                store.event_tx(db, project_id, None, "project.updated", {"fields": changed_fields})

        updated = store.project(project_id)
        return {
            "id": updated["id"],
            "name": updated["name"],
            "brief": updated["brief"],
            "profile": updated["profile"],
            "state": updated["state"],
            "created": updated["created"],
        }

    @app.delete("/api/projects/{project_id}")
    def delete_project(
        project_id: str,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Delete a project, its DB history, product repository, and release artifacts."""
        try:
            store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        active = store.all(
            "SELECT id FROM jobs WHERE project_id=? AND state IN ('queued','running') LIMIT 1",
            (project_id,),
        )
        if active:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete project while a job is queued or running",
            )

        _stop_preview_session(project_id)

        with store.tx() as db:
            db.execute("DELETE FROM workers WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM tasks WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM events WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))

        base_products = settings.products_dir.resolve()
        prod_dir = _safe_product_dir(project_id)
        if prod_dir.is_dir() and prod_dir.is_relative_to(base_products) and prod_dir != base_products:
            shutil.rmtree(prod_dir, ignore_errors=True)

        base_artifacts = (settings.data / "artifacts").resolve()
        art_dir = (base_artifacts / project_id).resolve()
        if art_dir.is_dir() and art_dir.is_relative_to(base_artifacts) and art_dir != base_artifacts:
            shutil.rmtree(art_dir, ignore_errors=True)

        return {"deleted": True, "project_id": project_id}

    @app.get("/api/projects/{project_id}/files")
    def list_project_files(
        project_id: str,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """List safe source files inside the generated product directory."""
        try:
            store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        base = _safe_product_dir(project_id)
        if not base.is_dir():
            return {"project_id": project_id, "files": []}

        files: list[dict[str, Any]] = []
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_relative_to(base):
                continue
            rel = path.relative_to(base)
            if any(part in SKIP_FILE_DIRS or part.startswith(".env") for part in rel.parts):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(
                {
                    "path": rel.as_posix(),
                    "size": size,
                    "ext": path.suffix.lstrip("."),
                }
            )
            if len(files) >= 300:
                break

        return {"project_id": project_id, "files": files}

    @app.get("/api/projects/{project_id}/file")
    def read_project_file(
        project_id: str,
        path: str,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Read the contents of a single generated source file safely."""
        try:
            store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        base = _safe_product_dir(project_id)
        if not base.is_dir():
            raise HTTPException(status_code=404, detail="Product directory not found")

        rel_clean = path.strip().lstrip("/")
        if not rel_clean or "\\" in rel_clean:
            raise HTTPException(status_code=400, detail="Invalid file path")
        parts = [p for p in rel_clean.split("/") if p]
        if any(_is_blocked_rel_part(p) or p in SKIP_FILE_DIRS for p in parts):
            raise HTTPException(status_code=403, detail="Access to this path is forbidden")

        target = (base / rel_clean).resolve()
        if not target.is_relative_to(base):
            raise HTTPException(status_code=403, detail="Path escapes product directory")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {rel_clean}")

        size = target.stat().st_size
        with target.open("rb") as fh:
            raw = fh.read(MAX_FILE_PREVIEW_BYTES + 1)

        truncated = len(raw) > MAX_FILE_PREVIEW_BYTES
        sample = raw[:MAX_FILE_PREVIEW_BYTES]
        if b"\x00" in sample:
            return {
                "path": target.relative_to(base).as_posix(),
                "size": size,
                "binary": True,
                "content": "",
                "truncated": False,
            }

        return {
            "path": target.relative_to(base).as_posix(),
            "size": size,
            "binary": False,
            "content": sample.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    @app.post("/api/projects/{project_id}/preview/start")
    async def start_project_preview(
        project_id: str,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Start or reuse an authenticated live preview session for a built project."""
        try:
            store.project(project_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Project not found: {project_id}")

        project_dir = _safe_product_dir(project_id)
        if not project_dir.is_dir():
            raise HTTPException(status_code=404, detail="Product directory has not been built yet")

        existing = preview_sessions.get(project_id)
        if existing:
            if existing["mode"] == "static" or (
                existing.get("proc") is not None and existing["proc"].poll() is None
            ):
                return {
                    "project_id": project_id,
                    "mode": existing["mode"],
                    "preview_url": f"/preview/{project_id}/?pt={existing['token']}",
                }
            _stop_preview_session(project_id)

        token = secrets.token_urlsafe(24)
        is_backend = any(
            (project_dir / marker).is_file() for marker in ("go.mod", "build.zig", "package.json")
        )

        if is_backend:
            try:
                proc, tmpdir_obj, port = await asyncio.to_thread(_start_backend_preview, project_dir)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Failed to start backend preview: {exc}")
            preview_sessions[project_id] = {
                "token": token,
                "project_id": project_id,
                "mode": "proxy",
                "upstream_url": f"http://127.0.0.1:{port}",
                "proc": proc,
                "tmpdir": tmpdir_obj,
                "created": time.time(),
            }
            mode = "proxy"
        else:
            preview_sessions[project_id] = {
                "token": token,
                "project_id": project_id,
                "mode": "static",
                "upstream_url": None,
                "proc": None,
                "tmpdir": None,
                "created": time.time(),
            }
            mode = "static"

        return {
            "project_id": project_id,
            "mode": mode,
            "preview_url": f"/preview/{project_id}/?pt={token}",
        }

    @app.post("/api/projects/{project_id}/preview/stop")
    def stop_project_preview(
        project_id: str,
        _auth: str = Depends(require_operator_auth),
    ) -> dict[str, Any]:
        """Stop an active live preview session."""
        stopped = _stop_preview_session(project_id)
        return {"project_id": project_id, "stopped": stopped}

    @app.get("/preview/{project_id}")
    def preview_root_redirect(project_id: str, request: Request) -> RedirectResponse:
        """Redirect /preview/<id> to /preview/<id>/ preserving query token."""
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/preview/{project_id}/{qs}", status_code=307)

    @app.api_route(
        "/preview/{project_id}/{subpath:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
    )
    async def serve_preview(project_id: str, subpath: str, request: Request) -> Response:
        """Serve static build files or reverse-proxy to a managed backend preview server."""
        sess = preview_sessions.get(project_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Preview session not active")

        pt = request.query_params.get("pt")
        cookie_pt = request.cookies.get("factory_preview_token")
        valid_pt = bool(pt and secrets.compare_digest(pt, sess["token"]))
        valid_cookie = bool(cookie_pt and secrets.compare_digest(cookie_pt, sess["token"]))
        if not (valid_pt or valid_cookie):
            raise HTTPException(status_code=401, detail="Unauthorized preview session")

        response: Response
        if sess["mode"] == "static":
            if request.method not in ("GET", "HEAD"):
                raise HTTPException(status_code=405, detail="Method not allowed for static preview")

            base = _safe_product_dir(project_id)
            clean_sub = subpath.lstrip("/")
            raw_parts = [p for p in clean_sub.split("/") if p]
            if any(_is_blocked_rel_part(p) for p in raw_parts):
                raise HTTPException(status_code=403, detail="Forbidden path")

            if not clean_sub or clean_sub.endswith("/"):
                rel_candidates = (
                    [f"{clean_sub}index.html"]
                    if clean_sub
                    else ["index.html", "public/index.html", "dist/index.html"]
                )
            else:
                rel_candidates = [clean_sub]

            target: Path | None = None
            for cand in rel_candidates:
                cand_path = (base / cand).resolve()
                if not cand_path.is_relative_to(base):
                    raise HTTPException(status_code=403, detail="Path escapes product directory")
                if cand_path.is_file():
                    target = cand_path
                    break

            if target is None:
                raise HTTPException(status_code=404, detail="Preview file not found")

            media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            response = FileResponse(path=str(target), media_type=media_type)
        else:
            proc = sess.get("proc")
            if proc is not None and proc.poll() is not None:
                raise HTTPException(status_code=502, detail="Preview backend process exited")

            fwd_params = [(k, v) for k, v in request.query_params.multi_items() if k != "pt"]
            qs = urlencode(fwd_params)
            target_url = f"{sess['upstream_url']}/{subpath.lstrip('/')}"
            if qs:
                target_url = f"{target_url}?{qs}"

            fwd_headers = {
                k: v
                for k, v in request.headers.items()
                if k.lower() not in HOP_BY_HOP_HEADERS
                and k.lower() not in ("cookie", "accept-encoding")
            }
            body = await request.body()

            async with httpx.AsyncClient(follow_redirects=False, timeout=20.0, trust_env=False) as client:
                upstream_resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    content=body if body else None,
                )

            resp_headers = {
                k: v
                for k, v in upstream_resp.headers.items()
                if k.lower() not in HOP_BY_HOP_HEADERS
            }
            response = Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type=upstream_resp.headers.get("content-type"),
            )

        if valid_pt:
            response.set_cookie(
                key="factory_preview_token",
                value=sess["token"],
                httponly=True,
                samesite="lax",
                path=f"/preview/{project_id}",
            )
        return response

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

        return _parse_event_rows(rows)

    @app.get("/api/events/stream")
    async def stream_events(
        request: Request,
        project_id: str | None = None,
        after_seq: int = 0,
        once: bool = False,
        _auth: str = Depends(require_operator_auth),
    ) -> StreamingResponse:
        """Stream factory events in real time using Server-Sent Events (SSE)."""

        async def event_generator():
            cursor = after_seq
            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    break

                if project_id:
                    rows = store.all(
                        "SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq ASC LIMIT 50",
                        (project_id, cursor),
                    )
                else:
                    rows = store.all(
                        "SELECT * FROM events WHERE seq>? ORDER BY seq ASC LIMIT 50",
                        (cursor,),
                    )

                _parse_event_rows(rows)
                if rows:
                    idle_ticks = 0
                    for r in rows:
                        cursor = int(r["seq"])
                        yield f"id: {cursor}\nevent: factory_event\ndata: {json.dumps(r)}\n\n"
                elif once:
                    yield ": connected\n\n"
                    break
                else:
                    idle_ticks += 1
                    if idle_ticks >= 15:
                        idle_ticks = 0
                        yield ": heartbeat\n\n"

                if once:
                    break

                await asyncio.sleep(1.0)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
