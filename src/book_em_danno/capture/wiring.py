"""Wire a `DannoConfig` for `--capture`: redirect each redirectable backend's
`base_url` at a per-backend recording proxy, and thread the egress allow-list.

`plan_capture` is a pure transform (it returns a rewritten config copy + the proxy
targets) so command code can unit-test the redirect without Docker or a live proxy.
`captures_running` owns the proxies' lifetime; `capture_allow_hosts` produces the
sandbox egress allow-list that opens the proxy ports. Deliberately imports nothing
from `commands.sandbox` (the caller passes the base allow-list) so `sandbox` can call
back into this module without an import cycle.
"""

from __future__ import annotations

import contextlib
import re
import socket
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import CaptureProxyConfig, _CaptureServer, capture_proxy
from book_em_danno.capture.summary import CallSummary
from book_em_danno.config.schema import DannoConfig, OllamaBackend, OpenAIBackend

# The sandbox dials the host via this name; the Docker egress proxy rewrites it to
# `localhost` for allow-list matching, so the allow-rule names `localhost:<port>`.
_SANDBOX_HOST_ALIAS = "host.docker.internal"


@dataclass(frozen=True)
class CaptureTarget:
    """One backend's capture proxy: where the harness dials, where the proxy forwards,
    and where the JSONL lands."""

    backend_name: str
    real_base_url: str  # the backend's original base_url (for provenance/logging)
    upstream: str  # scheme+host[:port] the proxy re-originates to (no path)
    proxy_port: int  # host port the proxy binds (0.0.0.0)
    capture_file: Path


