"""Unit tests for the Phase-2 occ AUT seam — `install_occ` command construction
(clone/checkout/npm-install/stamp; no source patch or shim — the fork carries those
natively), `interactive_launch_script` (local relay vs shimless cloud), `occ_repo_ref`
pin precedence, and `authed_occ_run` forwarding. No Docker daemon: the install is asserted
via the advised (non-apply) command, and the turn producer is checked by stubbing
`driver.occ_run`."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator import driver, occ


def _cfg(env: dict[str, str] | None = None) -> DannoConfig:
    return DannoConfig(env=env or {})


# ── install_occ ─────────────────────────────────────────────────────────────────────


def test_install_occ_clones_checks_out_and_stamps() -> None:
    cmd = occ.install_occ(Runner(), "box")
    assert cmd[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    script = cmd[6]
    # full clone (no --depth: OCC_REF may be an arbitrary commit) + checkout of the pin
    assert f'git clone "{occ.OCC_REPO_DEFAULT}"' in script
    assert f'git -C "{occ.OCC_CLONE_DIR}" checkout "{occ.OCC_REF_DEFAULT}"' in script
    # skip-guard on the danno stamp = repo@ref AND entrypoint present
    stamp_val = f"{occ.OCC_REPO_DEFAULT}@{occ.OCC_REF_DEFAULT}"
    assert f'= "{stamp_val}" ]' in script
    assert f'printf %s "{stamp_val}" > "$stamp"' in script
    assert occ.OCC_VERSION_STAMP in script
    assert f'[ -f "{driver.OCC_ENTRY}" ]' in script


def test_install_occ_does_not_patch_source() -> None:
    # The fork routes detectProvider on OPENAI_BASE_URL natively — install must NOT sed the
    # source or write a proxy shim (both moved into the fork; see its ADR-004).
    script = occ.install_occ(Runner(), "box")[6]
    assert "sed -i" not in script
    assert "refusing to patch" not in script
    assert "NODE_OPTIONS" not in script
    assert "setGlobalDispatcher" not in script
    assert "EnvHttpProxyAgent" not in script


def test_install_occ_installs_deps_including_undici() -> None:
    # The fork declares undici as a dependency (its global dispatcher needs it); install runs
    # `npm install` in the v2 workspace, plus an explicit undici install as belt-and-suspenders.
    script = occ.install_occ(Runner(), "box")[6]
    assert f'npm --prefix "{occ.OCC_CLONE_DIR}/v2" install' in script
    assert f'npm --prefix "{occ.OCC_CLONE_DIR}/v2" install undici' in script


def test_install_occ_requires_node_and_npm() -> None:
    script = occ.install_occ(Runner(), "box")[6]
    assert "command -v node" in script
    assert "command -v npm" in script


def test_install_occ_honors_repo_ref_pin() -> None:
    cfg = _cfg({"OCC_REPO": "https://example.test/fork", "OCC_REF": "v9"})
    cmd = occ.install_occ(Runner(), "box", cfg)
    script = cmd[6]
    assert 'git clone "https://example.test/fork"' in script
    assert 'checkout "v9"' in script
    assert 'printf %s "https://example.test/fork@v9"' in script


# ── occ_repo_ref precedence ─────────────────────────────────────────────────────────


def test_occ_repo_ref_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCC_REPO", raising=False)
    monkeypatch.delenv("OCC_REF", raising=False)
    assert occ.occ_repo_ref(None) == (occ.OCC_REPO_DEFAULT, occ.OCC_REF_DEFAULT)


def test_occ_repo_ref_toml_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCC_REF", raising=False)
    assert occ.occ_repo_ref(_cfg({"OCC_REF": "v1.2.3"}))[1] == "v1.2.3"


def test_occ_repo_ref_host_env_over_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    # exported host var MUST win over the committed [env] value (user-requested flow).
    monkeypatch.setenv("OCC_REF", "from-host")
    assert occ.occ_repo_ref(_cfg({"OCC_REF": "from-toml"}))[1] == "from-host"


def test_occ_repo_ref_env_indirection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCC_REF", raising=False)
    monkeypatch.setenv("MY_PIN", "abc123")
    assert occ.occ_repo_ref(_cfg({"OCC_REF": "{env:MY_PIN}"}))[1] == "abc123"


def test_occ_repo_ref_missing_indirection_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCC_REF", raising=False)
    monkeypatch.delenv("MISSING_PIN", raising=False)
    with pytest.raises(CommandFailedError):
        occ.occ_repo_ref(_cfg({"OCC_REF": "{env:MISSING_PIN}"}))


# ── interactive_launch_script ───────────────────────────────────────────────────────


def test_interactive_launch_local_relay_and_openai_env() -> None:
    argv = occ.interactive_launch_script("ollama/gemma4", [])
    assert argv[:2] == ["bash", "-lc"]
    script = argv[2]
    assert "-m gemma4" in script  # bare tag (no ollama/ prefix)
    assert "ollama/gemma4" not in script
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434/v1" in script
    assert "OPENAI_API_KEY=dummy" in script
    assert "CLAUDE_CODE_STREAMING=0" in script
    assert "DANNO_RELAY_UPSTREAM_PORT=11434 " in script  # relay to real Ollama
    assert "-p" not in script.split()  # TUI: no headless print flag


def test_interactive_launch_local_capture_port_redirects_relay() -> None:
    script = occ.interactive_launch_script("ollama/x", ["--foo"], capture_port=40404)[2]
    assert "DANNO_RELAY_UPSTREAM_PORT=40404 " in script
    assert "--foo" in script  # passthru preserved


def test_interactive_launch_cloud_no_shim_no_relay() -> None:
    script = occ.interactive_launch_script("nvidia/qwen/q3", [])[2]
    # The fork's global dispatcher reads HTTPS_PROXY from the env-file — no NODE_OPTIONS shim.
    assert "NODE_OPTIONS" not in script
    assert "CLAUDE_CODE_STREAMING=0" in script
    assert "-m qwen/q3" in script  # backend prefix stripped
    assert "ThreadingHTTPServer" not in script  # no relay
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434" not in script


# ── authed_occ_run forwarding ───────────────────────────────────────────────────────


def test_authed_occ_run_binds_env_file_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_occ_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        captured.update(kw)
        captured["name"] = name
        captured["prompt"] = prompt
        return object()

    monkeypatch.setattr(occ, "occ_run", fake_occ_run)
    run = occ.authed_occ_run(Path("/tmp/danno-env-xyz"))
    run(Runner(), "box", "do it", model="ollama/gemma4", workspace="/ws")
    assert captured["env_file"] == Path("/tmp/danno-env-xyz")
    assert captured["model"] == "ollama/gemma4"
    assert captured["workspace"] == "/ws"
    assert captured["name"] == "box"


def test_authed_occ_run_allows_none_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(occ, "occ_run", lambda r, n, p, **kw: captured.update(kw) or object())
    occ.authed_occ_run(None)(Runner(), "box", "hi")
    assert captured["env_file"] is None


def test_authed_occ_run_model_override_replaces_dial_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reported matrix ref (a `danno-ollama/…` backend name) would be misread as cloud
    # by occ_run's prefix check; model_override is what actually reaches occ_run's `-m`.
    captured: dict[str, object] = {}
    monkeypatch.setattr(occ, "occ_run", lambda r, n, p, **kw: captured.update(kw) or object())
    run = occ.authed_occ_run(None, model_override="ollama/qwen3-coder-next")
    run(Runner(), "box", "go", model="danno-ollama/qwen3-coder-next")
    assert captured["model"] == "ollama/qwen3-coder-next"


def test_authed_occ_run_no_override_keeps_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(occ, "occ_run", lambda r, n, p, **kw: captured.update(kw) or object())
    occ.authed_occ_run(None)(Runner(), "box", "go", model="ollama/gemma4")
    assert captured["model"] == "ollama/gemma4"
