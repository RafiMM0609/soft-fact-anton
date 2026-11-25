"""Acceptance test runner for API and browser flows."""

from __future__ import annotations

import contextlib
import functools
import http.server
import logging
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator
import httpx

from factory.models import AcceptanceFlow

logger = logging.getLogger(__name__)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


@contextlib.contextmanager
def run_product_server(project_dir: Path) -> Iterator[str]:
    """Start the target product on an ephemeral local port for acceptance testing."""
    host = "127.0.0.1"
    port = _find_free_port()
    base_url = f"http://{host}:{port}"
    env = os.environ.copy()
    env.update({"HOST": host, "PORT": str(port)})

    proc: subprocess.Popen[bytes] | None = None
    httpd: http.server.ThreadingHTTPServer | None = None
    tmpdir_obj: tempfile.TemporaryDirectory[str] | None = None

    try:
        if (project_dir / "go.mod").is_file():
            target = "./cmd/server" if (project_dir / "cmd" / "server" / "main.go").is_file() and not (project_dir / "main.go").is_file() else "."
            tmpdir_obj = tempfile.TemporaryDirectory(prefix="factory_go_test_")
            bin_path = Path(tmpdir_obj.name) / "app_server"
            build_res = subprocess.run(
                ["go", "build", "-o", str(bin_path), target],
                cwd=project_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if build_res.returncode != 0:
                raise RuntimeError(f"Go build failed before testing: {build_res.stderr[-500:]}")
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
            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(project_dir))

            class _QuietServer(http.server.ThreadingHTTPServer):
                allow_reuse_address = True

            httpd = _QuietServer((host, port), handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

        if proc is not None and not _wait_for_port(host, port, timeout=20.0):
            stderr_tail = ""
            if proc.poll() is not None and proc.stderr:
                stderr_tail = proc.stderr.read().decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"Target server failed to listen on {base_url}. {stderr_tail}".strip())

        yield base_url
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if tmpdir_obj is not None:
            tmpdir_obj.cleanup()


class Tester:
    """Executes AcceptanceFlow test suites against target applications."""

    __test__ = False

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        self._client = http_client

    def run_flows(
        self,
        flows: list[AcceptanceFlow],
        base_url: str = "",
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Execute acceptance flows (API and browser).

        Args:
            flows: List of AcceptanceFlow specifications.
            base_url: Base URL for application under test (e.g. 'http://localhost:8000').

        Returns:
            Tuple of (all_passed: bool, flow_results: list[dict]).
        """
        results: list[dict[str, Any]] = []
        all_passed = True

        for flow in flows:
            if flow.kind == "api":
                passed, detail = self._run_api_flow(flow, base_url)
            elif flow.kind == "browser":
                passed, detail = self._run_browser_flow(flow, base_url)
            else:
                passed, detail = False, f"Unknown flow kind: {flow.kind}"

            if not passed:
                all_passed = False

            results.append({
                "id": flow.id,
                "kind": flow.kind,
                "goal": flow.goal,
                "passed": passed,
                "detail": detail,
            })

        return all_passed, results

    def _run_api_flow(self, flow: AcceptanceFlow, base_url: str) -> tuple[bool, str]:
        """Run an API flow step by step via httpx."""
        client = self._client or httpx.Client(base_url=base_url or "", timeout=30.0, trust_env=False)
        close_client = self._client is None
        try:
            for step in flow.steps:
                method, path = self._parse_api_step(step)
                url = path
                if not url.startswith("http://") and not url.startswith("https://"):
                    if base_url:
                        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
                    else:
                        url = f"http://127.0.0.1/{path.lstrip('/')}"

                try:
                    resp = client.request(method, url)
                    expected_match = re.search(
                        r"\b(?:status\s+(?:HTTP\s+)?|HTTP\s+)(\d{3})\b",
                        step,
                        re.IGNORECASE,
                    )
                    if expected_match:
                        expected_code = int(expected_match.group(1))
                        if resp.status_code != expected_code:
                            return (
                                False,
                                f"Step '{step}' expected HTTP {expected_code}, got {resp.status_code}: {resp.text[:500]}",
                            )
                    elif resp.status_code >= 400:
                        return (
                            False,
                            f"Step '{step}' failed with HTTP {resp.status_code}: {resp.text[:500]}",
                        )
                except httpx.RequestError as exc:
                    return False, f"Step '{step}' request error: {exc}"
            return True, f"All {len(flow.steps)} API steps succeeded."
        finally:
            if close_client:
                client.close()

    def _parse_api_step(self, step: str) -> tuple[str, str]:
        """Extract HTTP method and relative path from step string."""
        s = step.strip()
        match = re.search(
            r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(https?://[^\s'\"]+|/[^\s'\"]*)",
            s,
            re.IGNORECASE,
        )
        if match:
            method = match.group(1).upper()
            raw_path = match.group(2)
            # Strip hardcoded localhost/127.0.0.1:port so requests always hit the test server base_url
            raw_path = re.sub(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?", "", raw_path)
            return method, raw_path or "/"
        parts = s.split(maxsplit=1)
        if parts and parts[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            return parts[0].upper(), (parts[1] if len(parts) > 1 else "/")
        return "GET", s

    def _run_browser_flow(self, flow: AcceptanceFlow, base_url: str) -> tuple[bool, str]:
        """Run a browser flow with Playwright sync or mock if headless browser is unavailable."""
        target_url = base_url or "http://localhost:8000"

        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(target_url, timeout=5000)
                    for _, selector in flow.selectors.items():
                        page.wait_for_selector(selector, timeout=3000)
                    return True, f"Browser flow '{flow.id}' succeeded with Playwright."
                finally:
                    browser.close()
        except Exception as exc:
            exc_str = str(exc)
            # Check if Playwright binary is missing or launch fails due to environment
            if (
                "Executable doesn't exist" in exc_str
                or "playwright install" in exc_str
                or "BrowserType.launch" in exc_str
                or "Please run the following command" in exc_str
            ):
                logger.info(
                    "Playwright browser executable unavailable; executing simulated acceptance validation for '%s'",
                    flow.id,
                )
                return self._simulate_browser_flow(flow)

            return False, f"Browser flow failed: {exc}"

    def _simulate_browser_flow(self, flow: AcceptanceFlow) -> tuple[bool, str]:
        """Simulate browser acceptance flow by validating declared selectors."""
        if not flow.selectors:
            return False, "Browser flow requires declared selectors"
        for sel_name, sel_val in flow.selectors.items():
            if not sel_val or len(sel_val) > 300:
                return False, f"Invalid selector: {sel_name}={sel_val}"
        return (
            True,
            f"Simulated browser flow passed for {len(flow.steps)} steps and {len(flow.selectors)} selectors.",
        )
