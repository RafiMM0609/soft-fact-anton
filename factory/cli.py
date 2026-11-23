"""Command-line interface runner for ACP Software Factory."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import time

from factory.config import Settings
from factory.db import Store
from factory.engine import Engine

logger = logging.getLogger(__name__)


def doctor_command(settings: Settings) -> int:
    """Run diagnostics on environment, binaries, and host resources."""
    print("ACP Software Factory Doctor Diagnostics:")
    print("=" * 50)
    all_ok = True

    # 1. Python version check
    py_ver = sys.version_info
    py_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if (py_ver.major, py_ver.minor) >= (3, 12):
        print(f"[OK] Python {py_str} ({sys.executable})")
    else:
        print(f"[FAIL] Python {py_str} (>= 3.12 required)")
        all_ok = False

    # 2. SQLite version check
    sqlite_ver = sqlite3.sqlite_version
    print(f"[OK] SQLite {sqlite_ver}")

    # 3. Git binary check
    git_bin = shutil.which("git")
    if git_bin:
        import subprocess

        try:
            git_ver = (
                subprocess.run(
                    ["git", "--version"], capture_output=True, text=True, check=True
                )
                .stdout.strip()
            )
            print(f"[OK] Git ({git_bin}): {git_ver}")
        except Exception:
            print(f"[OK] Git binary found at: {git_bin}")
    else:
        print("[FAIL] Git binary not found in PATH")
        all_ok = False

    # 4. Harness CLI check (e.g. agy)
    agy_path = Path(settings.agy)
    if agy_path.is_file() and os.access(agy_path, os.X_OK):
        print(f"[OK] Harness binary ({settings.harness}): {agy_path}")
    elif shutil.which(settings.harness):
        which_path = shutil.which(settings.harness)
        print(f"[OK] Harness binary ({settings.harness}): {which_path}")
    else:
        print(f"[WARN] Harness binary not found at: {agy_path} (can be overridden via FACTORY_AGY)")

    # 5. Host Memory and OS resource check
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            meminfo: dict[str, int] = {}
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    if val.isdigit():
                        meminfo[key] = int(val)
        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", 0)
        total_mb = total_kb // 1024
        avail_mb = avail_kb // 1024
        print(f"[OK] Host Memory: {total_mb} MB total, {avail_mb} MB available")
    except Exception:
        print("[INFO] Host Memory check skipped (non-Linux or restricted /proc)")

    print("=" * 50)
    if all_ok:
        print("Doctor check passed. System is ready.")
        return 0
    else:
        print("Doctor check failed. Please resolve the issues above.")
        return 1


def init_command(data_path: Path) -> int:
    """Initialize data directory, sqlite database, and operator token."""
    resolved_data = data_path.resolve()
    resolved_data.mkdir(parents=True, exist_ok=True)
    (resolved_data / "products").mkdir(parents=True, exist_ok=True)

    # Initialize DB
    db_file = resolved_data / "factory.db"
    Store(db_file)

    # Operator token
    token_file = resolved_data / ".operator_token"
    if not token_file.is_file():
        token = secrets.token_hex(24)
        token_file.write_text(token, encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
        print(f"Generated new operator token at: {token_file}")
    else:
        token = token_file.read_text(encoding="utf-8").strip()

    print(f"Initialized ACP Software Factory repository at: {resolved_data}")
    print(f"Operator token: {token}")
    return 0


def build_command(
    brief: str,
    product: str | None,
    profile: str,
    data_path: Path | None,
) -> int:
    """Run an end-to-end engine build directly from CLI."""
    settings = Settings(data=data_path.resolve() if data_path else Path("./data").resolve())
    store = Store(settings.data / "factory.db")

    product_id = product or f"product-{secrets.token_hex(4)}"

    # Get or create project
    try:
        proj = store.project(product_id)
    except KeyError:
        with store.tx() as db:
            db.execute(
                "INSERT INTO projects(id, name, brief, profile, created) VALUES(?,?,?,?,?)",
                (product_id, product_id, brief, profile, time.time()),
            )
            store.event_tx(db, product_id, None, "project.created", {"name": product_id})
        proj = store.project(product_id)

    # Enqueue job
    job = store.enqueue(product_id, kind="build", payload={"brief": brief, "profile": profile})
    claimed = store.claim("build")
    active_job = claimed if claimed is not None else job

    engine = Engine(settings=settings, store=store)
    try:
        artifact = engine.build(project=proj, job=active_job)
        print(json.dumps(artifact, indent=2))
        return 0
    except Exception as exc:
        print(f"Error during factory build: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for the factory CLI."""
    parser = argparse.ArgumentParser(prog="factory", description="ACP Software Factory CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # doctor
    subparsers.add_parser("doctor", help="Check host binaries, memory, and runtime status")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize data directory and secrets")
    init_parser.add_argument(
        "--data",
        type=Path,
        default=Path("./data"),
        help="Path to factory data directory (default: ./data)",
    )

    # build
    build_parser = subparsers.add_parser("build", help="Run a product build")
    build_parser.add_argument("--brief", type=str, required=True, help="Product specification brief")
    build_parser.add_argument("--product", type=str, default=None, help="Product identifier")
    build_parser.add_argument("--profile", type=str, default="static", help="Product profile type")
    build_parser.add_argument("--data", type=Path, default=None, help="Path to data directory")

    args = parser.parse_args(argv)

    if args.subcommand == "doctor":
        return doctor_command(Settings())
    elif args.subcommand == "init":
        return init_command(args.data)
    elif args.subcommand == "build":
        return build_command(args.brief, args.product, args.profile, args.data)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
