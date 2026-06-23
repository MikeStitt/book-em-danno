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
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from book_em_danno.capture.proxy import CaptureProxyConfig, capture_proxy
from book_em_danno.config.schema import DannoConfig, OllamaBackend, OpenAIBackend

# The sandbox dials the host via this name; the Docker egress proxy rewrites it to
# `localhost` for allow-list matching, so the allow-rule names `localhost:<port>`.
_SANDBOX_HOST_ALIAS = "host.docker.internal"


@dataclass(frozen=True)
class CaptureTarget:
    """One backend's capture proxy: where the agent dials, where the proxy forwards,
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
    is rewritten to `http://host.docker.internal:<port><path>` so the sandboxed agent
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


@contextmanager
def captures_running(targets: Sequence[CaptureTarget]) -> Iterator[None]:
    """Run every target's capture proxy for the duration of the block (one ExitStack).

    Ordering invariant (caller's job): enter this BEFORE provisioning/launching the
    agent and exit it AFTER the last turn — the proxy must be up for every request."""
    with contextlib.ExitStack() as stack:
        for target in targets:
            stack.enter_context(
                capture_proxy(
                    CaptureProxyConfig(
                        upstream=target.upstream,
                        capture_file=target.capture_file,
                        port=target.proxy_port,
                    )
                )
            )
        yield


def capture_allow_hosts(targets: Sequence[CaptureTarget], base: Sequence[str]) -> tuple[str, ...]:
    """The sandbox egress allow-list: the caller's `base` plus a `localhost:<port>`
    hole per proxy (the egress rewrites `host.docker.internal`→`localhost`)."""
    return tuple(base) + tuple(f"localhost:{t.proxy_port}" for t in targets)


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
