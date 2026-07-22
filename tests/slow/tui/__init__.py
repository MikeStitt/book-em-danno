"""Interactive `@slow @sandbox` TUI-launch tests.

Proves `danno sandbox start` reaches a real harness TUI, drives a model turn, and
reaches a compaction decision — asserting against the captured HTTP wire ("wire, not
paint"), driven through a host pty (`pexpect` on POSIX, `pywinpty` on Windows) against
the real `sbx exec -it … <argv>` frame. Design of record:
`.docs/plan-slow-sandbox-tui-tests.md`.
"""
