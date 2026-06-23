# Plannotator in the Docker sandbox — research, recommendation, plan

> **Living document.** Research + design captured 2026-06-20 on whether/how
> Plannotator's interactive plan review can work for OpenCode running inside
> danno's `docker sandbox` microVM, the impact on the agents/cloud refactor, a
> `docker sandbox` vs plain `docker` comparison, and a phased plan to implement a
> danno-native exec/socat tunnel **(not yet built — "we may implement later")**.
> `.docs/` is **exempt from markdown/format quality checks**. External facts
> (plannotator env vars, `docker sandbox` CLI surface) verified 2026-06-20
> against plannotator v0.21.0 / the `@plannotator/opencode` repo and the local
> Docker Desktop `docker sandbox` CLI; **re-verify before relying** — both move
> fast. Companions: [`agents-and-cloud-backend-refactor.md`](agents-and-cloud-backend-refactor.md),
> [`user-experience-elephant.md`](user-experience-elephant.md),
> [`connecting-claude-code-to-models-investigation.md`](connecting-claude-code-to-models-investigation.md).

## TL;DR

- **An egress hole does NOT make plan review work.** Plannotator's review UI is a
  **local web server inside the sandbox VM**; the *host* browser must reach it.
  That needs **ingress port-forwarding (host→VM)**, and `docker sandbox` exposes
  **no port-publish mechanism** (`create` has no `-p`; `network` is egress-only).
  Our `--allow-host` lever is the wrong axis. Plannotator's core flow needs no
  egress anyway ("plans never leave your machine").
- **Best path that keeps the sandbox: a stdio tunnel** — host `socat` ⇄
  `docker sandbox exec -i <vm> nc 127.0.0.1 <port>` ⇄ the in-VM server. Gives the
  full interactive round-trip without weakening the egress proxy. Hinges on two
  **unverified** risks (exec stdio must be 8-bit clean; websocket/multi-conn
  behaviour).
- **Do NOT switch to plain `docker` just for this.** You'd trade away microVM
  isolation, maintained agent images, and the turnkey egress proxy — danno's core
  value — to fix one peripheral dev tool. Switch only if host-reachable services
  become a *central, recurring* need.
