"""An UNPARSEABLE streaming usage chunk is a different fact from a well-formed chunk that
simply carries no usage: the former is a genuine anomaly (a corrupt stream would otherwise
silently report zero tokens), so it WARNs before being ignored; the latter is silent
(policy §5, WARNING). A parseable stream still extracts usage normally."""

from __future__ import annotations

import pytest

from book_em_danno.capture.usage import extract_usage


def test_unparseable_sse_chunk_warns_and_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    body = 'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}\ndata: {not json\n'
    usage = extract_usage(body)
    assert usage == {"prompt": 3, "completion": 4, "total": None, "cached": None}
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "unparseable SSE usage chunk" in err


def test_unparseable_ndjson_line_warns_and_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    body = '{"eval_count": 5, "prompt_eval_count": 2}\n{broken ndjson\n'
    usage = extract_usage(body)
    assert usage == {"prompt": 2, "completion": 5, "total": 7, "cached": None}
    assert "unparseable NDJSON usage line" in " ".join(capsys.readouterr().err.split())


def test_wellformed_usageless_chunk_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    # A valid chunk that carries no usage legitimately returns None — no anomaly, no warning.
    body = 'data: {"choices": [{"delta": {"content": "hi"}}]}\n'
    assert extract_usage(body) is None
    assert capsys.readouterr().err == ""