def _free_port() -> int:
    """An OS-assigned free TCP port. Small TOCTOU window before the proxy binds it;
    the proxy fails loud if it loses the race (`capture_proxy`)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def _upstream_for(base_url: str) -> str:
    """The scheme+host[:port] the host-side proxy forwards to (path appended later).

    A backend's `base_url` uses `host.docker.internal` (the sandbox's name for the
    host); from the host the proxy runs on, that same service is `127.0.0.1`. A public
    cloud host (e.g. NVIDIA) is reachable verbatim."""
    parts = urlsplit(base_url)
    if parts.hostname == _SANDBOX_HOST_ALIAS:
        netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
    else:
        netloc = parts.netloc
    return f"{parts.scheme}://{netloc}"


def plan_capture(config: DannoConfig, capture_dir: Path) -> tuple[DannoConfig, list[CaptureTarget]]:
    """Return (rewritten_config, targets) for capturing `config`'s redirectable backends.

    Each `OllamaBackend`/`OpenAIBackend` gets a free port and a target; its `base_url`
    is rewritten to `http://host.docker.internal:<port><path>` so the sandboxed harness
    dials the proxy (always plain HTTP — the proxy re-originates TLS for cloud). The
    URL path (e.g. `/v1`) is preserved so `/v1/chat/completions` reconstructs upstream.
    `llamacpp` (stubbed) and built-in cloud refs like `anthropic/*` (no backend → no
    `base_url` lever) are left untouched; see `uncaptured_cloud_refs`. The config is
    copied (never mutated)."""
    targets: list[CaptureTarget] = []
    new_backends: dict[str, object] = {}
    for name, backend in config.backends.items():
        if isinstance(backend, OllamaBackend | OpenAIBackend):
            port = _free_port()
            path = urlsplit(backend.base_url).path
            targets.append(
                CaptureTarget(
                    backend_name=name,
                    real_base_url=backend.base_url,
                    upstream=_upstream_for(backend.base_url),
                    proxy_port=port,
                    capture_file=capture_dir / f"{name}.jsonl",
                )
            )
            rewritten = f"http://{_SANDBOX_HOST_ALIAS}:{port}{path}"
            new_backends[name] = backend.model_copy(update={"base_url": rewritten})
        else:
            new_backends[name] = backend
    new_config = config.model_copy(update={"backends": new_backends})
    return new_config, targets


@dataclass(frozen=True)
class RunningCaptures:
    """A live handle over a cell's running proxies, exposing their merged body-free
    summaries. Held open for the block; its `read_summaries()` stays valid after the block
    exits (the servers outlive `serve_forever`), so the metrics-only path can roll up numbers
    once the harness turn is done — with no bodies ever written to disk."""

    _servers: tuple[_CaptureServer, ...]

    def read_summaries(self) -> list[CallSummary]:
        """Every proxy's summaries combined and ordered by `seq` — mirroring how
        `wire_metrics.metrics_from_files` combines per-file records before rolling up."""
        merged = [s for server in self._servers for s in server.read_summaries()]
        merged.sort(key=lambda s: s.seq)
        return merged


@contextmanager
def captures_running(
    targets: Sequence[CaptureTarget], tally: GateTally | None = None, *, persist: bool = True
) -> Iterator[RunningCaptures]:
    """Run every target's capture proxy for the duration of the block (one ExitStack),
    yielding a `RunningCaptures` handle over their live summaries.

    Ordering invariant (caller's job): enter this BEFORE provisioning/launching the
    harness and exit it AFTER the last turn — the proxy must be up for every request.
    `tally`, when set (runaway gates), is shared across all of a cell's backend proxies
    so the watchdog sees the cell's combined inference-call + token totals. `persist=False`
    (`--no-save-captures`) runs the proxies as pure gate sensors: they feed the tally and
    accumulate body-free summaries but write no JSONL to disk."""
    with contextlib.ExitStack() as stack:
        servers = [
            stack.enter_context(
                capture_proxy(
                    CaptureProxyConfig(
                        upstream=target.upstream,
                        capture_file=target.capture_file,
                        port=target.proxy_port,
                        tally=tally,
                        persist=persist,
                    )
                )
            )
            for target in targets
        ]
        yield RunningCaptures(_servers=tuple(servers))


def capture_allow_hosts(targets: Sequence[CaptureTarget], base: Sequence[str]) -> tuple[str, ...]:
    """The sandbox egress allow-list: the caller's `base` plus a `localhost:<port>`
    hole per proxy (the egress rewrites `host.docker.internal`→`localhost`)."""
    return tuple(base) + tuple(f"localhost:{t.proxy_port}" for t in targets)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    """A filesystem-safe slug for a path segment (kept local so this lower-layer
    module doesn't import `danno_validator.report.slug`)."""
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "x"


def perm_segment(suite: str, task_id: str, model: str | None) -> Path:
    """The `<suite>/<task>/<model-slug>` sub-path shared by every per-permutation
    sidecar family (captures/metrics/transcripts/samples) so they line up across their
    sibling dirs. `None` model (the claude reference row) → the `default` segment."""
    name = _slug(model) if model else "default"
    return Path(_slug(suite)) / _slug(task_id) / name


@dataclass(frozen=True)
class CaptureBinding:
    """The fixed-port capture targets plus the root dir, ready to mint a
    per-permutation capture context for `danno bench`.

    `plan_capture` runs ONCE per bench run (stable proxy ports baked into the harness's
    provisioning), then `permutation()` is called per `(suite, task, model)` turn to
    write that turn's wire traffic to its own JSONL. Because bench turns run
    sequentially and `http.server.HTTPServer` sets `allow_reuse_address`, standing a
    fresh proxy up on the same port each turn is race-free and needs no proxy changes.
    """

    targets: tuple[CaptureTarget, ...]
    capture_dir: Path
    # When False (`--no-save-captures`), the per-permutation proxies run as pure gate sensors
    # (tally + in-RAM summaries) and write no capture JSONL/transcript to disk; the report's
    # wire metrics still roll up from the live summaries. Default preserves save-mode.
    persist: bool = True

    def permutation_targets(
        self, *, suite: str, task_id: str, model: str | None
    ) -> list[CaptureTarget]:
        """This permutation's targets, each rewritten to its own namespaced JSONL:
        `<capture_dir>/<suite>/<task>/<model-slug>.<backend>.jsonl`."""
        seg = self._perm_segment(suite=suite, task_id=task_id, model=model)
        return [
            replace(
                t,
                capture_file=self.capture_dir
                / seg.parent
                / f"{seg.name}.{_slug(t.backend_name)}.jsonl",
            )
            for t in self.targets
        ]

    def permutation(
        self, *, suite: str, task_id: str, model: str | None, tally: GateTally | None = None
    ) -> AbstractContextManager[RunningCaptures]:
        """A `captures_running` context whose files are namespaced by this permutation,
        yielding the `RunningCaptures` handle over its live summaries. `tally` (runaway gates)
        is shared across this cell's backend proxies. When `persist` is False the proxies write
        nothing to disk — the caller rolls the numbers up from the handle's summaries."""
        return captures_running(
            self.permutation_targets(suite=suite, task_id=task_id, model=model),
            tally=tally,
            persist=self.persist,
        )

    def _perm_segment(self, *, suite: str, task_id: str, model: str | None) -> Path:
        """The `<suite>/<task>/<model-slug>` sub-path shared by every per-permutation
        sidecar (capture/metrics/transcript), so they line up across the sibling dirs."""
        return perm_segment(suite, task_id, model)

    def metrics_path(self, *, suite: str, task_id: str, model: str | None) -> Path:
        """Where this permutation's derived wire metrics land — a `metrics/` sibling of
        `capture_dir` (so `<out>/captures` → `<out>/metrics`)."""
        seg = self._perm_segment(suite=suite, task_id=task_id, model=model)
        return self.capture_dir.parent / "metrics" / seg.with_suffix(".json")

    def transcript_path(self, *, suite: str, task_id: str, model: str | None) -> Path:
        """Where this permutation's readable transcript lands — a `transcripts/` sibling
        of `capture_dir` (§3.4)."""
        seg = self._perm_segment(suite=suite, task_id=task_id, model=model)
        return self.capture_dir.parent / "transcripts" / seg.with_suffix(".md")

    def ollama_port(self, config: DannoConfig) -> int | None:
        """The proxy port for the first Ollama backend, or None — this is the
        upstream (`capture_port`) for claurst local turns, which forward their
        Ollama traffic through the capture proxy instead of straight to the host."""
        for target in self.targets:
            backend = config.backends.get(target.backend_name)
            if isinstance(backend, OllamaBackend):
                return target.proxy_port
        return None


def uncaptured_cloud_refs(config: DannoConfig) -> list[str]:
    """Raw OpenCode model refs in `[agents]` (values containing '/') that capture
    cannot reach — they have no danno backend / `base_url` lever (e.g. `anthropic/*`).
    A command warns loudly (Working Rule 8) so a captured run never silently omits
    these from the JSONL."""
    refs: set[str] = set()
    for value in config.agents.values():
        ref = value if isinstance(value, str) else value.model
        if ref and "/" in ref:
            refs.add(ref)
    return sorted(refs)
