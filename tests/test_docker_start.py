from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spectra import docker_start


def test_main_passes_port_to_docker_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        calls.append((cmd, str(cwd) if cwd else None, env))

    opened_urls: list[str] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docker_start, "_run", fake_run)
    monkeypatch.setattr(docker_start, "_maybe_update_repo", lambda _repo_root: False)
    monkeypatch.setattr(docker_start.time, "sleep", lambda _: None)
    monkeypatch.setattr(docker_start.webbrowser, "open", opened_urls.append)
    monkeypatch.setattr(
        docker_start.sys,
        "argv",
        ["spectra-start", "--port", "3000"],
    )

    docker_start.main()

    assert calls[0] == (["docker", "info"], None, None)
    assert calls[1][0] == ["docker", "compose", "up", "-d", "--build"]
    assert calls[1][1] == str(tmp_path)
    assert calls[1][2] is not None
    assert calls[1][2]["SPECTRA_PORT"] == "3000"
    assert opened_urls == ["http://localhost:3000"]


def test_main_requires_compose_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docker_start.sys, "argv", ["spectra-start"])

    with pytest.raises(SystemExit) as exc:
        docker_start.main()

    assert exc.value.code == 1


def test_main_exits_with_docker_compose_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        if cmd[:3] == ["docker", "compose", "up"]:
            raise subprocess.CalledProcessError(returncode=7, cmd=cmd)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docker_start, "_run", fake_run)
    monkeypatch.setattr(docker_start, "_maybe_update_repo", lambda _repo_root: False)
    monkeypatch.setattr(
        docker_start.sys,
        "argv",
        ["spectra-start", "--no-open"],
    )

    with pytest.raises(SystemExit) as exc:
        docker_start.main()

    assert exc.value.code == 7


def test_main_builds_when_auto_update_pulls_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    calls: list[tuple[list[str], str | None, dict[str, str] | None]] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> None:
        calls.append((cmd, str(cwd) if cwd else None, env))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(docker_start, "_run", fake_run)
    monkeypatch.setattr(docker_start, "_maybe_update_repo", lambda _repo_root: True)
    monkeypatch.setattr(docker_start.time, "sleep", lambda _: None)
    monkeypatch.setattr(docker_start.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(docker_start.sys, "argv", ["spectra-start", "--no-build", "--no-open"])

    docker_start.main()

    assert calls[1][0] == ["docker", "compose", "up", "-d", "--build"]


def test_maybe_update_repo_pulls_when_branch_is_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(docker_start.shutil, "which", lambda _name: "/usr/bin/git")

    outputs = {
        ("git", "rev-parse", "--is-inside-work-tree"): subprocess.CompletedProcess([], 0, stdout="true\n", stderr=""),
        ("git", "diff", "--quiet", "--ignore-submodules", "--"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("git", "diff", "--cached", "--quiet", "--ignore-submodules", "--"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("git", "branch", "--show-current"): subprocess.CompletedProcess([], 0, stdout="main\n", stderr=""),
        ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"): subprocess.CompletedProcess([], 0, stdout="origin/main\n", stderr=""),
        ("git", "fetch", "--quiet", "origin"): subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"): subprocess.CompletedProcess([], 0, stdout="0 2\n", stderr=""),
    }

    monkeypatch.setattr(
        docker_start,
        "_run_capture",
        lambda cmd, cwd=None: outputs[tuple(cmd)],
    )

    run_calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
        run_calls.append(cmd)

    monkeypatch.setattr(docker_start, "_run", fake_run)

    assert docker_start._maybe_update_repo(tmp_path) is True
    assert run_calls == [["git", "pull", "--ff-only"]]


def test_maybe_update_repo_skips_when_worktree_is_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(docker_start.shutil, "which", lambda _name: "/usr/bin/git")

    def fake_capture(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd[:4] == ["git", "diff", "--quiet", "--ignore-submodules"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_start, "_run_capture", fake_capture)
    monkeypatch.setattr(docker_start, "_run", lambda *args, **kwargs: None)

    assert docker_start._maybe_update_repo(tmp_path) is False
    assert "Skipping auto-update" in capsys.readouterr().out
