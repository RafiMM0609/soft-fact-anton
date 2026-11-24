from pathlib import Path
import tarfile
from factory.artifact import assemble_artifact, render_readme
from factory.models import Plan, Task


def test_render_readme():
    task = Task(id="t1", title="Setup core", instructions="Install deps", acceptance=["Must run"])
    plan = Plan(summary="Build awesome app", tasks=[task])
    readme = render_readme("my-app", "Build awesome app", plan=plan, profile="static")
    assert "# my-app" in readme
    assert "Build awesome app" in readme
    assert "**t1**: Setup core" in readme
    assert "factory run my-app --port 8080" in readme


def test_assemble_artifact(tmp_path: Path):
    source = tmp_path / "product_repo"
    source.mkdir()
    (source / "index.html").write_text("<h1>Hello World</h1>")
    (source / "style.css").write_text("body { color: red; }")

    # Excluded files/dirs that shouldn't appear in artifact tarball
    git_dir = source / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]")
    (source / ".env").write_text("SECRET=123")
    (source / ".DS_Store").write_bytes(b"\x00\x00")

    output_tarball = tmp_path / "artifacts" / "test-proj" / "job-1.tar.gz"
    res = assemble_artifact(
        source_root=source,
        output_tarball=output_tarball,
        project={"id": "test-proj", "name": "Test Project", "brief": "A simple test project"},
        profile="static",
    )

    assert output_tarball.is_file()
    assert res["artifact_path"] == str(output_tarball)
    assert res["digest"].startswith("sha256:")
    assert res["size_bytes"] > 0

    # Inspect contents of tarball
    with tarfile.open(output_tarball, "r:gz") as tar:
        names = tar.getnames()
        assert "index.html" in names
        assert "style.css" in names
        assert "README.md" in names
        assert ".git" not in names
        assert ".git/config" not in names
        assert ".env" not in names
        assert ".DS_Store" not in names
