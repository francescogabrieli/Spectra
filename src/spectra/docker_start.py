"""Helper command to start Spectra via Docker and open the app in the browser."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, check=True, cwd=str(cwd) if cwd else None, env=env)


def _run_capture(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


def _maybe_update_repo(repo_root: Path) -> bool:
    if os.environ.get("SPECTRA_AUTO_UPDATE", "1") == "0":
        return False

    if shutil.which("git") is None:
        return False

    if _run_capture(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_root).returncode != 0:
        return False

    if _run_capture(["git", "diff", "--quiet", "--ignore-submodules", "--"], cwd=repo_root).returncode != 0:
        print("Local git changes detected. Skipping auto-update.")
        return False

    if _run_capture(["git", "diff", "--cached", "--quiet", "--ignore-submodules", "--"], cwd=repo_root).returncode != 0:
        print("Local git changes detected. Skipping auto-update.")
        return False

    branch = _run_capture(["git", "branch", "--show-current"], cwd=repo_root).stdout.strip()
    if not branch:
        return False

    upstream = _run_capture(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=repo_root,
    ).stdout.strip()
    if not upstream:
        return False

    remote = upstream.split("/", 1)[0]
    fetch = _run_capture(["git", "fetch", "--quiet", remote], cwd=repo_root)
    if fetch.returncode != 0:
        print(f"Could not reach {remote}. Skipping auto-update.")
        return False

    counts = _run_capture(["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream}"], cwd=repo_root)
    if counts.returncode != 0:
        return False

    ahead_str, behind_str = (counts.stdout.strip().split() + ["0", "0"])[:2]
    ahead = int(ahead_str)
    behind = int(behind_str)

    if ahead > 0 and behind > 0:
        print(f"Local branch has diverged from {upstream}. Skipping auto-update.")
        return False

    if behind <= 0:
        return False

    print(f"Updating Spectra from {upstream}...")
    try:
        _run(["git", "pull", "--ff-only"], cwd=repo_root)
    except subprocess.CalledProcessError:
        print("Auto-update failed. Continuing with local checkout.")
        return False

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="spectra-start",
        description="Start Spectra with Docker Compose and open the app in your browser.",
    )
    parser.add_argument("--port", type=int, default=8080, help="Local port (default: 8080)")
    parser.add_argument("--no-build", action="store_true", help="Skip image build")
    parser.add_argument("--no-open", action="store_true", help="Do not open browser automatically")
    parser.add_argument("--no-update", action="store_true", help="Skip git auto-update before start")
    args = parser.parse_args()

    repo_root = Path.cwd()
    compose_file = repo_root / "docker-compose.yml"
    if not compose_file.exists():
        print("Error: docker-compose.yml not found. Run this command from the project root.", file=sys.stderr)
        sys.exit(1)

    try:
        _run(["docker", "info"])
    except Exception:
        print(
            "Error: Docker daemon is not running.\n"
            "Start Docker Desktop (or Docker Engine) and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    auto_updated = False if args.no_update else _maybe_update_repo(repo_root)

    cmd = ["docker", "compose", "up", "-d"]
    if not args.no_build or auto_updated:
        cmd.append("--build")
    env = os.environ.copy()
    env["SPECTRA_PORT"] = str(args.port)

    try:
        _run(cmd, cwd=repo_root, env=env)
    except subprocess.CalledProcessError as exc:
        print(f"Error: failed to start Docker Compose (exit code {exc.returncode}).", file=sys.stderr)
        sys.exit(exc.returncode)

    url = f"http://localhost:{args.port}"
    print(f"Spectra is starting on {url}")

    if not args.no_open:
        # Small delay so the browser opens after container boot starts.
        time.sleep(1)
        webbrowser.open(url)
        print("Browser opened.")


if __name__ == "__main__":
    main()
