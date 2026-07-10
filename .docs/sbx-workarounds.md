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
| 2 | **Ollama local-alias → loopback resolution** | `sbx` has no `host.docker.internal`→`localhost` rewrite (docker sandbox had one). A **same-host** Ollama is reached at `127.0.0.1:port` **through the host-side proxy** (verified 2026-07-09: allowed→200, unallowed→403, other-port→403) — network-independent (no LAN IP, no VPN, works offline). So a local Ollama alias resolves to `127.0.0.1`; a concrete/remote host is used literally. **Phase-2 companion (not yet done):** the harness must route `127.0.0.1` through the proxy (drop it from `NO_PROXY`); claurst keeps its own in-sandbox `127.0.0.1` relay. | `[sandbox].resolve_ollama_host = true` | `sbx` routes `host.docker.internal` egress ([NVIDIA/OpenShell#263](https://github.com/NVIDIA/OpenShell/issues/263)) | `SBX-WORKAROUND(OpenShell#263)` · `commands/sandbox.py` (`resolve_ollama_hostport`, `_LOCAL_OLLAMA_ALIASES`, `configure_proxy`), `config/schema.py` (`Sandbox.resolve_ollama_host`) |

## How to retire one

1. Confirm the trigger (e.g. `docker sandbox` removed, or `sbx` fixes #263 — re-run
   the boundary test: `200` to the allowed Ollama, `403` to `example.com`/LAN).
2. `grep -rn "<marker>" src/` to list every site.
3. Delete the branch/resolver + its config field + its tests + this row.

## Related

- Security invariant + the verify-by-HTTP-403 lesson: the
  `sandbox-security-contract-fail-loud` memory.
- Full migration record: [`plan-sbx-migration.md`](plan-sbx-migration.md).
- The reachability reasoning (why loopback/host-alias don't work; why a routable IP
  is required): that plan's P3 section.