- **Agent-layer impact is small but real:** the plugin injects a `submit_plan`
  tool + prompt/permission tweaks into the `planningAgents` at *plugin-runtime*
  (above markdown ∪ JSON; orthogonal to danno's model overlay). The one
  actionable item: **`submit_plan` is a tool call, so a planning agent routed to a
  local model MUST be tool-capable** — danno should warn otherwise.
- **Real fix is upstream:** file a `docker sandbox` feature request for port
  publishing.

---

## 1. How Plannotator works (researched)

Sources: <https://plannotator.ai/>, the repo `backnotprop/plannotator`
(`apps/opencode-plugin/README.md`, `.../devcontainer.md`,
`apps/marketing/src/content/docs/guides/remote-and-devcontainers.md`), fetched
2026-06-20.

### Plan-review mechanics
- The configured planning agent calls **`submit_plan`** → Plannotator starts a
  **local web server** and opens a browser to it → user selects text and
  annotates (delete / replace / comment) → **Approve** lets the agent proceed,
  **Request changes** returns structured feedback to the agent.
- "**Runs locally. No network requests. Plans never leave your machine.**" Private
  sharing **compresses the plan + annotations into the URL itself** (no backend);
  `share.plannotator.ai` / a paste worker are used only for the optional
  share/short-link path.
- Port: **random locally**, or a **fixed port in remote mode** (`PLANNOTATOR_PORT`,
  default `19432`).

### OpenCode plugin (`@plannotator/opencode`) — the agent touchpoint
Workflow modes (config key `workflow`):
- **`plan-agent`** (our default): registers `submit_plan` for the built-in `plan`
  agent **plus any in `planningAgents`**, and *modifies their prompt + permissions*
  to integrate with plan mode. Keeps `build` from calling it.
- **`manual`**: `submit_plan` not registered — use the slash commands
  (`/plannotator-review`, `/plannotator-annotate`, `/plannotator-last`).
- **`user-managed`**: tool registered, **no prompt/permission changes** — you
  decide which agents can call it via **native OpenCode agent config** (i.e. what
  danno generates). *Aligns with danno owning the JSON agent layer.*
- **`all-agents`**: legacy broad — all primary agents can call it.

Our config (generated from `danno.toml`):
```jsonc
["@plannotator/opencode@latest", { "workflow": "plan-agent", "planningAgents": ["plan"] }]
```

Runtime selection is automatic: **Bun-hosted OpenCode uses the embedded server
bundled with the plugin**; Node-hosted/wrapped falls back to the installed
`plannotator` CLI (`runtime: "cli"` forces it; `PLANNOTATOR_BIN` overrides the
path). → The install-log failure *"Skipping agent terminal runtime install
(npm ERR…)"* is a **different component** (the pi-style agent-terminal runtime);
the `plannotator` CLI itself installed fine, and Bun-hosted OpenCode doesn't need
either. **That npm failure is almost certainly not the blocker.**

### Environment variables (plugin README)
| Variable | Meaning |
|---|---|
| `PLANNOTATOR_REMOTE` | `1`/`true` = remote mode; `0`/`false` = local; unset = SSH auto-detect (`SSH_TTY`/`SSH_CONNECTION`). Remote ⇒ fixed port + manual browser. |
| `PLANNOTATOR_PORT` | Fixed port. Default: random local, `19432` remote. |
| `PLANNOTATOR_BROWSER` | Custom browser/app, **or a script** that opens the URL on another machine / sends a notification with the link. |
| `PLANNOTATOR_SHARE_URL` | Share portal (self-host). Default `https://share.plannotator.ai`. |
| `PLANNOTATOR_PASTE_URL` | Paste service (self-host). Default `https://plannotator-paste.plannotator.workers.dev`. |
| `PLANNOTATOR_PLAN_TIMEOUT_SECONDS` | `submit_plan` wait timeout. Default `345600` (96h); `0` disables. |
| `PLANNOTATOR_BIN` | Override CLI path for the plugin's CLI fallback. |
| `PLANNOTATOR_JINA` | `0` disables the Jina reader used for URL annotation. |

Documented container recipe (VS Code devcontainer): `PLANNOTATOR_REMOTE=1` +
`PLANNOTATOR_PORT=9999` + `"forwardPorts": [9999]`, then open
`http://localhost:9999` on the host. **`forwardPorts`/`-p` is the missing piece
under `docker sandbox`.**

---

## 2. Why it doesn't work under `docker sandbox` today

`docker sandbox` CLI surface (verified 2026-06-20):
- `create`: only `--name`, `--template`, `--pull-template`, `--quiet`. **No `-p`/publish.**
- `network`: `proxy` (`--policy allow|deny`, `--allow/block/bypass-host/-cidr`) and
  `log`. **Egress/MITM only — no ingress / port forwarding.**
- `exec`, `run`: no port flags.

So the host browser cannot reach the in-VM server. Our egress allow-list governs
*outbound* and is irrelevant to this *inbound* need. (See
[[opencode-only-in-docker-sandbox]] for why we never run OpenCode on the host
instead.)

---

## 3. Impact on the agents/cloud refactor

(Cross-ref [`agents-and-cloud-backend-refactor.md`](agents-and-cloud-backend-refactor.md).)

| Interaction | Verdict                                                                                                                                                                                                                                                                                                                        |
|---|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Plugin injects `submit_plan` + prompt/permission tweaks at **plugin-runtime** | **Orthogonal** to danno's JSON `model` overlay (different layer; no precedence conflict). It IS a *third writer* to an agent's effective config: effective = (markdown ∪ JSON) then plugin-runtime tweaks on top.                                                                                                              |
| `planningAgents` references agent **names** | danno's `[agents]` reframe must keep planning-agent names valid; a renamed/removed `plan` silently stops interception.                                                                                                                                                                                                         |
| **`submit_plan` is a tool call** | We removed `tool_call` from `danna.toml`. Previously: ⚠️ **Actionable:** routing a `planningAgent` to a **local model** (the hybrid pitch) requires that model be **tool-capable** (`tool_call = true`), else plan review never fires. danno already tracks `tool_call` per model → **warn on a non-tool-capable planning model.** |
| `workflow: "user-managed"` | Clean seam once danno owns the agent layer: register the tool, let danno's generated agent config decide who may call it. Consider as danno default.                                                                                                                                                                           |

These are documentation + one validation rule — they do **not** change the
refactor's shape.

---

## 4. Decision: `docker sandbox` (+ tunnel) vs plain `docker`

This is a **foundational** choice, not a plannotator tweak — danno's whole design
sits on `docker sandbox`.

| Dimension | `docker sandbox` + stdio tunnel | Switch to plain `docker` |
|---|---|---|
| plannotator port | tunnel hack (unverified) | **native `-p 9999:9999`** |
| other host-reachable services (`opencode web` :4096, dev servers) | tunnel per service | native `-p` |
| isolation | **microVM (strong)** | container/shared-kernel (weaker) unless you add gVisor/Kata/Firecracker |
| agent images | **maintained by Docker** ([[docker-sandbox-agents]]) | you build/maintain Dockerfiles per agent |
| egress security | **built-in proxy + policy CLI** | DIY (proxy sidecar + iptables; fail-open risk) |
| danno code change | tiny (tunnel helper + env) | **large**: rewrite sandbox layer + image build/publish |
| portability / CI | needs Docker Desktop sandbox feature | runs anywhere docker runs |
| maturity / lock-in | young, Docker-Desktop-only, no port-publish *yet* | stable, ubiquitous |

**Asymmetry that decides it:** the tunnel pokes *one loopback hole* for a dev UI;
switching runtimes drops the *entire VM boundary* and the maintained-image +
egress-firewall stack. Hugely disproportionate to fix one peripheral tool.

**Recommendation:** keep `docker sandbox`; make plannotator work via the tunnel
(or share-mode); file the upstream port-publish request. **Flip to plain `docker`
only if** host-reachable services become central *and* you accept rebuilding
isolation/images/egress (or weaker isolation).

### Alternatives considered (kept for the record)
- **Share-URL mode** (egress-only): `PLANNOTATOR_REMOTE=1` + a `PLANNOTATOR_BROWSER`
  script that emits a self-contained share link (needs egress allow to
  `share.plannotator.ai` + the paste worker). No tunnel, no docker switch, but
  the round-trip likely degrades to link/manual — **unverified** whether feedback
  returns serverless. Good fallback.
- **`workflow: "manual"`**: slash-commands only, no auto-interception. Lowest-
  friction "doesn't break" baseline.
- **Wait for upstream port publishing**: the real fix; not available now.

---

## 5. Plan — the corrected, danno-native tunnel (NOT YET BUILT)

Fixes the third-party plan's real bug (env set in a *sibling* exec shell never
reaches the OpenCode process). Two-tier policy applies: **advise by default,
execute under `--apply`** ([[danno-unified-install-design]]).

