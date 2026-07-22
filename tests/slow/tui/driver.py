"""`TuiDriver` — the frozen host-pty contract the tests are written against.

One tiny protocol, three backends behind it (`.docs/plan-slow-sandbox-tui-tests.md` §4/§8.1):

- **`PexpectDriver`** (POSIX — macOS/Linux/WSL2): the P1 deliverable. `pexpect.spawn` of the
  real `sbx exec -it … <argv>` under a Unix pty, raw bytes → `pyte.ByteStream`/`pyte.Screen`.
- **`WinPtyDriver`** (Windows — cmd/PowerShell): a P1 STUB (`raise NotImplementedError`) so the
  Windows lane has a compile target; filled in at P2 (pywinpty/ConPTY, §3.4).
- **`TmuxDriver`** (documented fallback): only via `make_driver(prefer="tmux")`, never a silent
  downgrade. Not written in P1 — `make_driver` fails loud if asked for it.

`make_driver` picks the host-pty backend by platform and raises `DriverUnavailable` (→ a loud
`pytest.skip`) when that backend can't import or can't drive sbx. It NEVER silently downgrades
to tmux — a reduced-fidelity run happens only when a human explicitly passes `prefer="tmux"`.

The primitives (`settle_and_dismiss`, `submit`, `one_shot_inflate`) operate on this protocol,
never on a pty library directly, so they are backend-agnostic. Freezing this surface is the P1
exit gate: the Windows Claude implements `WinPtyDriver` against it without touching shared code.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

# Spike geometry: a wide-enough terminal that the harness banners/composers/modals render on
# one screen (matched on stable markers after a settle, not transient paint — §3.4 R5).
COLS, ROWS = 160, 48


class DriverUnavailable(RuntimeError):
    """The platform's host-pty backend can't import or can't drive sbx. The caller turns this
    into a loud `pytest.skip` — never a silent pass and never a silent tmux downgrade."""


@runtime_checkable
class TuiDriver(Protocol):
    """The full surface the A/H/C tests use. Coordinates are (rows, cols); `screen()` is the
    rendered display text. FROZEN at the P1 exit gate — the Windows backend builds against it."""

    def start(self) -> None:
        """Spawn `sbx exec -it … <argv>` under a host pty."""
        ...

    def pump(self, seconds: float, want: Sequence[str] | None = None) -> bool:
        """Feed pty output into the emulator for `seconds`; return True early if any `want`
        marker (case-insensitive substring) is on the rendered screen."""
        ...

    def screen(self) -> str:
        """The current rendered screen text."""
        ...

    def send(self, keys: str) -> bool:
        """Write text/control bytes to the pty; False if the child is dead."""
        ...

    def enter(self) -> bool:
        """Convenience: submit (send a carriage return)."""
        ...

    def alive(self) -> bool:
        """Whether the child process is still running."""
        ...

    def close(self) -> None:
        """Force-terminate the frame; never raise (EOF on quit is expected)."""
        ...


def _wants(want: Sequence[str] | None) -> list[str]:
    if want is None:
        return []
    if isinstance(want, str):
        return [want.lower()]
    return [w.lower() for w in want]


class PexpectDriver:
    """POSIX host-pty driver (macOS/Linux/WSL2). Spawns the real `sbx exec -it … <argv>` under a
    Unix pty and feeds raw bytes to `pyte`, matching the rendered screen. This is the
    spike-proven path (all three harnesses A/H/C green on macOS)."""

    def __init__(self, exe: str, args: list[str], env: dict[str, str]) -> None:
        # Already-resolved launch tuple (make_driver / launch_argv did the resolution).
        self._exe = exe
        self._args = args
        self._env = env
        self._child: object | None = None
        self._screen: object | None = None
        self._stream: object | None = None

    def start(self) -> None:
        import pexpect
        import pyte

        self._screen = pyte.Screen(COLS, ROWS)
        self._stream = pyte.ByteStream(self._screen)
        # encoding=None → raw bytes into ByteStream (partial-multibyte-safe, §3.4). timeout is a
        # ceiling for a single blocking op; pump() drives with a short read_nonblocking window.
        self._child = pexpect.spawn(
            self._exe,
            args=self._args,
            env=self._env,
            encoding=None,
            dimensions=(ROWS, COLS),
            timeout=120,
        )

    def pump(self, seconds: float, want: Sequence[str] | None = None) -> bool:
        import pexpect

        wants = _wants(want)

        def hit() -> bool:
            s = self.screen().lower()
            return bool(wants and any(w in s for w in wants))

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data = self._child.read_nonblocking(8192, timeout=0.4)  # type: ignore[union-attr]
            except pexpect.TIMEOUT:
                data = b""
            except pexpect.EOF:
                # EOF is EXPECTED on quit and on the opencode update-restart — never an
                # exception the test sees. Stop early, reporting the current hit-state.
                return hit()
            if data:
                self._stream.feed(data)  # type: ignore[union-attr]
            if hit():
                return True
        return hit()

    def screen(self) -> str:
        if self._screen is None:
            return ""
        return "\n".join(self._screen.display).rstrip()  # type: ignore[union-attr]

    def send(self, keys: str) -> bool:
        child = self._child
        try:
            if child is None or not child.isalive():  # type: ignore[union-attr]
                return False
            child.send(keys)  # type: ignore[union-attr]
            return True
        except OSError:
            return False

    def enter(self) -> bool:
        return self.send("\r")

    def alive(self) -> bool:
        child = self._child
        try:
            return bool(child is not None and child.isalive())  # type: ignore[union-attr]
        except OSError:
            return False

    def close(self) -> None:
        child = self._child
        if child is None:
            return
        # Best-effort graceful quit (Ctrl-C ×2 then 'q'), then force-close. All swallowed:
        # the child may already be gone (EOF), which must never surface as a teardown error.
        try:
            child.sendcontrol("c")  # type: ignore[union-attr]
            time.sleep(0.4)
            child.sendcontrol("c")  # type: ignore[union-attr]
            child.send("q")  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            child.close(force=True)  # type: ignore[union-attr]
        except Exception:
            pass


class WinPtyDriver:
    """Windows host-pty driver (cmd/PowerShell via pywinpty/ConPTY).

    P1 STUB — a compile target for the Windows lane; implemented at P2 against this frozen
    protocol (`.docs/plan-slow-sandbox-tui-tests.md` §3.4). It will spawn the SAME
    `sbx exec -it … <argv>` under a ConPTY (`winpty.PtyProcess.spawn(..., dimensions=(ROWS,
    COLS))`), decode UTF-8 `str` into a `pyte.Screen` (via `pyte.Stream`, not `ByteStream`),
    and read non-blocking via the §3.4 levers. Every primitive on top stays unchanged.
    """

    def __init__(self, exe: str, args: list[str], env: dict[str, str]) -> None:
        self._exe = exe
        self._args = args
        self._env = env

    def start(self) -> None:
        raise NotImplementedError("WinPtyDriver is implemented in P2 (pywinpty/ConPTY, plan §3.4)")

    def pump(self, seconds: float, want: Sequence[str] | None = None) -> bool:
        raise NotImplementedError("WinPtyDriver: P2")

    def screen(self) -> str:
        raise NotImplementedError("WinPtyDriver: P2")

    def send(self, keys: str) -> bool:
        raise NotImplementedError("WinPtyDriver: P2")

    def enter(self) -> bool:
        raise NotImplementedError("WinPtyDriver: P2")

    def alive(self) -> bool:
        raise NotImplementedError("WinPtyDriver: P2")

    def close(self) -> None:
        # A stub close must never raise (it runs in test teardown).
        return None


def make_driver(argv: list[str], env: dict[str, str], *, prefer: str = "auto") -> TuiDriver:
    """The host-pty driver for this platform, or a loud `DriverUnavailable`.

    - `prefer="auto"`: `PexpectDriver` on POSIX, `WinPtyDriver` on Windows.
    - `prefer="tmux"`: the in-VM fallback — NOT written in P1, so this fails loud (rather than
      silently downgrading). Implement `TmuxDriver` when a platform genuinely needs it (§8.1).

    Raises `DriverUnavailable(reason)` when the platform's host-pty library can't import; the
    caller turns that into `pytest.skip`. `argv[0]` is the executable, `argv[1:]` its args.
    """
    if prefer == "tmux":
        raise DriverUnavailable(
            "tmux fallback not implemented in P1 (plan §8.1); implement TmuxDriver when a "
            "platform needs it — never a silent downgrade"
        )
    if prefer != "auto":
        raise DriverUnavailable(f"unknown driver preference {prefer!r}")

    exe = shutil.which(argv[0]) or argv[0]
    args = list(argv[1:])

    if os.name == "posix":
        try:
            import pexpect  # noqa: F401
            import pyte  # noqa: F401
        except ImportError as e:
            raise DriverUnavailable(
                f"POSIX host-pty backend unavailable (need pexpect + pyte): {e}"
            ) from e
        return PexpectDriver(exe, args, env)

    if os.name == "nt":
        try:
            import pyte  # noqa: F401
            import winpty  # noqa: F401
        except ImportError as e:
            raise DriverUnavailable(
                f"Windows host-pty backend unavailable (need pywinpty + pyte): {e}"
            ) from e
        return WinPtyDriver(exe, args, env)

    raise DriverUnavailable(f"no host-pty driver for os.name={os.name!r}")
