# Codex integration — Phase-0 live spike findings

Live spike run **2026-07-18** in a throwaway Docker sandbox (legacy `docker sandbox`
v0.12.0; no `sbx` on this host), against **host Ollama 0.30.6** exposing `/v1/responses`.
Answers the four Phase-0 unknowns from `.docs/plan-harness-api.md` §5 before the codex
harness module is written. **All four resolved; codex wires exactly like claurst
(relay-free).**

Spike sandbox: `danno-codex-spike` (image `shell`), egress
`--policy allow --allow-host localhost:11434` (danno's existing default). `codex-cli
0.144.5`, installed via `npm install -g @openai/codex` (proxy-aware, ~5s).

## Prerequisites (verified)

- **Ollama ≥ 0.13.3** for `/v1/responses`: host has **0.30.6** (well above). A `POST
  /v1/responses` with an empty body returns **400** (endpoint exists, body rejected), not
  404 — the reachability probe. Doctor/bench pre-flight should probe the endpoint (or parse
  `ollama --version`), failing loud below 0.13.3.
- Node/npm present in the `shell` image (node v22, npm 9). `npm i -g @openai/codex` works
  through the egress proxy.

## Q1 — Proxy reach: **RELAY-FREE** (like claurst, NOT like occ)

Codex (Rust) **honors `HTTPS_PROXY`/`HTTP_PROXY`** and reaches host Ollama at
`http://host.docker.internal:11434/v1` directly through the sandbox egress proxy (the proxy
rewrites `host.docker.internal`→host `localhost`; allow-host `localhost:11434` covers it).
**No in-VM relay bracket needed** — codex uses the same relay-free wiring as claurst
(`CLAURST_RELAY_FREE_OLLAMA_HOST`), and under `--capture` its base_url points at the
recording proxy the same way.

Sandbox env confirmed: `https_proxy=http://host.docker.internal:3128`,
`no_proxy=localhost,127.0.0.1,::1`, proxy CA at `/usr/local/share/ca-certificates/
proxy-ca.crt`. Because `localhost`/`127.0.0.1` are in `no_proxy`, a `base_url` of
`localhost:11434` bypasses the proxy and hits the container (→ connection refused).
**`base_url` MUST be `host.docker.internal:11434`, never localhost.**

### config.toml (the file form — chosen for parity with the other config-gen harnesses)

```toml
model = "gpt-oss:20b"
model_provider = "ollama-danno"          # NOT "ollama" (see gotcha)

[model_providers.ollama-danno]
name = "Ollama (host via danno proxy)"
base_url = "http://host.docker.internal:11434/v1"
wire_api = "responses"
# env_key = "NVIDIA_API_KEY"             # cloud only; omit for local no-auth (worked)
```

- **GOTCHA (blocking): `ollama` is a RESERVED built-in provider ID** — codex refuses to
  override it (`model_providers contains reserved built-in provider IDs: 'ollama'`). The
  built-in `ollama` provider defaults to `localhost:11434`, which is unreachable from the
  sandbox (no_proxy bypass) and can't be redirected. **`generate_codex_config` must name the
  provider something else** (spike used `ollama-danno`). `model_provider` + the dial `-m` ref
  key on the custom id.
- `CODEX_HOME` is set per-session to a VM-local dir (spike used `~/codex-home`). Avoid `/tmp`
  — codex warns it refuses to create PATH-alias helper binaries under a temp `CODEX_HOME`
  (non-fatal, but noisy). Sessions/rollouts persist under `$CODEX_HOME/sessions/YYYY/MM/DD/`.

## Q2 — CodexTurn event schema (`codex exec --json` → NDJSON on stdout)

Working headless argv (0.144.5):

```
codex exec --json -s danger-full-access --skip-git-repo-check -C <workspace> -m <tag> "<prompt>" </dev/null
```

