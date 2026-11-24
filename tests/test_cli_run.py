import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from factory.cli import main, run_command


def test_cli_run_missing_project(tmp_path: Path, capsys: pytest.CaptureFixture):
    ret = run_command("non-existent-product", port=8991, data_path=tmp_path)
    assert ret == 1
    captured = capsys.readouterr()
    assert "Product directory not found" in captured.err


def test_cli_run_static_serve(tmp_path: Path):
    # Setup dummy static product
    products_dir = tmp_path / "products"
    prod_dir = products_dir / "my-static-site"
    prod_dir.mkdir(parents=True)
    html_file = prod_dir / "index.html"
    html_file.write_text("<h1>Hello from ACP Factory</h1>", encoding="utf-8")

    port = 8997
    host = "127.0.0.1"
    stop_event = threading.Event()

    server_thread = threading.Thread(
        target=lambda: run_command(
            "my-static-site",
            port=port,
            host=host,
            data_path=tmp_path,
            stop_event=stop_event,
        ),
        daemon=True,
    )
    server_thread.start()

    time.sleep(0.3)
    try:
        # Avoid local proxy routing for loopback
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/") as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "Hello from ACP Factory" in content
    finally:
        stop_event.set()
        server_thread.join(timeout=2.0)


def test_cli_main_run_parser():
    with patch("factory.cli.run_command") as mock_run:
        mock_run.return_value = 0
        ret = main(["run", "kopi-senja", "--port", "8321", "--host", "127.0.0.1"])
        assert ret == 0
        mock_run.assert_called_once_with("kopi-senja", 8321, "127.0.0.1", None)
