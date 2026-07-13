# sbx workarounds ledger — declared, removable, tracked

danno drives Docker's `sbx` sandbox CLI. `sbx` is experimental ("the API will
change"), and it replaced the deprecated `docker sandbox` subcommand. A few
accommodations in danno exist **only** because of the current state of those two
things. Each is a **declared workaround with a removal trigger** — not a silent hack
— so it can be deleted cleanly when the trigger fires, instead of rotting into
"overtaken-by-events" cruft.

**Find them all:** `grep -rn "SBX-TRANSITION\|SBX-WORKAROUND" src/`

| # | Workaround | Why it exists | Config knob | Remove when | Marker · files |
|---|---|---|---|---|---|
| 1 | **`docker sandbox` backend + selection** | Docker Desktop still ships the deprecated `docker sandbox`; some hosts only have it, others only `sbx`. danno supports both during the transition, defaulting to `sbx`. | `[sandbox].cli = "auto"\|"sbx"\|"docker"` (env `DANNO_SANDBOX_CLI` overrides) | `docker sandbox` is gone everywhere danno runs | `SBX-TRANSITION` · `commands/sandbox_cli.py` (the `docker` branch of every builder + `set_backend`/`resolve_backend`), `config/schema.py` (`Sandbox.cli`) |

## Retired

- **Ollama local-alias → loopback resolution** (was row 2) — **REMOVED 2026-07-11
  (plan W1).** Rested on the false premise that `sbx` lacked the
  `host.docker.internal`→`localhost` proxy rewrite; the 2026-07-10 review proved sbx
  **has** it (identical to `docker sandbox`), and the workaround's `127.0.0.1:11434`
  token actually **403'd** the documented path. Retired the whole resolver
  (`resolve_ollama_hostport`, `_LOCAL_OLLAMA_ALIASES`, `_SBX_LOOPBACK`), the
  `[sandbox].resolve_ollama_host` knob, and its tests. Both backends now use the
  default `localhost:11434` allow token for a same-host Ollama; a concrete remote host
  is allowed literally (`sbx-egress-model.md` §7). The `OpenShell#263` citation was a
  mis-cite (a different project's tracker).

## How to retire one

1. Confirm the trigger (e.g. `docker sandbox` removed everywhere) and re-run the
   boundary test: `200` to the allowed Ollama, `403` (sbx) / `500`-with-policy-body
   (legacy) to `example.com`/LAN/other host ports.
2. `grep -rn "<marker>" src/` to list every site.
3. Delete the branch/resolver + its config field + its tests + this row.

## Related

- Security invariant + the verify-by-HTTP-403 lesson: the
  `sandbox-security-contract-fail-loud` memory.
- Full migration record: [`plan-sbx-migration.md`](plan-sbx-migration.md) (its
  corrected Phase 2 is the current plan; the P3 "routable IP required" reasoning is
  refuted).
- The verified egress model + the 2026-07-10 review outcome:
  [`sbx-egress-model.md`](sbx-egress-model.md) §0.
