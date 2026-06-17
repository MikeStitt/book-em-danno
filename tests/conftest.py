from __future__ import annotations

from pathlib import Path

from book_em_danno.core.exec import Runner


class RecordingRunner(Runner):
    """A Runner that records advised/run commands instead of executing, for
    asserting exact command construction and ordering without a Docker/Ollama
    daemon. Records both `advise` (gated) and `run` (always-execute) so launch and
    shell are captured rather than shelling out."""

    def __init__(self) -> None:
        super().__init__(apply=False, verbose=False)
        self.commands: list[list[str]] = []

    def advise(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        self.commands.append(cmd)
        return cmd

    def run(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> list[str]:
        self.commands.append(cmd)
        return cmd

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.commands]
