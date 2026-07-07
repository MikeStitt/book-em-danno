"""Benchmark-task suites for the validator.

Distinct from `danno_validator.benchmark` (which benchmarks whole opencode
*configs* for editing performance): a *suite* feeds real industry-style SWE
benchmark *tasks* (Aider Polyglot exercises, a SWE-bench Verified subset) into the
harness-under-test, reusing the Level-2 `seed -> run -> grade` contract and the
harness-agnostic oracle. The harness axis (which HUT) and the task axis (which suite)
are orthogonal; a suite runs against whichever HUT the sweep/baseline selected.

We run real benchmark task *content* via danno's own execution model (a headless
turn in a disposable sandbox, graded by the task's own tests) — NOT the official
Docker-per-task harness, so we never report an official "SWE-bench Verified score".
"""
