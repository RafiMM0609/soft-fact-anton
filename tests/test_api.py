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

    with patch("factory.engine.Engine.build") as mock_build:
        mock_build.return_value = {"status": "succeeded", "digest": "sha256:dummy"}

        # 1. Trigger build
        payload = {
            "brief": "Build a coffee shop website",
            "product": "coffee-shop-api",
            "profile": "static",
        }
        resp = client.post("/api/build", json=payload, headers=headers)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        job_id = body["job_id"]
        assert body["project_id"] == "coffee-shop-api"

        # 2. Get Job
        resp_job = client.get(f"/api/jobs/{job_id}", headers=headers)
        assert resp_job.status_code == 200
        job_data = resp_job.json()
        assert job_data["id"] == job_id
        assert job_data["project_id"] == "coffee-shop-api"

        # 3. Get Project
        resp_proj = client.get("/api/projects/coffee-shop-api", headers=headers)
        assert resp_proj.status_code == 200
        proj_data = resp_proj.json()
        assert proj_data["id"] == "coffee-shop-api"
        assert len(proj_data["recent_jobs"]) >= 1


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

    # List projects and jobs
    proj = store.create_project({"name": "dash-proj", "brief": "brief", "profile": "static"})
    store.enqueue(proj["id"], kind="build", payload={"brief": "brief"})

    resp_projs = client.get("/api/projects", headers=headers)
    assert resp_projs.status_code == 200
    assert any(p["id"] == proj["id"] for p in resp_projs.json())

    resp_jobs = client.get("/api/jobs", headers=headers)
    assert resp_jobs.status_code == 200
    assert any(j["project_id"] == proj["id"] for j in resp_jobs.json())


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
