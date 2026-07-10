"""Backend selection + argv mapping for the sandbox CLI seam (`sbx` vs legacy
`docker sandbox`). The autouse conftest fixture pins `DANNO_SANDBOX_CLI=docker`,
so each sbx/auto-detect case overrides the env explicitly."""

from __future__ import annotations

import pytest

from book_em_danno.commands import sandbox as sb
from book_em_danno.commands import sandbox_cli
from conftest import RecordingRunner


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


def test_policy_allow_sbx_allows_only_given_hosts_never_star(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    # Security contract: allow ONLY the enumerated host(s), verbatim. NEVER "**".
    argv = sandbox_cli.policy_allow_argv("danno-app", ("10.0.1.9:11434",))
    assert argv == [
        "sbx",
        "policy",
        "allow",
        "network",
        "--sandbox",
        "danno-app",
        "10.0.1.9:11434",
    ]
    assert "**" not in argv  # would expose host + LAN + cloud metadata


def test_policy_allow_sbx_multiple_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    argv = sandbox_cli.policy_allow_argv("danno-app", ("10.0.1.9:11434", "127.0.0.1:9000"))
    assert argv[-1] == "10.0.1.9:11434,127.0.0.1:9000"


def test_configure_proxy_sbx_allows_ollama_ip_from_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    runner = RecordingRunner()
    sb.configure_proxy(runner, "danno-app", ollama_url="http://10.0.1.9:11434/v1")
    # the docker-proxy localhost token is replaced by the real routable endpoint
    assert runner.commands == [
        ["sbx", "policy", "allow", "network", "--sandbox", "danno-app", "10.0.1.9:11434"]
    ]


def test_configure_proxy_sbx_warns_when_ollama_unroutable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    warnings: list[str] = []
    monkeypatch.setattr(sb, "log_warn", lambda msg: warnings.append(msg))
    runner = RecordingRunner()
    sb.configure_proxy(runner, "danno-app")  # default ollama_url = host.docker.internal
    assert warnings and "host.docker.internal" in warnings[0]


def test_configure_proxy_docker_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    runner = RecordingRunner()
    sb.configure_proxy(runner, "danno-app")
    assert runner.commands == [
        [
            "docker",
            "sandbox",
            "network",
            "proxy",
            "danno-app",
            "--policy",
            "allow",
            "--allow-host",
            "localhost:11434",
        ]
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


def test_rm_forces_on_sbx_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # sbx rm aborts on a non-tty without --force; docker sandbox rm takes no force flag.
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    assert sandbox_cli.rm_argv("N") == ["sbx", "rm", "--force", "N"]
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    assert sandbox_cli.rm_argv("N") == ["docker", "sandbox", "rm", "N"]


def test_policy_init_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    assert sandbox_cli.policy_init_argv() == ["sbx", "policy", "init", "balanced"]
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    assert sandbox_cli.policy_init_argv() is None


def test_ensure_policy_initialized_docker_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    runner = RecordingRunner()
    sb.ensure_policy_initialized(runner)
    assert runner.commands == []


def test_ensure_policy_initialized_sbx_inits_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    monkeypatch.setattr(sb, "_sbx_policy_initialized", lambda: False)
    runner = RecordingRunner()
    sb.ensure_policy_initialized(runner)
    assert runner.commands == [["sbx", "policy", "init", "balanced"]]


def test_ensure_policy_initialized_sbx_skips_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")
    monkeypatch.setattr(sb, "_sbx_policy_initialized", lambda: True)
    runner = RecordingRunner()
    sb.ensure_policy_initialized(runner)
    assert runner.commands == []