### Target UX
```bash
# 1. Launch opencode with the env in ITS OWN process (danno injects via the chmod-600 env-file):
danno sandbox start --target . \
  --env PLANNOTATOR_REMOTE=1 --env PLANNOTATOR_PORT=9999 \
  --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY --env NVIDIA_API_KEY=$NVIDIA_API_KEY

# 2. Host-side tunnel (separate terminal):
danno sandbox tunnel <name> 9999       # wraps the socat loop below

#    …equivalently, raw:
socat TCP-LISTEN:9999,fork,reuseaddr \
  EXEC:'docker sandbox exec -i danno-<name> nc 127.0.0.1 9999'

# 3. Trigger a plan → open http://localhost:9999 on the host.
```

### Phases
- **Phase 0 — verify the load-bearing unknowns (do FIRST).**
  - (a) Is `docker sandbox exec -i` an **8-bit-clean** stream? Probe: in-VM
    `nc -l` echo server, tunnel via socat, push binary both ways, diff. (Classic
    `docker exec` stdcopy-frames non-tty output — must confirm the sandbox CLI
    hands socat clean, demuxed stdout with stderr kept off-socket.)
  - (b) **Websocket / multi-connection** behaviour: does the review UI hold over
    `fork`+per-connection `exec` spawns; is latency tolerable?
  - (c) Does the **embedded (Bun) server honour `PLANNOTATOR_REMOTE`/`PORT`**, or
    only the CLI runtime? If only CLI, also set `runtime: "cli"` + `PLANNOTATOR_BIN`.
  - If (a) fails → tunnel is dead; fall back to share-mode / `manual`.