- The plan's `-a never` is **stale**: `exec` no longer has `-a/--ask-for-approval` (it's
  non-interactive by definition). Use `-s danger-full-access` (or
  `--dangerously-bypass-approvals-and-sandbox` for full autonomy — the outer Docker sandbox
  is the isolation boundary, same rationale as occ's old `bypassPermissions`).
- **Redirect stdin from `/dev/null`** — with a piped-but-open stdin codex prints "Reading
  additional input from stdin..." and can block; the driver must close it.

One clean turn emitted **8 NDJSON events**:

| `type`            | payload                                                                 |
|-------------------|-------------------------------------------------------------------------|
| `thread.started`  | `{thread_id}`                                                           |
| `turn.started`    | `{}`                                                                     |
| `item.started`    | `{item}` — first sighting of an item                                     |
| `item.completed`  | `{item}` — terminal state of an item                                     |
| `turn.completed`  | `{usage}` — see Q4                                                       |

`item.type` values seen:
- `reasoning` → `{id, text}` (chain-of-thought; may be dropped from transcript)
- `command_execution` → `{id, command, aggregated_output, exit_code, status}` — the executed
  shell (codex's abstraction over the tool call). `status`: `in_progress`→`completed`.
- `agent_message` → `{id, text}` — **assistant final text** (parser's primary text source).
- `error` → `{message}` — **non-fatal** (e.g. "Model metadata for `gpt-oss:20b` not found.
  Defaulting to fallback metadata…"). The `CodexTurn` parser MUST treat `item.type=="error"`
  as a warning, not a turn failure (only a `turn.failed`/non-zero exit is a failure).

**CodexTurn parser plan:** assistant text = last `agent_message.text`; tool calls =
`command_execution` items; round/stop = `turn.completed`; usage from `turn.completed.usage`.

## Q3 — Loop tool for the gate fixture: **`exec_command`**

On the wire (rollout `response_item`), the model's tool call is
`function_call {name:"exec_command", call_id, arguments}` with arguments:

```json
{"cmd":"echo -n banana > hello.txt","shell":"bash","workdir":"/tmp/codex-ws",
 "login":true,"max_output_tokens":1000,"justification":"","prefix_rule":[],
 "sandbox_permissions":"use_default","tty":false,"yield_time_ms":250}
```

matched by `function_call_output {call_id, output}`. So for `tests/slow/gates_fixtures.py`
the codex row's **`LOOP_TOOL = "exec_command"`** with `LOOP_TOOL_ARGS` carrying a `cmd`
(today opencode uses `bash`). The stub AI must advertise/return `exec_command`.

## Q4 — Usage populates on `turn.completed`: **YES**

```json
"usage": {"input_tokens": 11855, "cached_input_tokens": 0,
          "output_tokens": 146, "reasoning_output_tokens": 0}
```

Real non-zero totals against Ollama's experimental `/v1/responses`. `capture/usage.py`
already normalizes Responses `input_tokens`/`output_tokens`, so round + token telemetry
should work out of the box; live-verify the headroom column in Phase 3.

## Q — RESPONSES history shape (for the #97 wire-well-formedness assertion, §4.1)

The rollout confirms the `input[]` item shape the §4.1 extractor must handle for
`WireProtocol.RESPONSES`:
- `message` `{role, content}`
- `reasoning` `{id, summary, content, encrypted_content}`
- `function_call` `{name, call_id, arguments}`
- `function_call_output` `{call_id, output}`

Assertion (RESPONSES branch): each `function_call_output.call_id` resolves to a preceding
`function_call.call_id`, and at least one assistant `message`/`reasoning` item is present —
the direct analog of the CHAT `tool_calls[].id ↔ tool_call_id` check.

## Deferred / polish

- Silence the "Model metadata not found" warning by declaring model metadata (context
  window) in config — optional; non-fatal.
- Cloud codex (NVIDIA/OpenAI-compat): add `env_key` to the provider block + inject the key
  via the chmod-600 env-file, mirroring claurst's cloud path. Not spiked (local was the
  Phase-0 target); wire it in Phase 3 alongside `cloud_env_lines`.
