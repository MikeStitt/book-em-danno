"""The `danno` CLI. Commands — `install`, `doctor`, `sandbox`, `validate` — over the
two-mode automation policy: advise by default, execute under `--apply`. `install`
is the one provisioning path; `sandbox` operates the provisioned VM; `doctor` is a
read-only preflight; `validate` sweeps danno.toml's models through the tiered
battery (it runs immediately, like `sandbox start`). `--apply` is a per-command
option (`danno install --apply`)."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import typer

from .commands import doctor as doctor_cmd
from .commands import install as install_cmd
from .commands import sandbox as sandbox_cmd
from .config.loader import DannoConfigError, load_config
from .config.schema import DannoConfig
from .core import registry
from .core.exec import (
    CommandFailedError,
    CommandNotFoundError,
    Runner,
    console,
    log_err,
    log_warn,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Declarative, transparent setup of OpenCode in a Docker Sandbox, driven by danno.toml.",
)
sandbox_app = typer.Typer(no_args_is_help=True, help="Operate the provisioned Docker sandbox.")
app.add_typer(sandbox_app, name="sandbox")

# Per-command options shared across the side-effecting commands (mirrors _HARNESS_OPT).
_APPLY_OPT = typer.Option(
    False, "--apply", help="Execute host/Docker/Ollama commands instead of only printing them."
)
_VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug output.")
_CONFIG_OPT = typer.Option(Path("danno.toml"), "--config", help="Path to danno.toml.")


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        ver = pkg_version("danno")
    except PackageNotFoundError:
        ver = "unknown (dev)"
    console.print(f"danno {ver}")
    raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the danno version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    pass


def _load(config_path: Path) -> DannoConfig:
    try:
        return load_config(config_path)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc


def _guard(action: Callable[[], object]) -> None:
    """Run a Tier-2 action, turning a failed/missing external command into a clean
    exit 4 instead of a traceback."""
    try:
        action()
    except (CommandFailedError, CommandNotFoundError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def install(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    ados_repo: str = typer.Option(
        None, "--ados-repo", help="ADOS checkout to install from (else auto-detected)."
    ),
    config: Path = _CONFIG_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Provision a target project: config + Ollama models + tools + sandbox.

    Default prints the host/Docker commands to run yourself; `--apply` executes.
    Stops before the TUI — launch with `danno sandbox start`.
    """
    cfg = _load(config)
    runner = Runner(apply=apply, verbose=verbose)
    try:
        install_cmd.run_install(cfg, target, runner, ados_repo=ados_repo)
    except (install_cmd.InstallError, NotImplementedError, ValueError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandNotFoundError, CommandFailedError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def doctor() -> None:
    """Read-only preflight: report environment readiness with copy-paste fixes."""
    failed = doctor_cmd.run_doctor()
    if failed:
        raise typer.Exit(code=1)


@app.command()
def validate(
    target: Path = typer.Option(
        Path("."), "--target", "-C", help="Project whose danno.toml is swept."
    ),
    only: list[str] = typer.Option(
        None, "--only", help="Restrict the sweep to these danno.toml model keys (repeatable)."
    ),
    max_level: int = typer.Option(
        2, "--max-level", min=0, max=2, help="Highest tier (0 liveness · 1 +tool/bash · 2 +dev)."
    ),
    baseline: bool = typer.Option(
        False, "--baseline", help="Also run the Claude Code baseline row (needs a host token)."
    ),
    baseline_model: str = typer.Option(
        None,
        "--baseline-model",
        help="Pin the baseline's claude model (opus/sonnet/… or a full id).",
    ),
    judge: bool = typer.Option(
        False, "--judge", help="Grade L2 dev quality with an Anthropic judge (needs an API key)."
    ),
    judge_model: str = typer.Option(
        None, "--judge-model", help="Pin the judge model (opus/sonnet/haiku or a full id)."
    ),
    harness: str = typer.Option(
        sandbox_cmd.DEFAULT_HARNESS,
        "--harness",
        help="Harness-under-test for the sweep: opencode (default), claurst, or codex.",
    ),
    env: list[str] = typer.Option(
        None, "--env", help="KEY=VAL credential to inject into cloud-config sweeps (repeatable)."
    ),
    env_file: list[str] = typer.Option(
        None, "--env-file", help="File of KEY=VAL credentials to inject (repeatable)."
    ),
    workspace: Path = typer.Option(
        None, "--workspace", help="Throwaway workspace mount (default a temp dir)."
    ),
    out: Path = typer.Option(
        None, "--out", help="Report output dir (default .danno-validator/<timestamp>/)."
    ),
    menu: bool = typer.Option(
        True, "--menu/--no-menu", help="Emit the annotated menu danno.toml into the run dir."
    ),
    html: bool = typer.Option(
        False, "--html", help="Render the report to HTML (deferred — see help)."
    ),
    keep_sandboxes: bool = typer.Option(
        False, "--keep-sandboxes", help="Leave the disposable sandboxes up for debugging."
    ),
    reset: bool = typer.Option(
        True, "--reset/--no-reset", help="Guarded per-config workspace reset between configs."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero if any swept config fails its requested tiers."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and exit without provisioning or running."
    ),
    capture: bool = typer.Option(
        False,
        "--capture",
        help="Record opencode<->backend wire traffic (Ollama + openai/NVIDIA) into the run dir.",
    ),
    capture_dir: Path = typer.Option(
        None, "--capture-dir", help="Where to write capture JSONL (default <out>/captures/)."
    ),
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Sweep danno.toml's models through the tiered battery and write the report.

    Runs immediately (like `sandbox start`), over disposable, validator-owned
    sandboxes seeded from a copy of your danno.toml — your project is never
    modified. `--dry-run` previews the plan. `--baseline` adds a Claude Code
    reference row (needs CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY). Outputs land
    under `.danno-validator/<timestamp>/` (report + menu + results.json).

    Cloud configs (an anthropic/NVIDIA/… model) need credentials to clear L0:
    danno auto-injects host-exported keys it can identify (the provider's
    `<PROVIDER>_API_KEY`, e.g. `ANTHROPIC_API_KEY`, and any `{env:VAR}` the config
    references); pass `--env KEY=VAL` / `--env-file` to supply or override. A
    missing key only warns — that config errors loudly in its own row. Local Ollama
    models need none.

    `--judge` adds a host-side Anthropic judge that grades L2 software-dev *quality*
    (clarity, over-/under-build) on top of the objective hidden-test verdict; it
    never changes pass/fail. Needs `ANTHROPIC_API_KEY` (API billing) and the
    `danno[validator]` extra; `--judge-model` pins the model (default opus). The
    graded verdict lands in the report and results.json.
    """
    from danno_validator.console import ConsoleReporter
    from danno_validator.run import ValidateOptions, run_validate

    if html:
        log_err(
            "--html is not yet wired: HTML rendering ships with the danno[validator] "
            "Sphinx extra (tracked for M7). Re-run without --html; the MyST report is "
            "still written to the run dir."
        )
        raise typer.Exit(code=3)

    cfg = _load(target / "danno.toml")
    opts = ValidateOptions(
        target=target,
        only=only or None,
        max_level=max_level,
        baseline=baseline,
        baseline_model=baseline_model,
        judge=judge,
        judge_model=judge_model,
        harness=harness,
        env=env or [],
        env_file=env_file or [],
        workspace=workspace,
        out_dir=out,
        menu=menu,
        keep_sandboxes=keep_sandboxes,
        reset=reset,
        strict=strict,
        dry_run=dry_run,
        capture=capture or capture_dir is not None,
        capture_dir=capture_dir,
    )
    try:
        result = run_validate(
            cfg, opts, Runner(apply=True, verbose=verbose), reporter=ConsoleReporter()
        )
    except ValueError as exc:  # e.g. --only names an undeclared model (fail loud)
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandFailedError, CommandNotFoundError) as exc:  # missing token / Docker
        log_err(str(exc))
        raise typer.Exit(code=4) from exc
    if result.strict_failed:
        raise typer.Exit(code=1)


@app.command()
def bench(
    target: Path = typer.Option(
        Path("."), "--target", "-C", help="Project whose danno.toml models are the matrix."
    ),
    harness: list[str] = typer.Option(
        None,
        "--harness",
        help="Harness-under-test (repeatable): opencode (default), claurst, codex, or claude. "
        "codex is the OpenAI Codex CLI over the Responses API on local Ollama (needs Ollama "
        ">= 0.13.3). claude is the cloud reference harness — sweeps the models declared on an "
        "inert backend, threading each tag to `claude --model`; collapses to one "
        "`(default model)` row if none are declared. Overrides benchmarks.toml [harnesses]; "
        "several harnesses produce a comparison report.",
    ),
    only: list[str] = typer.Option(
        None,
        "--only",
        help="Restrict the model matrix to these danno.toml model keys (repeatable). "
        "For --harness claude, restricts the swept inert-backend models the same way.",
    ),
    benchmarks: Path = typer.Option(
        None, "--benchmarks", help="benchmarks.toml path (default: next to danno.toml)."
    ),
    workspace: Path = typer.Option(
        None, "--workspace", help="Throwaway workspace mount (default a temp dir)."
    ),
    out: Path = typer.Option(None, "--out", help="Output dir (default .danno-bench/<timestamp>/)."),
    keep_sandboxes: bool = typer.Option(
        False, "--keep-sandboxes", help="Leave the disposable sandboxes up for debugging."
    ),
    save_captures: bool = typer.Option(
        True,
        "--save-captures/--no-save-captures",
        help="Persist each permutation's harness<->backend wire capture (JSONL + readable "
        "transcript) under <out>/captures + <out>/transcripts. On by default. Capture itself is "
        "ALWAYS on in bench (it feeds the runaway-gate token/round tally); --no-save-captures "
        "runs the recording proxy as a pure gate sensor that writes NO capture bytes to disk — "
        "no dir, no JSONL, no transcript, no message bodies retained. The report's numeric wire "
        "metrics (<out>/metrics + bench.json) still derive live, so you keep the numbers "
        "without keeping the prompts.",
    ),
    capture: bool = typer.Option(
        False,
        "--capture",
        hidden=True,
        help="Deprecated no-op: capture is always on in bench (it powers the runaway gates).",
    ),
    capture_dir: Path = typer.Option(
        None, "--capture-dir", help="Where to persist capture JSONL (default <out>/captures/)."
    ),
    sample: bool = typer.Option(
        False,
        "--sample",
        help="Sample host CPU/memory + GPU (nvidia-smi) + model VRAM (Ollama /api/ps) on an "
        "interval during each permutation, writing a time series under <out>/samples/.",
    ),
    sample_interval: float = typer.Option(
        0.5, "--sample-interval", help="Resource sampler interval in seconds (with --sample)."
    ),
    warm: bool = typer.Option(
        True,
        "--warm/--no-warm",
        help="Pre-load each local Ollama model (one /v1 call) before the timed cells so the "
        "first cell doesn't eat a cold model-load hit. On by default; --no-warm to measure "
        "cold-start too. The per-model warm result is recorded in provenance.json.",
    ),
    env: list[str] = typer.Option(
        None,
        "--env",
        help="KEY=VAL credential to inject into every bench turn (repeatable). Cloud keys are "
        "read from danno's host env automatically; use this to override or supply one inline.",
    ),
    env_file: list[str] = typer.Option(
        None, "--env-file", help="File of KEY=VAL credentials to inject (repeatable)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and exit without provisioning or running."
    ),
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Run the enabled benchmark suites across danno.toml's models against a harness.

    Suites + selection come from `benchmarks.toml` (Aider Polyglot + a SWE-bench Verified
    subset; each independently enabled with a `select` list). Provisions disposable,
    validator-owned sandboxes over a throwaway workspace, runs each enabled suite for
    every model variant (the permutations), writes `bench.json` + a summary, and tears
    the sandboxes down. Your project is never modified.

    These run real benchmark task *content* via danno's own execution model — NOT the
    official Docker-per-task harness, so the pass counts are not official benchmark
    scores. `--harness claurst` benchmarks the Rust Claude-Code clone on local models;
    `--harness codex` benchmarks the OpenAI Codex CLI over the Responses API on local Ollama
    (needs Ollama >= 0.13.3); `--harness claude` adds the cloud reference harness (needs a
    host token), sweeping every model declared on an inert backend and honoring `--only`.
    """
    from danno_validator.suites.bench import (
        BenchOptions,
        resolve_bench_harnesses,
        run_bench_harnesses,
    )
    from danno_validator.suites.config import DEFAULT_BENCHMARKS_FILE, load_benchmarks

    if not save_captures and capture_dir is not None:
        # Contradictory flags — fail loud at the boundary before any config I/O (F4). There is
        # no sensible "both": --no-save-captures persists nothing, so --capture-dir (where to
        # persist) would be silently discarded.
        log_err(
            "--no-save-captures conflicts with --capture-dir: nothing is persisted, so a "
            "capture directory is meaningless. Drop one."
        )
        raise typer.Exit(code=2)

    cfg = _load(target / "danno.toml")
    bench_path = benchmarks or (target / DEFAULT_BENCHMARKS_FILE)
    try:
        bench_cfg = load_benchmarks(bench_path)
    except ValueError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc
    if not bench_cfg.any_enabled():
        log_err(
            f"no benchmark suites enabled in {bench_path} — set enabled = true under "
            "[aider_polyglot] or [swebench] and list `select` ids."
        )
        raise typer.Exit(code=2)
    try:
        harnesses = resolve_bench_harnesses(harness or None, bench_cfg)
    except ValueError as exc:  # unknown --harness / [harnesses] name (fail loud)
        log_err(str(exc))
        raise typer.Exit(code=2) from exc
    if capture:
        log_warn("--capture is deprecated and a no-op: capture is always on in bench.")
    opts = BenchOptions(
        target=target,
        harness=harnesses[0],
        only=only or None,
        benchmarks_path=bench_path,
        workspace=workspace,
        out_dir=out,
        keep_sandboxes=keep_sandboxes,
        dry_run=dry_run,
        save_captures=save_captures,
        capture_dir=capture_dir,
        sample=sample,
        sample_interval=sample_interval,
        warm=warm,
        env=env or [],
        env_file=env_file or [],
    )
    try:
        run_bench_harnesses(cfg, bench_cfg, opts, Runner(apply=True, verbose=verbose), harnesses)
    except ValueError as exc:  # bad --only / unknown swebench id (fail loud)
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandFailedError, CommandNotFoundError) as exc:  # Docker / provision failure
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def benchmark(
    configs: Path = typer.Argument(
        ..., help="Directory of candidate configs — each a subdir with its own .opencode/ tree."
    ),
    target: Path = typer.Option(
        Path("."), "--target", "-C", help="Project whose danno.toml supplies sandbox/env setup."
    ),
    max_level: int = typer.Option(
        2, "--max-level", min=0, max=2, help="Highest tier (0 liveness · 1 +tool/bash · 2 +dev)."
    ),
    baseline: bool = typer.Option(
        False, "--baseline", help="Also run the Claude Code reference row (needs a host token)."
    ),
    baseline_model: str = typer.Option(
        None, "--baseline-model", help="Pin the baseline's claude model (opus/sonnet/… or an id)."
    ),
    judge: bool = typer.Option(
        False, "--judge", help="Grade L2 dev quality with an Anthropic judge (needs an API key)."
    ),
    judge_model: str = typer.Option(
        None, "--judge-model", help="Pin the judge model (opus/sonnet/haiku or a full id)."
    ),
    harness: str = typer.Option(
        sandbox_cmd.DEFAULT_HARNESS,
        "--harness",
        help="Harness-under-test. benchmark compares opencode config trees, so opencode "
        "only; to benchmark claurst across danno.toml models use `danno bench --harness claurst`.",
    ),
    env: list[str] = typer.Option(
        None, "--env", help="KEY=VAL credential to inject into every config run (repeatable)."
    ),
    env_file: list[str] = typer.Option(
        None, "--env-file", help="File of KEY=VAL credentials to inject (repeatable)."
    ),
    workspace: Path = typer.Option(
        None, "--workspace", help="Throwaway workspace mount (default a temp dir)."
    ),
    out: Path = typer.Option(
        None, "--out", help="Report output dir (default .danno-benchmark/<timestamp>/)."
    ),
    keep_sandboxes: bool = typer.Option(
        False, "--keep-sandboxes", help="Leave the disposable sandboxes up for debugging."
    ),
    reset: bool = typer.Option(
        True, "--reset/--no-reset", help="Guarded workspace reset between configs."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero if any config fails its requested tiers."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and exit without provisioning or running."
    ),
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Benchmark candidate agent CONFIGS for editing performance.

    Where `validate` sweeps your danno.toml's models, `benchmark` sweeps whole
    configs: each subdir of CONFIGS is a candidate holding its own `.opencode/` tree
    (opencode.jsonc + agent `.md`). danno applies each into a disposable,
    validator-owned workspace and runs the same tiered battery (L0→L1→L2, `--judge`
    for dev-quality) plus the optional Claude `--baseline`, then writes a comparison
    report + results.json under `.danno-benchmark/<timestamp>/`. Your project is never
    modified; `danno.toml` is read only for sandbox/env setup.
    """
    from danno_validator.benchmark import BenchmarkOptions, run_benchmark

    cfg = _load(target / "danno.toml")
    opts = BenchmarkOptions(
        configs_dir=configs,
        target=target,
        max_level=max_level,
        baseline=baseline,
        baseline_model=baseline_model,
        judge=judge,
        judge_model=judge_model,
        harness=harness,
        env=env or [],
        env_file=env_file or [],
        workspace=workspace,
        out_dir=out,
        keep_sandboxes=keep_sandboxes,
        reset=reset,
        strict=strict,
        dry_run=dry_run,
    )
    try:
        result = run_benchmark(cfg, opts, Runner(apply=True, verbose=verbose))
    except (FileNotFoundError, ValueError) as exc:  # bad/empty configs dir (fail loud)
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandFailedError, CommandNotFoundError) as exc:  # missing token / Docker
        log_err(str(exc))
        raise typer.Exit(code=4) from exc
    if result.strict_failed:
        raise typer.Exit(code=1)


_HARNESS_OPT = typer.Option(
    sandbox_cmd.DEFAULT_HARNESS,
    "--harness",
    help="Harness: opencode, claude, claurst, or codex; non-default harnesses get their own "
    "sandbox.",
)
_MODEL_OPT = typer.Option(
    None,
    "--model",
    "-m",
    help="Model for --harness claurst/codex (a danno.toml models entry, e.g. gemma4 or an "
    "OpenAI-compatible cloud model). clone-harnesses only; a backend danno can't wire, or a "
    "raw non-Ollama ref, is rejected loud (codex is local-Ollama only so far).",
)


def _sandbox_target(target: Path, name: str | None, harness: str) -> tuple[Path, str]:
    abs_target = Path(target).resolve()
    if not abs_target.is_dir():
        log_err(f"target directory not found: {target}")
        raise typer.Exit(code=3)
    return abs_target, (name or sandbox_cmd.default_name(abs_target, harness))


def _resolve_home(abs_target: Path, sandbox_name: str) -> Path | None:
    """Resolve the agent-home dir (loud exit 2 on a malformed config)."""
    try:
        return sandbox_cmd.resolve_home(abs_target, sandbox_name)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc


def _resolve_model(
    abs_target: Path, harness: str, model: str | None
) -> tuple[str | None, list[str]]:
    """Resolve `--model` for `sandbox start`. Returns `(ref, cloud_env_lines)` — `ref`
    is None when no `--model` is given; `cloud_env_lines` carries a cloud model's provider
    key (`["<VAR>=<value>"]`, injected into the chmod-600 env-file) and is empty for local
    Ollama. Maps a danno [models] name to the clone harness's `-m` ref, failing loud on a
    malformed config (exit 2) or an unreachable model, missing cloud key, or a non-clone
    harness (exit 4)."""
    if model is None:
        return None, []
    try:
        return sandbox_cmd.resolve_start(abs_target, harness, model)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc
    except CommandFailedError as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


_NAME_OPT_HELP = "Sandbox name (default danno-<parent>-<dir>)."

_CAPTURE_OPT = typer.Option(
    False,
    "--capture",
    help="Record harness<->backend wire traffic (opencode<->Ollama/openai-NVIDIA, or "
    "claurst<->Ollama); needs --apply.",
)
_CAPTURE_DIR_OPT = typer.Option(
    None, "--capture-dir", help="Where to write capture JSONL (default ./.danno/captures/<ts>/)."
)


def _resolve_capture_dir(capture: bool, capture_dir: Path | None) -> Path | None:
    """The capture dir for `sandbox start`/`shell`: an explicit `--capture-dir`, else a
    timestamped default under `./.danno/captures/` when `--capture` is set, else None."""
    if capture_dir is not None:
        return capture_dir
    if capture:
        from datetime import datetime

        return Path(".danno") / "captures" / datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return None


@sandbox_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def sandbox_start(
    ctx: typer.Context,
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    harness: str = _HARNESS_OPT,
    model: str = _MODEL_OPT,
    env: list[str] = typer.Option(None, "--env", help="KEY=VAL to inject (repeatable)."),
    env_file: list[str] = typer.Option(None, "--env-file", help="File of KEY=VAL to inject."),
    capture: bool = _CAPTURE_OPT,
    capture_dir: Path = _CAPTURE_DIR_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Launch the in-container harness (provisioning it first under `--apply`).

    Launching is the command's purpose, so it runs without `--apply`; `--apply`
    additionally executes the provisioning side effects (create/proxy). On an
    unprovisioned sandbox without `--apply`, it fails loud with the fix.

    Tip: `cd <project> && danno sandbox start` (no --target/--name) recomputes the
    same name every time — stand in the sandbox's directory rather than naming it.

    `--harness claurst` runs a pure-Rust Claude-Code clone on local Ollama or a cloud
    provider danno can fully wire (today NVIDIA NIM); pick the model with `-m <name>`
    (a danno.toml models entry — its cloud key is injected from the backend's
    `api_key_env`). `--harness codex` runs the OpenAI Codex CLI over the Responses API on
    local Ollama (needs Ollama >= 0.13.3; `-m <name>` selects a danno.toml Ollama entry).
    Anything after `--` is forwarded verbatim to the harness, e.g.
    `danno sandbox start --harness claude -- --resume <id>`.
    """
    abs_target, sandbox_name = _sandbox_target(target, name, harness)
    home = _resolve_home(abs_target, sandbox_name)
    resolved_model, cloud_env = _resolve_model(abs_target, harness, model)
    _guard(
        lambda: sandbox_cmd.start(
            Runner(apply=apply, verbose=verbose),
            sandbox_name,
            abs_target,
            harness=harness,
            env_pairs=(env or []) + cloud_env,
            env_files=env_file or [],
            home=home,
            registry_path=registry.default_path(),
            harness_args=ctx.args,
            capture_dir=_resolve_capture_dir(capture, capture_dir),
            model=resolved_model,
        )
    )


@sandbox_app.command("shell")
def sandbox_shell(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    harness: str = _HARNESS_OPT,
    env: list[str] = typer.Option(None, "--env", help="KEY=VAL to inject (repeatable)."),
    env_file: list[str] = typer.Option(None, "--env-file", help="File of KEY=VAL to inject."),
    capture: bool = _CAPTURE_OPT,
    capture_dir: Path = _CAPTURE_DIR_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Open an interactive bash shell inside the sandbox VM.

    Identical to `sandbox start` except it drops you at a bash prompt instead of
    launching the harness: same provisioning (under `--apply`), same `-w <project>`
    working dir, and the same injected env (harness auth / Ollama URL / relocated
    config home). So a tool you run by hand here is wired exactly as `start` wires
    it. On an unprovisioned sandbox without `--apply`, it fails loud with the fix."""
    abs_target, sandbox_name = _sandbox_target(target, name, harness)
    home = _resolve_home(abs_target, sandbox_name)
    _guard(
        lambda: sandbox_cmd.shell(
            Runner(apply=apply, verbose=verbose),
            sandbox_name,
            abs_target,
            harness=harness,
            env_pairs=env or [],
            env_files=env_file or [],
            home=home,
            registry_path=registry.default_path(),
            capture_dir=_resolve_capture_dir(capture, capture_dir),
        )
    )


@sandbox_app.command("stop")
def sandbox_stop(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    harness: str = _HARNESS_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Stop the sandbox VM."""
    _, sandbox_name = _sandbox_target(target, name, harness)
    _guard(lambda: sandbox_cmd.stop(Runner(apply=apply, verbose=verbose), sandbox_name))


@sandbox_app.command("rebuild")
def sandbox_rebuild(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    harness: str = _HARNESS_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Remove and re-provision the sandbox from scratch (the harness home survives)."""
    abs_target, sandbox_name = _sandbox_target(target, name, harness)
    home = _resolve_home(abs_target, sandbox_name)
    _guard(
        lambda: sandbox_cmd.rebuild(
            Runner(apply=apply, verbose=verbose),
            sandbox_name,
            abs_target,
            harness=harness,
            home=home,
            registry_path=registry.default_path(),
        )
    )


@sandbox_app.command("update")
def sandbox_update(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    harness: str = _HARNESS_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Advise how to update the harness inside the sandbox."""
    _, sandbox_name = _sandbox_target(target, name, harness)
    _guard(lambda: sandbox_cmd.update(Runner(apply=apply, verbose=verbose), sandbox_name, harness))


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """Read-only: list recorded sandboxes (name → target) and their live status."""
    sandbox_cmd.ls(registry.default_path())


# Re-exported so tests and `from book_em_danno.cli import console` keep working.
__all__ = ["app", "console"]
