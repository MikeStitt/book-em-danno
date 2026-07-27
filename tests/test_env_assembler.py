"""Unit tests for the unified `[env]` assembler (Phase 1).

`sandbox.assemble_harness_env` folds four layers into one precedence-ordered env-file
line list (highest wins): CLI `--env`/`--env-file` > host `os.environ` > danno.toml
`[env]` literal > harness code default. These pin every layer boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import sandbox
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import CommandFailedError


def _cfg(env: dict[str, str] | None = None) -> DannoConfig:
    return DannoConfig(env=env or {})


def _as_dict(lines: list[str]) -> dict[str, str]:
    return dict(line.split("=", 1) for line in lines)


def test_harness_default_is_the_base_layer() -> None:
    # No [env], no CLI, no matching host var — the harness default stands.
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg(),
            harness_defaults=["OLLAMA_BASE_URL=http://h:11434/v1"],
            env_pairs=[],
            env_files=[],
        )
    )
    assert out == {"OLLAMA_BASE_URL": "http://h:11434/v1"}


def test_env_literal_overrides_harness_default() -> None:
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg({"CLAURST_REF": "v1.2.3"}),
            harness_defaults=["CLAURST_REF=main"],
            env_pairs=[],
            env_files=[],
        )
    )
    assert out["CLAURST_REF"] == "v1.2.3"


def test_host_env_overrides_env_literal_for_declared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An exported host var beats the committed [env] value — but ONLY because the key
    # is declared in [env] (the operator opted into managing it).
    monkeypatch.setenv("CLAURST_REF", "abc123")
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg({"CLAURST_REF": "v1.2.3"}), harness_defaults=[], env_pairs=[], env_files=[]
        )
    )
    assert out["CLAURST_REF"] == "abc123"


def test_cli_pair_overrides_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAURST_REF", "host-value")
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg({"CLAURST_REF": "toml-value"}),
            harness_defaults=["CLAURST_REF=default-value"],
            env_pairs=["CLAURST_REF=cli-value"],
            env_files=[],
        )
    )
    assert out["CLAURST_REF"] == "cli-value"


def test_bare_host_var_does_not_clobber_harness_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The footgun guard: an operator's shell OLLAMA_BASE_URL must NOT silently replace
    # danno's computed sandbox-networking default (the key is not in [env] or CLI).
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg(),
            harness_defaults=["OLLAMA_BASE_URL=http://host.docker.internal:11434/v1"],
            env_pairs=[],
            env_files=[],
        )
    )
    assert out["OLLAMA_BASE_URL"] == "http://host.docker.internal:11434/v1"


def test_env_indirection_resolves_from_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg({"PROVIDER_KEY": "{env:NVIDIA_API_KEY}"}),
            harness_defaults=[],
            env_pairs=[],
            env_files=[],
        )
    )
    assert out["PROVIDER_KEY"] == "nvapi-secret"


def test_missing_indirection_warns_and_drops_in_non_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOT_SET", raising=False)
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg({"PROVIDER_KEY": "{env:NOT_SET}"}),
            harness_defaults=["KEEP=me"],
            env_pairs=[],
            env_files=[],
        )
    )
    assert out == {"KEEP": "me"}  # PROVIDER_KEY dropped, no raise


def test_missing_indirection_raises_in_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOT_SET", raising=False)
    with pytest.raises(CommandFailedError, match="NOT_SET"):
        sandbox.assemble_harness_env(
            _cfg({"PROVIDER_KEY": "{env:NOT_SET}"}),
            harness_defaults=[],
            env_pairs=[],
            env_files=[],
            strict=True,
        )


def test_none_config_is_defaults_plus_cli() -> None:
    # config-less sandbox shell/start on a bare dir: no [env] overlay, CLI still applies.
    out = _as_dict(
        sandbox.assemble_harness_env(
            None,
            harness_defaults=["OLLAMA_BASE_URL=http://h:11434/v1"],
            env_pairs=["EXTRA=1"],
            env_files=[],
        )
    )
    assert out == {"OLLAMA_BASE_URL": "http://h:11434/v1", "EXTRA": "1"}


def test_env_file_layer_below_cli_pair(tmp_path: Path) -> None:
    # --env-file and --env pairs are both the CLI tier; a pair wins over a file for the
    # same key (mirrors _provided_env's later-wins ordering).
    ef = tmp_path / "creds.env"
    ef.write_text("SHARED=from-file\nONLY_FILE=f\n", encoding="utf-8")
    out = _as_dict(
        sandbox.assemble_harness_env(
            _cfg(),
            harness_defaults=[],
            env_pairs=["SHARED=from-pair"],
            env_files=[str(ef)],
        )
    )
    assert out == {"SHARED": "from-pair", "ONLY_FILE": "f"}
