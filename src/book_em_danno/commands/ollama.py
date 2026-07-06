"""Ollama model management (internal; orchestrated by `install`).

Ports `scripts/setup-ollama.sh`. Model pulls go through the Runner (advise by
default); reachability and verification are read-only HTTP probes used by the
slow tests and by `--apply` runs. Uses stdlib `urllib` — no new dependency.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request

from ..core.exec import Runner, log_info

DEFAULT_HOST_URL = "http://localhost:11434"


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


def ensure_model(runner: Runner, tag: str, *, host_url: str = DEFAULT_HOST_URL) -> list[str]:
    """Advise (and under --apply, run) `ollama pull <tag>`. `ollama pull` is itself
    idempotent — an already-present model is a fast no-op."""
    return runner.advise(["ollama", "pull", tag], why=f"ensure Ollama model present: {tag}")


def verify_responds(tag: str, *, host_url: str = DEFAULT_HOST_URL, num_ctx: int = 32000) -> bool:
    """POST /api/generate and confirm the model returns a response field."""
    payload = json.dumps(
        {
            "model": tag,
            "prompt": "reply with the single word: ok",
            "stream": False,
            "options": {"num_ctx": num_ctx},
        }
    ).encode()
    try:
        req = urllib.request.Request(
            f"{host_url}/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
        return "response" in body
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def tool_call_probe(tag: str, *, host_url: str = DEFAULT_HOST_URL, num_ctx: int = 32000) -> bool:
    """Best-effort probe: ask the model to use a tool and check for tool_calls.

    A model that cannot tool-call is unusable for ADOS agents (gemma3:1b is the
    known-bad case). Returns False on any failure — caller decides severity.
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
    try:
        req = urllib.request.Request(
            f"{host_url}/api/chat", data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
        message = body.get("message", {})
        return bool(message.get("tool_calls"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False


def loopback_warning(*, port: int = 11434) -> str | None:
    """If Ollama is bound to 127.0.0.1 only, return the fix advice, else None.

    A loopback-bound server is unreachable from the sandbox VM; the fix is to
    rebind to 0.0.0.0. Uses `lsof`; returns None if lsof is unavailable or the
    port isn't listening (no false alarms).
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
    bound = f"127.0.0.1:{port}"
    public = (f"0.0.0.0:{port}", f"*:{port}")
    if bound in out and not any(p in out for p in public):
        return (
            f"Host Ollama is bound to {bound} only — the sandbox VM cannot reach it. "
            f"Restart it with: OLLAMA_HOST=0.0.0.0:{port} ollama serve"
        )
    return None


def announce_loopback(*, port: int = 11434) -> None:
    """Print the loopback warning if one applies (shared by sandbox + doctor)."""
    msg = loopback_warning(port=port)
    if msg:
        log_info(f"[yellow]WARN[/yellow] {msg}")
