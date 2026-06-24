"""SWE-bench Verified suite: real GitHub-issue instances as per-instance `BenchTask`s.

Instance metadata is fetched HOST-SIDE from the HuggingFace datasets-server REST API
(no `datasets` dependency, no in-VM HF egress) and filtered to the configured
`select` instance ids. Each `SwebenchTask` then, IN the sandbox: clones the repo at
`base_commit`, applies the instance's `test_patch` (which adds the grading tests),
installs the repo's deps (pip in the proxy-only VM — see the M0 spike), and grades by
running the instance's `FAIL_TO_PASS` + `PASS_TO_PASS` pytest node ids.

We run real SWE-bench Verified *instances* via danno's own execution model — NOT the
official Docker-per-task harness, so this is never an official "SWE-bench score". A
`select`ed instance whose deps don't install in the VM errors loudly in its own row.
"""

from __future__ import annotations

import json
import shlex
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from book_em_danno.core.exec import Runner
from danno_validator.driver import capture_exec

# HuggingFace datasets-server rows endpoint (host-side; the host has normal egress).
_ROWS_API = "https://datasets-server.huggingface.co/rows"
_PAGE = 100  # max rows per request
_TOTAL = 500  # SWE-bench_Verified size; paged to find the select ids
# The patch file the test_patch is written to inside each instance checkout.
_TEST_PATCH_FILE = ".danno_test.patch"


def _as_list(value: object) -> list[str]:
    """FAIL_TO_PASS / PASS_TO_PASS come as a JSON-encoded list (or already a list)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]
    return []


def fetch_instances(select: Sequence[str], *, dataset: str) -> dict[str, dict]:
    """Fetch the `select` instances' rows from the HF datasets-server (host-side).

    Pages through the dataset's `test` split collecting rows whose `instance_id` is
    in `select`, stopping once all are found. Fails loud (ValueError) if any
    requested id is absent. Returns instance_id -> row dict.
    """
    wanted = set(select)
    found: dict[str, dict] = {}
    offset = 0
    while offset < _TOTAL and len(found) < len(wanted):
        url = (
            f"{_ROWS_API}?dataset={urllib.parse.quote(dataset)}"
            f"&config=default&split=test&offset={offset}&length={_PAGE}"
        )
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 (trusted host)
            payload = json.load(resp)
        rows = payload.get("rows", [])
        if not rows:
            break
        for entry in rows:
            row = entry.get("row", {})
            iid = row.get("instance_id")
            if iid in wanted:
                found[iid] = row
        offset += _PAGE
    missing = wanted - found.keys()
    if missing:
        raise ValueError(f"swebench: instance id(s) not found in {dataset}: {sorted(missing)}")
    return found


@dataclass(frozen=True)
class SwebenchTask:
    """One SWE-bench Verified instance, mapped onto the `BenchTask` contract.

    Per-instance isolation: the repo is cloned + deps installed once (`provision`),
    then `reset` restores the worktree to base + test_patch between agent/model
    variants, and `grade` runs the instance's FAIL_TO_PASS + PASS_TO_PASS node ids.
    """

    instance_id: str
    repo: str  # "owner/name"
    base_commit: str
    problem_statement: str
    test_patch: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    deps: Literal["offline-wheel-cache", "no-cache-dir"] = "offline-wheel-cache"

    @property
    def id(self) -> str:
        return self.instance_id

    @property
    def prompt(self) -> str:
        return (
            f"You are working in a clone of the {self.repo} repository. Resolve this "
            f"issue by editing the project's source code (do NOT edit any test files):\n\n"
            f"{self.problem_statement}\n\n"
            "Make the necessary code changes so the project's tests pass."
        )

    @property
    def _subdir(self) -> str:
        return self.instance_id

    def workspace_dir(self, workspace: Path) -> Path:
        return workspace / self._subdir

    def _pip(self) -> str:
        # --no-build-isolation avoids the PEP 517 isolated-subprocess proxy timeout
        # (M0 spike); setuptools/wheel/pytest are pre-installed for that to work.
        base = "python3 -m pip install --break-system-packages --no-cache-dir"
        isolation = " --no-build-isolation" if self.deps == "offline-wheel-cache" else ""
        # `|| true`: a failed editable install still leaves a gradeable worktree (the
        # instance errors in its row at grade time rather than aborting provision).
        return f"{base} setuptools wheel pytest && {base}{isolation} -e . || true"

    def provision(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        """Clone repo@base_commit, apply the test patch, install deps (one-time)."""
        d = self.workspace_dir(workspace)
        d.mkdir(parents=True, exist_ok=True)
        (d / _TEST_PATCH_FILE).write_text(self.test_patch, encoding="utf-8")
        q = shlex.quote(str(d))
        url = f"https://github.com/{self.repo}.git"
        script = (
            f"set -e; cd {q}; "
            "if [ ! -d .git ]; then "
            f"git clone {shlex.quote(url)} .; fi; "
            f"git fetch --depth 1 origin {shlex.quote(self.base_commit)} || git fetch origin; "
            f"git checkout -f {shlex.quote(self.base_commit)}; "
            f"git apply {_TEST_PATCH_FILE}; "
            f"{self._pip()}"
        )
        capture_exec(runner, sandbox, script, check=True)

    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        """Restore the worktree to base_commit + test_patch (drop the agent's edits)."""
        q = shlex.quote(str(self.workspace_dir(workspace)))
        script = (
            f"set -e; cd {q}; git checkout -f {shlex.quote(self.base_commit)}; "
            f"git clean -fd -e {_TEST_PATCH_FILE}; git apply {_TEST_PATCH_FILE}"
        )
        capture_exec(runner, sandbox, script, check=True)

    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> bool:
        """True iff every FAIL_TO_PASS + PASS_TO_PASS node id passes (exit 0)."""
        nodes = (*self.fail_to_pass, *self.pass_to_pass)
        if not nodes:
            return False
        q = shlex.quote(str(self.workspace_dir(workspace)))
        ids = " ".join(map(shlex.quote, nodes))
        cmd = f"cd {q} && python3 -m pytest -q -p no:cacheprovider {ids}"
        return capture_exec(runner, sandbox, cmd, check=False).ok


def task_from_row(row: dict, *, deps: str = "offline-wheel-cache") -> SwebenchTask:
    """Build a `SwebenchTask` from a datasets-server row dict."""
    return SwebenchTask(
        instance_id=str(row["instance_id"]),
        repo=str(row["repo"]),
        base_commit=str(row["base_commit"]),
        problem_statement=str(row.get("problem_statement", "")),
        test_patch=str(row.get("test_patch", "")),
        fail_to_pass=tuple(_as_list(row.get("FAIL_TO_PASS"))),
        pass_to_pass=tuple(_as_list(row.get("PASS_TO_PASS"))),
        deps=deps,  # type: ignore[arg-type]
    )


def load_swebench_tasks(
    select: Sequence[str], *, dataset: str, deps: str = "offline-wheel-cache"
) -> list[SwebenchTask]:
    """Fetch + build the selected SWE-bench tasks, in `select` order."""
    rows = fetch_instances(select, dataset=dataset)
    return [task_from_row(rows[iid], deps=deps) for iid in select]
