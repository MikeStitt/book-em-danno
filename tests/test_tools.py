from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import tools
from book_em_danno.config.schema import Tool
from book_em_danno.core.exec import Runner
from conftest import RecordingRunner


def test_generic_git_clones_into_temp_dir(tmp_path: Path) -> None:
    # install_generic_git must clone into a fresh temp dir, never the CWD, so an
    # --apply run can't pollute the repo root.
    r = RecordingRunner()
    tool = Tool(name="some-tool", source="https://github.com/x/some-tool", install_to="sandbox")
    tools.install_generic_git(r, tool, tmp_path)
    (clone,) = r.commands
    assert clone[:2] == ["git", "clone"]
    assert clone[2] == tool.source
    dest = Path(clone[3])
    assert dest.name == "some-tool"
    assert dest.parent != Path.cwd()  # not the repo root
    assert not dest.is_relative_to(tmp_path)  # nor the target


def test_generic_git_under_apply_warns_only_cloned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Under --apply the caller expects the tool INSTALLED, but this fallback only clones and
    # leaves the installer to a manual step — a clean provision would overstate "ready", so it
    # must WARN the gap, not narrate it as INFO (policy §5, WARNING).
    r = RecordingRunner()
    r.apply = True  # exercise the --apply branch without executing (advise is recorded, not run)
    tool = Tool(name="some-tool", source="https://github.com/x/some-tool", install_to="sandbox")
    tools.install_generic_git(r, tool, tmp_path)
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "only cloned, not installed" in err


def test_generic_git_advise_mode_is_info_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # In advise mode nothing was promised to be installed, so the follow-up is INFO, not WARN.
    r = RecordingRunner()  # apply=False
    tool = Tool(name="some-tool", source="https://github.com/x/some-tool", install_to="sandbox")
    tools.install_generic_git(r, tool, tmp_path)
    assert "[WARNING]" not in capsys.readouterr().err


def test_install_ados_advises_with_cwd_and_env(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The ADOS --local step must carry cwd=target and ADOS_SOURCE_DIR so it runs
    # in the right place under --apply (the why= string promises this).
    ados = tmp_path / "ados"
    (ados / ".opencode" / "agent").mkdir(parents=True)
    (ados / "scripts").mkdir(parents=True)
    (ados / "scripts" / "install.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    target = tmp_path / "proj"
    target.mkdir()

    captured: dict[str, object] = {}

    def fake_advise(cmd, why, *, cwd=None, env=None):  # type: ignore[no-untyped-def]
        if cmd[:1] == ["bash"]:
            captured["cwd"] = cwd
            captured["env"] = env
        return cmd

    r = RecordingRunner()
    monkeypatch.setattr(r, "advise", fake_advise)
    tool = Tool(name="ados", source="https://example/ados", install_to="sandbox")
    tools.install_ados(r, tool, target, ados_repo=str(ados))

    assert captured["cwd"] == target
    assert isinstance(captured["env"], dict)
    assert captured["env"]["ADOS_SOURCE_DIR"] == str(ados.resolve())


def test_copy_md_dir_write_failure_fails_loud(tmp_path: Path) -> None:
    # A copy/mkdir OSError must surface as ToolInstallError (which install.py catches), not a
    # raw traceback that escapes its except set (policy §5, ERROR). Force it by making the
    # dest's parent a FILE so mkdir(parents=True) raises deterministically.
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("---\nmodel: x\n---\n", encoding="utf-8")
    (tmp_path / "afile").write_text("i am a file, not a dir", encoding="utf-8")
    dest = tmp_path / "afile" / "agent"
    with pytest.raises(tools.ToolInstallError, match="cannot copy ADOS agent defs"):
        tools._copy_md_dir(Runner(apply=True), src, dest, "agent")


def test_write_provenance_write_failure_fails_loud(tmp_path: Path) -> None:
    # Same contract for the durable provenance write.
    ados = tmp_path / "ados"
    ados.mkdir()
    # target_abs/.opencode can't be created because target_abs is a FILE.
    target_abs = tmp_path / "afile"
    target_abs.write_text("i am a file, not a dir", encoding="utf-8")
    with pytest.raises(tools.ToolInstallError, match="cannot write ADOS provenance"):
        tools._write_provenance(ados, target_abs)
