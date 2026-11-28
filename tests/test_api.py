import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from factory.api import create_app
from factory.cli import main
from factory.config import Settings
from factory.db import Store


@pytest.fixture
def test_app_and_client(tmp_path: Path):
    data_dir = tmp_path / "factory_data"
    data_dir.mkdir(parents=True)
    token = "test-operator-secret-token-12345"

    settings = Settings(data=data_dir, operator_token=token)
    app = create_app(settings)
    client = TestClient(app)
    store = Store(settings.db_path)

    return settings, client, store, token


def test_api_health(test_app_and_client):
    _, client, _, _ = test_app_and_client
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


def test_api_doctor_auth(test_app_and_client):
    _, client, _, token = test_app_and_client

    # Without auth -> 401
    resp = client.get("/api/doctor")
    assert resp.status_code == 401

    # With invalid auth -> 401
    resp = client.get("/api/doctor", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401

    # With valid auth -> 200
    resp = client.get("/api/doctor", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["python"]
    assert "git" in data


def test_api_build_and_job_lifecycle(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    with patch("factory.engine.Engine.build", autospec=True) as mock_build:
        mock_build.return_value = {"status": "succeeded", "digest": "sha256:dummy"}

        # 1. Trigger build (with explicit opencode harness override)
        payload = {
            "brief": "Build a coffee shop website",
            "product": "coffee-shop-api",
            "profile": "static",
            "harness": "opencode",
            "model_id": " provider/model ",
        }
        resp = client.post("/api/build", json=payload, headers=headers)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["harness"] == "opencode"
        job_id = body["job_id"]
        assert body["project_id"] == "coffee-shop-api"
        build_engine = mock_build.call_args.args[0]
        assert build_engine.agent.model == build_engine.worker.model == "provider/model"
        store.record_acp_error(
            "coffee-shop-api", job_id, "worker", build_engine.worker,
            RuntimeError("Model unavailable"), task_id="setup", attempt=1,
        )

        # 2. Get Job
        resp_job = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp_job.status_code == 200
        job_data = resp_job.json()
        assert job_data["id"] == job_id
        assert job_data["project_id"] == "coffee-shop-api"
        assert job_data["acp_errors"][0]["detail"] == {
            "stage": "worker",
            "harness": "opencode",
            "model_id": "provider/model",
            "error": "Model unavailable",
            "task_id": "setup",
            "attempt": 1,
        }

        # 3. Get Project
        resp_proj = client.get("/api/projects/coffee-shop-api", headers=headers)
        assert resp_proj.status_code == 200
        proj_data = resp_proj.json()
        assert proj_data["id"] == "coffee-shop-api"
        assert len(proj_data["recent_jobs"]) >= 1


def test_api_build_with_distinct_planner_and_worker(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "brief": "Build a site",
        "planner_harness": "opencode",
        "planner_model_id": "opencode/planner",
        "worker_harness": "pi",
        "worker_model_id": "provider/worker",
    }
    invalid = client.post("/api/build", json={**payload, "worker_harness": "agy"}, headers=headers)
    assert invalid.status_code == 422 and "Worker Model ID" in invalid.json()["detail"]
    with patch("factory.engine.Engine.build", autospec=True) as mock_build:
        response = client.post("/api/build", json=payload, headers=headers)
        assert response.status_code == 202
        build_engine = mock_build.call_args.args[0]
        assert (build_engine.agent.agent_name, build_engine.agent.model) == ("opencode", "opencode/planner")
        assert (build_engine.worker.agent_name, build_engine.worker.model) == ("pi", "provider/worker")
    job_id = response.json()["job_id"]
    details = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert all(details["payload"][key] == value for key, value in payload.items())


def test_api_retry_failed_build_with_new_harness(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}
    project_id = store.create_project({"name": "Retry", "brief": "Build a site"})["id"]
    source = store.enqueue(project_id, "build", {
        "harness": "agy", "mode": "adjust", "instruction": "Fix mobile layout",
    })
    store.event(project_id, source["id"], "build.tasks_started", {})
    store.finish(source["id"], "failed", error="Worker timeout")
    url = f"/api/jobs/{source['id']}"
    assert client.get(url, headers=headers).json()["can_retry"]

    with patch("factory.engine.Engine.build", autospec=True) as mock_build:
        res = client.post(f"{url}/retry", json={
            "harness": "opencode", "model_id": "opencode/big-pickle",
        }, headers=headers)
        assert res.status_code == 202
        assert mock_build.call_args.args[0].worker.model == "opencode/big-pickle"

    retried = client.get(f"/api/jobs/{res.json()['job_id']}", headers=headers).json()
    assert retried["payload"] == {
        "harness": "opencode", "model_id": "opencode/big-pickle", "mode": "adjust",
        "instruction": "Fix mobile layout", "plan_ready": True, "retry_of": source["id"],
    }
    original = client.get(url, headers=headers).json()
    assert original["error"] == "Worker timeout" and not original["can_retry"]
    assert client.post(f"{url}/retry", json={"harness": "agy"}, headers=headers).status_code == 409


def test_api_cancel_job(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    # Create dummy project and job directly
    with store.tx() as db:
        db.execute(
            "INSERT INTO projects(id, name, brief, profile, created) VALUES(?,?,?,?,?)",
            ("cancel-proj", "cancel-proj", "brief", "static", 100.0),
        )
    job = store.enqueue("cancel-proj", kind="build", payload={"brief": "brief"})
    job_id = job["id"]

    # Cancel via API
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelling"

    # Verify state in store
    job_row = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert job_row["cancel"] == 1


def test_api_download_artifact(test_app_and_client):
    settings, client, _, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    # 404 for missing artifact
    resp_missing = client.get("/api/artifacts/p1/j1", headers=headers)
    assert resp_missing.status_code == 404

    # Create fake artifact
    artifacts_dir = settings.data / "artifacts" / "p1"
    artifacts_dir.mkdir(parents=True)
    tar_path = artifacts_dir / "j1.tar.gz"
    with tarfile.open(tar_path, "w:gz"):
        pass

    resp_found = client.get("/api/artifacts/p1/j1", headers=headers)
    assert resp_found.status_code == 200
    assert resp_found.headers["content-type"] == "application/gzip"


def test_api_events(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    with store.tx() as db:
        store.event_tx(db, "proj-evt", "job-evt", "test.event", {"foo": "bar"})

    resp = client.get("/api/events?project_id=proj-evt", headers=headers)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["kind"] == "test.event"


def test_api_dashboard_and_list_endpoints(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    # Dashboard HTML shell and static assets
    resp_html = client.get("/")
    assert resp_html.status_code == 200
    assert "acp-factory" in resp_html.text

    resp_css = client.get("/static/style.css")
    assert resp_css.status_code == 200

    resp_js = client.get("/static/app.js")
    assert resp_js.status_code == 200

    resp_i18n = client.get("/static/i18n.js")
    assert resp_i18n.status_code == 200

    # List projects and jobs
    proj = store.create_project({"name": "dash-proj", "brief": "brief", "profile": "static"})
    store.enqueue(proj["id"], kind="build", payload={"brief": "brief"})

    resp_projs = client.get("/api/projects", headers=headers)
    assert resp_projs.status_code == 200
    projs_list = resp_projs.json()
    assert any(p["id"] == proj["id"] and "brief" in p for p in projs_list)

    resp_jobs = client.get("/api/jobs", headers=headers)
    assert resp_jobs.status_code == 200
    assert any(j["project_id"] == proj["id"] for j in resp_jobs.json())


def test_api_ui_settings_get_and_update(test_app_and_client):
    _, client, _, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    # Public GET works without auth
    resp_default = client.get("/api/settings/ui")
    assert resp_default.status_code == 200
    assert resp_default.json()["brand_name"] == "acp-factory"

    # PUT requires auth
    resp_unauth = client.put("/api/settings/ui", json={"brand_name": "Acme Studio"})
    assert resp_unauth.status_code == 401

    # Valid PUT updates and persists
    payload = {
        "brand_name": "Acme Digital Studio",
        "brand_subtitle": "CLIENT DELIVERY",
        "client_label": "Prepared for PT Nusantara",
        "accent_color": "#10b981",
        "default_lang": "en",
    }
    resp_put = client.put("/api/settings/ui", json=payload, headers=headers)
    assert resp_put.status_code == 200
    assert resp_put.json() == payload

    resp_after = client.get("/api/settings/ui")
    assert resp_after.status_code == 200
    assert resp_after.json()["brand_name"] == "Acme Digital Studio"
    assert resp_after.json()["accent_color"] == "#10b981"


def test_api_project_update_and_delete(test_app_and_client):
    settings, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    proj = store.create_project({"name": "orig-name", "brief": "orig brief", "profile": "static"})
    pid = proj["id"]
    prod_dir = settings.products_dir / pid
    prod_dir.mkdir(parents=True, exist_ok=True)
    (prod_dir / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")

    # PATCH updates metadata
    resp_patch = client.patch(
        f"/api/projects/{pid}",
        headers=headers,
        json={"name": "New Client Name", "brief": "Updated brief", "profile": "go-htmx"},
    )
    assert resp_patch.status_code == 200
    assert resp_patch.json()["name"] == "New Client Name"
    assert resp_patch.json()["profile"] == "go-htmx"

    # Active queued job blocks DELETE with 409
    job = store.enqueue(pid, kind="build", payload={"brief": "Updated brief"})
    resp_del_conflict = client.delete(f"/api/projects/{pid}", headers=headers)
    assert resp_del_conflict.status_code == 409

    # Cancel job, then DELETE succeeds and cleans up disk & DB
    store.cancel(job["id"])
    resp_del = client.delete(f"/api/projects/{pid}", headers=headers)
    assert resp_del.status_code == 200
    assert resp_del.json()["deleted"] is True
    assert not prod_dir.exists()
    assert client.get(f"/api/projects/{pid}", headers=headers).status_code == 404


def test_api_project_files_and_file_content(test_app_and_client):
    settings, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    proj = store.create_project({"name": "files-proj", "brief": "brief", "profile": "static"})
    pid = proj["id"]
    prod_dir = settings.products_dir / pid
    (prod_dir / "src").mkdir(parents=True, exist_ok=True)
    (prod_dir / ".git").mkdir(parents=True, exist_ok=True)
    (prod_dir / "index.html").write_text("<!doctype html><h1>Hi</h1>", encoding="utf-8")
    (prod_dir / "src" / "app.js").write_text("console.log('ok');", encoding="utf-8")
    (prod_dir / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (prod_dir / ".env").write_text("SECRET=123", encoding="utf-8")

    resp_list = client.get(f"/api/projects/{pid}/files", headers=headers)
    assert resp_list.status_code == 200
    listed_paths = [f["path"] for f in resp_list.json()["files"]]
    assert "index.html" in listed_paths
    assert "src/app.js" in listed_paths
    assert ".git/HEAD" not in listed_paths
    assert ".env" not in listed_paths

    resp_read = client.get(f"/api/projects/{pid}/file?path=src/app.js", headers=headers)
    assert resp_read.status_code == 200
    assert resp_read.json()["content"] == "console.log('ok');"

    # Security checks on traversal and hidden git/env files
    assert client.get(f"/api/projects/{pid}/file?path=../secret", headers=headers).status_code == 403
    assert client.get(f"/api/projects/{pid}/file?path=.git/HEAD", headers=headers).status_code == 403
    assert client.get(f"/api/projects/{pid}/file?path=.env", headers=headers).status_code == 403


def test_api_live_preview_static(test_app_and_client):
    settings, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    proj = store.create_project({"name": "preview-proj", "brief": "brief", "profile": "static"})
    pid = proj["id"]
    prod_dir = settings.products_dir / pid
    (prod_dir / ".git").mkdir(parents=True, exist_ok=True)
    (prod_dir / "index.html").write_text("<h1>Live Preview Works</h1>", encoding="utf-8")
    (prod_dir / "app.css").write_text("body { color: #111; }", encoding="utf-8")
    (prod_dir / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    resp_start = client.post(f"/api/projects/{pid}/preview/start", headers=headers)
    assert resp_start.status_code == 200
    data = resp_start.json()
    assert data["mode"] == "static"
    preview_url = data["preview_url"]

    # Unauthorized without pt token or cookie
    resp_no_tok = client.get(f"/preview/{pid}/")
    assert resp_no_tok.status_code == 401

    # Valid pt token serves HTML and sets factory_preview_token cookie
    resp_ok = client.get(preview_url)
    assert resp_ok.status_code == 200
    assert "Live Preview Works" in resp_ok.text
    assert "factory_preview_token" in resp_ok.cookies

    # Subsequent asset request works using the cookie alone
    resp_css = client.get(f"/preview/{pid}/app.css")
    assert resp_css.status_code == 200
    assert "color: #111" in resp_css.text

    # Sensitive files remain blocked
    resp_git = client.get(f"/preview/{pid}/.git/HEAD")
    assert resp_git.status_code == 403

    # Stop preview clears session
    resp_stop = client.post(f"/api/projects/{pid}/preview/stop", headers=headers)
    assert resp_stop.status_code == 200
    assert resp_stop.json()["stopped"] is True
    assert client.get(preview_url).status_code == 404


def test_api_events_stream_once(test_app_and_client):
    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}

    with store.tx() as db:
        store.event_tx(db, "sse-proj", "sse-job", "verify.passed", {"ok": True})

    resp = client.get(
        "/api/events/stream?project_id=sse-proj&after_seq=0&once=true",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: factory_event" in resp.text
    assert "verify.passed" in resp.text


def test_api_adjust_preview_and_trigger(test_app_and_client):
    from factory.models import AdjustScope, DomainCandidate

    _, client, store, token = test_app_and_client
    headers = {"Authorization": f"Bearer {token}"}
    proj = store.create_project({"name": "adj-proj", "brief": "brief", "profile": "static"})

    mock_scope = AdjustScope(
        summary="Form input user",
        candidates=[
            DomainCandidate(
                domain_path="User > Form Input Data Detail User",
                reason="Matches user detail form request",
                target_files=["index.html", "app.js"],
            )
        ],
    )

    with patch("factory.engine.Engine.preview_adjust_scope", return_value=mock_scope):
        resp_prev = client.post(
            f"/api/projects/{proj['id']}/adjust/preview",
            headers=headers,
            json={"instruction": "Ubah form input data user"},
        )
        assert resp_prev.status_code == 200
        assert resp_prev.json()["candidates"][0]["domain_path"] == "User > Form Input Data Detail User"

    with patch("factory.engine.Engine.build", return_value={"status": "succeeded"}):
        resp_adj = client.post(
            f"/api/projects/{proj['id']}/adjust",
            headers=headers,
            json={
                "instruction": "Ubah form input data user",
                "selected_domains": ["User > Form Input Data Detail User"],
                "target_files": ["index.html", "app.js"],
            },
        )
        assert resp_adj.status_code == 202
        data = resp_adj.json()
        assert data["mode"] == "adjust"
        assert data["project_id"] == proj["id"]


def test_cli_main_api_parser():
    with patch("factory.cli.api_command") as mock_api:
        mock_api.return_value = 0
        ret = main(["api", "--port", "8000", "--host", "127.0.0.1"])
        assert ret == 0
        mock_api.assert_called_once_with(8000, "127.0.0.1", None)
