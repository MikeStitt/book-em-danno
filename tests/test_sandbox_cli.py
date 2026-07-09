"""Backend selection + argv mapping for the sandbox CLI seam (`sbx` vs legacy
`docker sandbox`). The autouse conftest fixture pins `DANNO_SANDBOX_CLI=docker`,
so each sbx/auto-detect case overrides the env explicitly."""

from __future__ import annotations

import pytest

from book_em_danno.commands import sandbox_cli


def test_env_override_selects_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    assert sandbox_cli.resolve_backend() == "sbx"
    assert sandbox_cli.base() == ["sbx"]
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    assert sandbox_cli.resolve_backend() == "docker"
    assert sandbox_cli.base() == ["docker", "sandbox"]


def test_invalid_override_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "podman")
    with pytest.raises(ValueError, match="invalid"):
        sandbox_cli.resolve_backend()


def test_auto_detect_prefers_sbx_when_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANNO_SANDBOX_CLI", raising=False)
    monkeypatch.setattr(sandbox_cli.shutil, "which", lambda name: "/usr/local/bin/sbx")
    assert sandbox_cli.resolve_backend() == "sbx"


def test_auto_detect_falls_back_to_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANNO_SANDBOX_CLI", raising=False)
    monkeypatch.setattr(sandbox_cli.shutil, "which", lambda name: None)
    assert sandbox_cli.resolve_backend() == "docker"


def test_availability_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    assert sandbox_cli.availability_argv() == ["sbx", "version"]
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    assert sandbox_cli.availability_argv() == ["docker", "sandbox", "version"]


def test_policy_allow_sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    # sbx allows all egress for the sandbox; the enumerated hosts are subsumed.
    assert sandbox_cli.policy_allow_argv("danno-app", ("localhost:11434",)) == [
        "sbx",
        "policy",
        "allow",
        "network",
        "--sandbox",
        "danno-app",
        "**",
    ]


def test_policy_allow_docker_keeps_allow_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    assert sandbox_cli.policy_allow_argv("danno-app", ("localhost:11434", "10.0.1.27:11434")) == [
        "docker",
        "sandbox",
        "network",
        "proxy",
        "danno-app",
        "--policy",
        "allow",
        "--allow-host",
        "localhost:11434",
        "--allow-host",
        "10.0.1.27:11434",
    ]
