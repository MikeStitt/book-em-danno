"""Resource-bounded Ollama `/v1` smoke test for 16 GiB development machines.

This complements, rather than replaces, ``test_ollama_v1.py``. That contract suite
intentionally loads ``gemma4:26b`` at its full 262K context and is unsuitable for a
16 GiB host. This smoke test uses the approximately 726 MB ``smollm2:360m`` model
with its 8K context window and checks only the model-independent OpenAI-compatible
response shape.

The test never pulls a model. It skips unless the exact small tag is already present,
and it unloads the model afterward when it was not already loaded before the test.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import warnings
from typing import Any

import pytest

from book_em_danno.commands import ollama

pytestmark = pytest.mark.slow

MODEL = "smollm2:360m"
HOST = ollama.DEFAULT_HOST_URL
MAX_MODEL_BYTES = 1_000_000_000
MAX_CONTEXT_LENGTH = 8_192


def _model_info(tag: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=2.0) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return next((model for model in body.get("models", []) if model.get("name") == tag), None)


def _loaded_model_info(tag: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{HOST}/api/ps", timeout=2.0) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return next((model for model in body.get("models", []) if model.get("name") == tag), None)


model_info = _model_info(MODEL)
model_absent = model_info is None
model_too_large = bool(model_info and model_info.get("size", 0) > MAX_MODEL_BYTES)

skip_unavailable = pytest.mark.skipif(
    not ollama.reachable() or model_absent or model_too_large,
    reason=(
        f"Ollama unreachable, {MODEL} not pulled, or its local artifact exceeds "
        f"{MAX_MODEL_BYTES // 1_000_000} MB"
    ),
)


def _chat() -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
            "stream": False,
            "temperature": 0,
            "max_tokens": 16,
        }
    ).encode()
    request = urllib.request.Request(
        f"{HOST}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as resp:
        return json.loads(resp.read())


def _unload() -> None:
    """Use Ollama's native keep_alive=0 mechanism to release test-owned memory."""
    payload = json.dumps(
        {
            "model": MODEL,
            "prompt": "",
            "stream": False,
            "keep_alive": 0,
        }
    ).encode()
    request = urllib.request.Request(
        f"{HOST}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        resp.read()


@skip_unavailable
def test_v1_small_model_chat_completion_smoke() -> None:
    already_loaded = _loaded_model_info(MODEL) is not None
    try:
        body = _chat()

        assert body["object"] == "chat.completion"
        assert body["model"]
        assert body["choices"]

        message = body["choices"][0]["message"]
        assert message["role"] == "assistant"
        assert message["content"].strip()

        usage = body["usage"]
        assert usage["prompt_tokens"] > 0
        assert 0 < usage["completion_tokens"] <= 16

        loaded = _loaded_model_info(MODEL)
        assert loaded is not None
        assert loaded["context_length"] <= MAX_CONTEXT_LENGTH
    finally:
        if not already_loaded:
            try:
                _unload()
            except (urllib.error.URLError, OSError):
                warnings.warn(f"failed to unload test-owned Ollama model {MODEL}", stacklevel=2)
