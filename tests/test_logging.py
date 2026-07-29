"""The Printing & Error-Reporting keystone: the `core.log` mechanism (channel split,
verbosity thresholds, the DEBUG-always file sink, the TRANSIENT level) and the FATAL
sandbox-egress guard that is its first consumer.

`core.log.err_console` and the console handler write to `sys.stderr`, resolved lazily
per write, so `capsys` captures them; `logger.propagate` is False by design, so these
tests assert on `capsys` + the file, not `caplog`."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from book_em_danno.commands import sandbox_cli
from book_em_danno.core import log as log_mod
from book_em_danno.core.exec import (
    SandboxSecurityError,
    console,
    log_debug,
    log_err,
    log_fatal,
    log_info,
    log_transient,
    log_warn,
)


@pytest.fixture(autouse=True)
def _restore_default_logging() -> Iterator[None]:
    """Each test reconfigures freely; restore danno's import-time defaults afterward so
    a verbosity/log-file choice never leaks into the next test."""
    yield
    log_mod.configure_logging()


def test_leveled_output_goes_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    log_warn("disk almost full")
    log_err("upload failed")
    log_fatal("egress breach")
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is the data channel — nothing leveled here
    assert "[WARNING] disk almost full" in captured.err
    assert "[ERROR] upload failed" in captured.err
    assert "[FATAL] egress breach" in captured.err


def test_data_product_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    console.print("danno 1.2.3")
    captured = capsys.readouterr()
    assert "danno 1.2.3" in captured.out
    assert captured.err == ""


def test_message_body_brackets_are_literal(capsys: pytest.CaptureFixture[str]) -> None:
    # A bracketed path/IP in the body must survive rich's markup parser verbatim.
    log_err("denied host [::1] path=/a/[b] [lan]")
    err = capsys.readouterr().err
    assert "[::1]" in err
    assert "/a/[b]" in err
    assert "[lan]" in err


def test_quiet_hides_info_shows_warning(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging(verbosity=log_mod.Verbosity.QUIET)
    log_info("provisioning step")
    log_warn("heads up")
    err = capsys.readouterr().err
    assert "provisioning step" not in err
    assert "heads up" in err


def test_default_shows_info_hides_debug(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging(verbosity=log_mod.Verbosity.DEFAULT)
    log_info("provisioning step")
    log_debug("inner detail")
    err = capsys.readouterr().err
    assert "provisioning step" in err
    assert "inner detail" not in err


def test_verbose_shows_debug(capsys: pytest.CaptureFixture[str]) -> None:
    log_mod.configure_logging(verbosity=log_mod.Verbosity.VERBOSE)
    log_debug("inner detail")
    assert "inner detail" in capsys.readouterr().err


def test_file_sink_captures_debug_and_up_even_at_info_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "logs" / "danno.log"  # parent is created by configure_logging
    log_mod.configure_logging(verbosity=log_mod.Verbosity.DEFAULT, log_file=log_file)
    log_debug("low-level trace")
    log_info("lifecycle event")
    log_err("something failed")

    console_err = capsys.readouterr().err
    assert "low-level trace" not in console_err  # console at INFO hides DEBUG...

    contents = log_file.read_text(encoding="utf-8")
    assert "[DEBUG] low-level trace" in contents  # ...but the file keeps everything
    assert "[INFO] lifecycle event" in contents
    assert "[ERROR] something failed" in contents


def test_transient_level_registered(capsys: pytest.CaptureFixture[str]) -> None:
    assert log_mod.TRANSIENT == 35
    assert logging.getLevelName(log_mod.TRANSIENT) == "TRANSIENT"
    log_transient("intermittent DNS blip")
    assert "[TRANSIENT] intermittent DNS blip" in capsys.readouterr().err


@pytest.mark.parametrize("allow_hosts", [(), ("**",), ("*",), ("",), ("   ",), ("ok", "**")])
def test_egress_guard_rejects_open_allow_lists(allow_hosts: tuple[str, ...]) -> None:
    with pytest.raises(SandboxSecurityError):
        sandbox_cli.policy_allow_argv("dev", allow_hosts)


def test_egress_guard_passes_explicit_host() -> None:
    # conftest pins DANNO_SANDBOX_CLI=docker; assert the host reaches the argv.
    argv = sandbox_cli.policy_allow_argv("dev", ("localhost:11434",))
    assert "localhost:11434" in argv
