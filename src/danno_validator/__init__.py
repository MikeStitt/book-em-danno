"""`danno_validator` — host-side harness that exercises danno.toml configs.

A sibling package to `book_em_danno` (reusing its config loader/generator and the
two-mode `Runner`), it provisions each candidate config in the Docker sandbox,
drives a tiered test battery against the agent-under-test, judges results, and
emits a Sphinx report plus an annotated "menu" danno.toml. Design of record:
`.docs/plan-danno-validator.md`.

The opencode-only-in-sandbox invariant holds: the agent-under-test runs only in
the VM; this harness and the judge run on the host. M0 ships the headless
primitives (`danno_validator.driver`); heavier deps (Anthropic SDK, Sphinx) live
behind the `danno[validator]` extra and arrive with later milestones.
"""
