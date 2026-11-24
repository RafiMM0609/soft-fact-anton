from pathlib import Path
import pytest
from checks.secret_scan import scan_directory


def test_secret_scan_clean_dir(tmp_path: Path):
    clean_file = tmp_path / "app.py"
    clean_file.write_text("print('Hello, world!')\nAPI_URL = 'https://api.example.com'")
    findings = scan_directory(tmp_path)
    assert findings == []


def test_secret_scan_detects_openai_key(tmp_path: Path):
    bad_file = tmp_path / "config.py"
    bad_file.write_text("OPENAI_KEY = 'sk-proj-abcdefghijklmnopqrstuvwxyz1234567890'")
    findings = scan_directory(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "openai_key"
    assert "config.py" in findings[0].file_path


def test_secret_scan_detects_anthropic_key(tmp_path: Path):
    bad_file = tmp_path / "secret.env"
    bad_file.write_text("ANTHROPIC_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz1234567890")
    findings = scan_directory(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "anthropic_key"


def test_secret_scan_detects_github_token(tmp_path: Path):
    bad_file = tmp_path / "ci.yml"
    bad_file.write_text("GITHUB_TOKEN: ghp_123456789012345678901234567890123456")
    findings = scan_directory(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "github_token"


def test_secret_scan_detects_private_key(tmp_path: Path):
    bad_file = tmp_path / "id_rsa"
    bad_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----")
    findings = scan_directory(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "private_key"


def test_secret_scan_literal_env_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-super-secret-key-123456789")
    bad_file = tmp_path / "index.js"
    bad_file.write_text("const key = 'real-super-secret-key-123456789';")
    findings = scan_directory(tmp_path)
    assert any(f.rule == "literal_env_secret" for f in findings)


def test_secret_scan_skips_git_and_venv(tmp_path: Path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("token = ghp_123456789012345678901234567890123456")
    findings = scan_directory(tmp_path)
    assert findings == []
