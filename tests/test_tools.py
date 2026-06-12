from __future__ import annotations

from pathlib import Path

from book_em_danno.commands import tools
from book_em_danno.config.schema import Tool
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