- **Phase 1 — env injection.** Already generic: `danno sandbox start --env …`
  flows into `_exec_session`'s env-file, so the vars land on the OpenCode process.
  Optionally add a convenience (`danno.toml` `[plannotator]` or a `--plannotator`
  flag) that sets `PLANNOTATOR_REMOTE=1`/`PORT` automatically. Decision pending.
- **Phase 2 — provision `nc`/`socat` in the VM** (idempotent). The opencode
  prebuilt image may lack `netcat-openbsd`. Hook into provisioning (an
  install/setup step), NOT a manual `apt` — must survive rebuilds and obey
  fail-loud if the package can't be installed. (Prefer `socat - TCP:127.0.0.1:port`
  in-VM if socat is present, avoiding `nc` entirely.)
- **Phase 3 — `danno sandbox tunnel <name> <port>` helper.** Advise-by-default
  (print the exact `socat … EXEC:'docker sandbox exec -i … '` command); run under
  `--apply`. Keep it a thin, mockable I/O wrapper (Working Rule 7). Resolve the
  sandbox name the same way `start`/`shell` do.
- **Phase 4 — docs + the agent-layer validation.** README sandbox section
  (the tunnel + the "open `http://localhost:<port>`" step + the egress-proxy
  bypass note); `danno.toml.example` note near the `@plannotator/opencode`
  `[[npm]]` entry; and the **tool-capable planning-model warning** from §3
  (wire into the agents-layer collision check).
- **Phase 5 — upstream.** File the `docker sandbox` port-publish feature request;
  if it lands, retire the tunnel.

### Risks / constraints to honour
- Tunnel **bypasses the egress proxy** (host→VM-loopback, outside the network
  policy). Narrow and deliberate, but document it as a known hole.
- Per-connection `exec` spawn is heavy (hundreds of ms); fine for a dev UI, not
  for high-throughput.
- Keep everything **reversible & non-destructive**; the tunnel is opt-in and
  leaves the sandbox security model otherwise intact.

---

## 6. Appendix — reproducible probe commands

Sandbox already provisioned as `danno-temp-example-project`. All non-TTY.

```bash
# §2 — confirm no port-publish surface
docker sandbox create --help          # only --name/--template/--pull-template/--quiet
docker sandbox network --help         # only: proxy, log
docker sandbox network proxy --help   # allow/block/bypass host|cidr, --policy

# Phase 0(a) — exec stdio binary-cleanliness (run when testing the tunnel):
#   in VM:   nc -l -p 9999 -k > /tmp/got &        (or socat)
#   host:    socat TCP-LISTEN:9999,fork,reuseaddr EXEC:'docker sandbox exec -i danno-temp-example-project nc 127.0.0.1 9999'
#   host:    head -c 1M /dev/urandom | tee ref | nc 127.0.0.1 9999 ; diff <(...) ref

# real flow (after Phase 0 passes): launch with env, trigger a plan, open http://localhost:9999
danno sandbox start --target /Users/mikestitt/projects/temp/example-project \
  --env PLANNOTATOR_REMOTE=1 --env PLANNOTATOR_PORT=9999 \
  --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY --env NVIDIA_API_KEY=$NVIDIA_API_KEY
```
