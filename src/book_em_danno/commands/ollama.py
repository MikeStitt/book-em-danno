"""Ollama model management (internal; orchestrated by `install`).

Ports `scripts/setup-ollama.sh`. Model pulls go through the Runner (advise by
default); reachability and verification are read-only HTTP probes used by the
slow tests and by `--apply` runs. Uses stdlib `urllib` — no new dependency.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

from ..core.exec import CommandFailedError, Runner, log_info, log_warn, retry_transient

# Transient network failures worth a retry before we trust a probe's negative verdict: a
# connection blip / timeout, not a parsed-but-negative body (that's a real answer, not a blip).
_TRANSIENT_NET = (urllib.error.URLError, OSError)

# Env override for the Ollama endpoint danno probes. Set it to route danno at a remote
# server, e.g. a LAN host: `DANNO_OLLAMA_HOST_URL=http://10.0.1.27:11434`. The slow Ollama
# tests and `--apply` reachability probes honor it; unset it and danno uses local Ollama.
HOST_URL_ENV = "DANNO_OLLAMA_HOST_URL"
_LOCAL_HOST_URL = "http://localhost:11434"


def resolve_host_url(env: Mapping[str, str] | None = None) -> str:
    """The Ollama base URL danno probes: `DANNO_OLLAMA_HOST_URL` if set, else local Ollama.

    A trailing slash is trimmed so `f"{url}/api/tags"` stays well-formed. `env` defaults to
    the process environment; pass a mapping to resolve deterministically in tests."""
    raw = (os.environ if env is None else env).get(HOST_URL_ENV, "").strip()
    return raw.rstrip("/") if raw else _LOCAL_HOST_URL


# Resolved once at import: the module-level default every probe below falls back to. Setting
# the env var before the process starts (the slow-test invocation) redirects them all.
DEFAULT_HOST_URL = resolve_host_url()


def reachable(host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> bool:
    """True if the Ollama server answers /api/tags."""
    try:
        with urllib.request.urlopen(f"{host_url}/api/tags", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def installed_tags(host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> set[str]:
    """Model tags already pulled, from /api/tags. Empty set if Ollama is
    unreachable — best-effort, so the caller falls back to advising the pull."""
    try:
        with urllib.request.urlopen(f"{host_url}/api/tags", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return set()
    return {m["name"] for m in body.get("models", []) if "name" in m}


def running_models(host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> list[dict]:
    """Models currently resident in Ollama, from /api/ps (best-effort).

    Each entry carries at least `name`, `size_vram` (bytes attributed to this model
    on the GPU), and `expires_at` (when Ollama will evict it). Used by the bench
    resource sampler for model-attributed VRAM (§5.4) and model-load detection
    (§2.5). Returns `[]` when Ollama is unreachable or the body is unparseable —
    the sampler degrades to no `model_ps` rows rather than failing the bench.
    """
    try:
        with urllib.request.urlopen(f"{host_url}/api/ps", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    return [m for m in body.get("models", []) if isinstance(m, dict)]


def model_digest(tag: str, host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> str | None:
    """The exact model bytes' digest (`sha256:…`) for `tag` from /api/tags (§7.1), or
    None if Ollama is unreachable or the tag isn't present. Provenance: two runs of the
    "same" tag can differ if the model was re-pulled — the digest pins what actually ran."""
    try:
        with urllib.request.urlopen(f"{host_url}/api/tags", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    for model in body.get("models", []):
        if model.get("name") == tag:
            digest = model.get("digest")
            return str(digest) if digest else None
    return None


def model_params(tag: str, host_url: str = DEFAULT_HOST_URL, *, timeout: float = 5.0) -> dict:
    """Static model facts for `tag` from /api/show (§7.2): quantization, parameter
    count, and the architecture's context length (the real ceiling for §6.3 headroom,
    NOT opencode's client-side `context_budget`). Returns `{}` best-effort on any
    failure. Keys present only when Ollama reports them: `quantization`, `param_size`,
    `context_length`, `architecture`."""
    payload = json.dumps({"model": tag}).encode()
    try:
        req = urllib.request.Request(
            f"{host_url}/api/show", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return {}
    return _parse_model_show(body)


def _parse_model_show(body: dict) -> dict:
    """Extract the provenance fields from an /api/show body (pure, for tests)."""
    out: dict = {}
    details = body.get("details") or {}
    if details.get("quantization_level"):
        out["quantization"] = details["quantization_level"]
    if details.get("parameter_size"):
        out["param_size"] = details["parameter_size"]
    info = body.get("model_info") or {}
    arch = info.get("general.architecture")
    if arch:
        out["architecture"] = arch
        ctx = info.get(f"{arch}.context_length")
        if isinstance(ctx, int):
            out["context_length"] = ctx
    return out


# Minimum Ollama version exposing the experimental OpenAI Responses endpoint
# (`/v1/responses`), which codex requires (Phase-0 spike — `.docs/codex-integration.md`).
MIN_OLLAMA_FOR_RESPONSES = (0, 13, 3)


def ollama_version(host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> str | None:
    """The running Ollama server's version string from `/api/version` (e.g. "0.30.6"), or
    None if unreachable/unparseable — best-effort."""
    try:
        with urllib.request.urlopen(f"{host_url}/api/version", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    v = body.get("version")
    return str(v) if v else None


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version ("0.30.6") to an int tuple for comparison; non-numeric
    trailing parts (e.g. "-rc1") are dropped."""
    parts: list[int] = []
    for chunk in version.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def responses_api_ready(host_url: str = DEFAULT_HOST_URL, *, timeout: float = 2.0) -> bool | None:
    """True iff this Ollama exposes the OpenAI Responses endpoint codex needs.

    Prefers the direct endpoint probe (a POST `/v1/responses` with an empty body returns
    400 — endpoint exists, body rejected — not 404, per the Phase-0 spike), and falls back
    to the `/api/version` gate (≥ 0.13.3). Returns None only when Ollama is unreachable, so
    the caller can distinguish "no Ollama" from "Ollama too old"."""
    if not reachable(host_url, timeout=timeout):
        return None
    try:
        req = urllib.request.Request(
            f"{host_url}/v1/responses", data=b"{}", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status != 404
    except urllib.error.HTTPError as exc:
        return exc.code != 404  # 400 (bad body) means the endpoint exists
    except (urllib.error.URLError, OSError):
        pass
    version = ollama_version(host_url, timeout=timeout)
    if version is None:
        return None
    return _version_tuple(version) >= MIN_OLLAMA_FOR_RESPONSES


def ensure_model(runner: Runner, tag: str, *, host_url: str = DEFAULT_HOST_URL) -> list[str]:
    """Advise (and under --apply, run) `ollama pull <tag>`. `ollama pull` is itself
    idempotent — an already-present model is a fast no-op.

    Under --apply a pull can fail on a transient registry/network blip; that shouldn't kill
    the whole provision on the first try. The pull is retried (TRANSIENT) and only a
    persistent failure escalates to ERROR and raises (policy §5, TRANSIENT→ERROR). Advise-only
    runs never execute, so `op` returns on the first call with no retry."""

    def _pull() -> list[str]:
        return runner.advise(["ollama", "pull", tag], why=f"ensure Ollama model present: {tag}")

    return retry_transient(_pull, what=f"ollama pull {tag}", retry_on=(CommandFailedError,))


def warm_model(tag: str, *, host_url: str = DEFAULT_HOST_URL, timeout: float = 600.0) -> dict:
    """Pre-load `tag` into Ollama so the first *timed* bench cell doesn't eat the cold
    model-load hit. Returns `{"tag", "cache_hit": bool, "warm_load_s": float | None}`.

    Warms via the SAME `/v1/chat/completions` transport the harnesses use — NOT
    `/api/generate` — on purpose: under `/v1` Ollama loads the model at its full trained
    context, and a Modelfile-baked `num_ctx` tag loads its own runner; either way this
    loads the exact runner every cell reuses. A small-`num_ctx` `/api/generate` warm-up
    would load a *different* runner and the harness would still trigger the real load.

    - `cache_hit=True, warm_load_s=0.0` when the tag is already resident (`/api/ps`): the
      keep-alive window covered it, so there was nothing to load.
    - `cache_hit=False, warm_load_s=<wall-clock>` when this call loaded it. The prompt is
      one token, so the wall-clock is load-dominated (an approximation, not `load_duration`).
    - `warm_load_s=None` when the warm call itself failed — best-effort, never raises.
    """
    if any(m.get("name") == tag for m in running_models(host_url)):
        return {"tag": tag, "cache_hit": True, "warm_load_s": 0.0}
    payload = json.dumps(
        {
            "model": tag,
            "messages": [{"role": "user", "content": "ok"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode()
    start = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{host_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except (urllib.error.URLError, OSError) as exc:
        log_warn(f"pre-warm failed for {tag} (non-fatal): {exc}")
        return {"tag": tag, "cache_hit": False, "warm_load_s": None}
    return {"tag": tag, "cache_hit": False, "warm_load_s": round(time.monotonic() - start, 3)}


def verify_responds(tag: str, *, host_url: str = DEFAULT_HOST_URL, num_ctx: int = 32000) -> bool:
    """POST /api/generate and confirm the model returns a response field.

    A transient network blip must not read as "the model can't respond": the probe is
    retried (TRANSIENT), and only a persistent failure escalates to ERROR and returns the
    negative verdict (policy §5, TRANSIENT→ERROR). A parsed-but-fieldless body is a real
    negative answer, returned without retry."""
    payload = json.dumps(
        {
            "model": tag,
            "prompt": "reply with the single word: ok",
            "stream": False,
            "options": {"num_ctx": num_ctx},
        }
    ).encode()

    def _probe() -> bool:
        req = urllib.request.Request(
            f"{host_url}/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
        return "response" in body

    try:
        return retry_transient(_probe, what=f"verify {tag} responds", retry_on=_TRANSIENT_NET)
    except (urllib.error.URLError, OSError):
        return False  # already escalated to ERROR inside retry_transient
    except json.JSONDecodeError:
        return False  # a malformed 200 body is a definitive no, not a retry-able blip


def tool_call_probe(tag: str, *, host_url: str = DEFAULT_HOST_URL, num_ctx: int = 32000) -> bool:
    """Best-effort probe: ask the model to use a tool and check for tool_calls.

    A model that cannot tool-call is unusable for ADOS agents (gemma3:1b is the
    known-bad case). A transient network blip is retried (TRANSIENT) so it isn't misread
    as a real capability failure; a persistent failure escalates to ERROR and returns the
    negative verdict (policy §5, TRANSIENT→ERROR).
    """
    payload = json.dumps(
        {
            "model": tag,
            "stream": False,
            "messages": [
                {"role": "user", "content": "What is the weather in Paris? Use the tool."}
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get the weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
            "options": {"num_ctx": num_ctx},
        }
    ).encode()

    def _probe() -> bool:
        req = urllib.request.Request(
            f"{host_url}/api/chat", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
        message = body.get("message", {})
        return bool(message.get("tool_calls"))

    try:
        return retry_transient(_probe, what=f"tool-call probe {tag}", retry_on=_TRANSIENT_NET)
    except (urllib.error.URLError, OSError):
        return False  # already escalated to ERROR inside retry_transient
    except json.JSONDecodeError:
        return False  # a malformed 200 body is a definitive no, not a retry-able blip


def lan_exposure_warning(*, port: int = 11434) -> str | None:
    """If Ollama is bound to a public interface (0.0.0.0 / *), return advice to
    rebind loopback-only, else None.

    Both sandbox backends' egress proxies run as host processes and dial the host's
    own loopback, so a loopback-only Ollama (`127.0.0.1`) is fully reachable from the
    sandbox — and is the SAFER binding: `0.0.0.0` exposes Ollama to the whole LAN.
    (Verified end-to-end on sbx + docker, 2026-07-11.) Uses `lsof`; returns None if
    lsof is unavailable or the port isn't listening (no false alarms).
    """
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except (FileNotFoundError, OSError):
        return None
    if not out.strip():
        return None
    public = (f"0.0.0.0:{port}", f"*:{port}")
    if any(p in out for p in public):
        return (
            f"Host Ollama is bound to a public interface — this exposes it to your "
            f"whole LAN. The sandbox reaches a loopback-only server through its host "
            f"proxy, so rebind safely with: OLLAMA_HOST=127.0.0.1:{port} ollama serve"
        )
    return None


def announce_lan_exposure(*, port: int = 11434) -> None:
    """Print the LAN-exposure warning if one applies (shared by sandbox + doctor)."""
    msg = lan_exposure_warning(port=port)
    if msg:
        log_info(f"[yellow]WARN[/yellow] {msg}")
