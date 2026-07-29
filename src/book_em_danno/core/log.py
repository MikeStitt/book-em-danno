"""Central logging — the standard reporting mechanism the Printing & Error-Reporting
policy requires (`.specify/memory/parts/printing-and-error-reporting.md`).

One `logging.Logger` named ``danno`` fans out to a **stderr** console handler (the
live log channel) and, when a ``--log-file`` is set, a **file** handler (the durable
mirror). Callers pass PLAIN message text; each handler's formatter adds the
``[LEVEL]`` tag — coloured rich markup on the console, plain greppable text in the
file — and the message body is markup-escaped so a bracket in a path/IP (``[::1]``)
is never eaten by rich's markup parser.

Severity is the stdlib/syslog set plus danno's two policy tiers: ``TRANSIENT`` (an
intermittent, retry-safe failure that a caller MUST escalate to ERROR once its retry
budget is spent) and ``FATAL`` (an alias for ``CRITICAL``, displayed as ``FATAL``).
**stdout stays the data channel** and is never written here — that is
``core.exec.console``.
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape

# danno-specific severities layered onto the stdlib levels (DEBUG=10 … CRITICAL=50).
TRANSIENT = 35  # between WARNING (30) and ERROR (40): retry-safe, escalates to ERROR
FATAL = logging.CRITICAL  # 50; displayed as "FATAL"
logging.addLevelName(TRANSIENT, "TRANSIENT")
logging.addLevelName(logging.CRITICAL, "FATAL")

logger = logging.getLogger("danno")

# Non-leveled stderr chrome — the advise ``$ cmd`` echo and the validator's rich
# progress UI. stderr so it never pollutes the stdout data channel, but with no
# ``[LEVEL]`` tag. Console(stderr=True) resolves sys.stderr lazily on each write, so
# a test's capsys still captures it.
err_console = Console(stderr=True)


class Verbosity(Enum):
    """Console log threshold. The file sink always captures DEBUG-and-up regardless."""

    QUIET = "quiet"  # WARNING and up
    DEFAULT = "default"  # INFO and up
    VERBOSE = "verbose"  # DEBUG and up


_CONSOLE_LEVEL = {
    Verbosity.QUIET: logging.WARNING,
    Verbosity.DEFAULT: logging.INFO,
    Verbosity.VERBOSE: logging.DEBUG,
}

# rich markup style per level for the console handler; unlisted levels (INFO) render
# the tag with no colour.
_LEVEL_STYLE = {
    logging.DEBUG: "dim",
    logging.WARNING: "yellow",
    TRANSIENT: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bold red",
}

# Marks the handlers this module installs so ``configure_logging`` can replace only
# its own (never a caplog/pytest handler someone else attached).
_MANAGED = "_danno_managed"


class _ConsoleFormatter(logging.Formatter):
    """``[LEVEL] msg`` with a colour-styled tag; the message body is markup-escaped so
    brackets in it are shown literally rather than parsed as rich markup."""

    def format(self, record: logging.LogRecord) -> str:
        tag = escape(f"[{record.levelname}]")
        body = escape(record.getMessage())
        style = _LEVEL_STYLE.get(record.levelno)
        return f"[{style}]{tag}[/{style}] {body}" if style else f"{tag} {body}"


def _clear_managed_handlers() -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _MANAGED, False):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(
    *, verbosity: Verbosity = Verbosity.DEFAULT, log_file: Path | None = None
) -> None:
    """Install danno's log handlers. **Idempotent** — replaces any handlers this module
    previously installed, so repeated CLI entry (and every test) never stacks duplicates.

    The logger sits at DEBUG; the CONSOLE handler is filtered by ``verbosity`` while the
    FILE handler always captures DEBUG-and-up, so a failure stays diagnosable from the
    file without re-running under ``-v`` (policy §File logging / §Verbosity).
    """
    _clear_managed_handlers()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # danno owns its output; don't double-log via the root

    console_handler = RichHandler(
        console=err_console,
        show_time=False,
        show_level=False,
        show_path=False,
        markup=True,
        rich_tracebacks=False,
    )
    console_handler.setLevel(_CONSOLE_LEVEL[verbosity])
    console_handler.setFormatter(_ConsoleFormatter())
    setattr(console_handler, _MANAGED, True)
    logger.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # the durable log keeps everything
        file_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        setattr(file_handler, _MANAGED, True)
        logger.addHandler(file_handler)


def log_info(msg: str) -> None:
    logger.info(msg)


def log_warn(msg: str) -> None:
    logger.warning(msg)


def log_transient(msg: str) -> None:
    """A retry-safe intermittent failure. Logged now at TRANSIENT; the caller MUST
    escalate to :func:`log_err` once its retry/threshold budget is exhausted (a
    TRANSIENT with no bound is a swallowed error — policy §TRANSIENT)."""
    logger.log(TRANSIENT, msg)


def log_err(msg: str) -> None:
    logger.error(msg)


def log_fatal(msg: str) -> None:
    """An unrecoverable precondition or security-invariant violation; the command MUST
    abort after this (policy §FATAL)."""
    logger.critical(msg)


def log_debug(msg: str, *, verbose: bool = False) -> None:
    """DEBUG-level log. The ``verbose`` keyword is kept for source-compatibility with
    existing callers (``verbose=self.verbose``) but is now inert — the global console
    verbosity set by :func:`configure_logging` gates DEBUG."""
    logger.debug(msg)


# Install sane defaults at import so `log_*` work before the CLI callback runs (and in
# tests that don't call configure_logging). The CLI reconfigures idempotently from flags.
configure_logging()
