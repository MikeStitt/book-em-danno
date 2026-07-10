# How sbx egress ("the firewall") works, and how danno reaches local Ollama — for review

**Date:** 2026-07-10 · **Author:** Claude (Opus 4.8), from a live investigation on
`sbx v0.34.0`, macOS (Apple Silicon), Docker Desktop. · **Purpose:** an
independent-review record of my current understanding — the model, the evidence, the
plan, and **explicitly what is test-verified vs inferred**, so a reviewer can find my
mistakes. (Context: earlier in the same session I reached several *wrong* "certain"
conclusions that only testing caught — see "Where I could still be wrong".)

> **Read this first — the one methodology rule.** Judge reachability by the **HTTP
> status code**, never by a tool's exit code. `curl` without `-f` exits **0 on a 403**,
> so "curl succeeded" ≠ "reached". All allow/deny claims below use
> `curl -s -o /dev/null -w '%{http_code}'`: **200 = reached, 403 = proxy-denied,
> 000 = connection failed (bypassed the proxy / nothing listening)**. My first big wrong
> conclusion ("sbx doesn't isolate") came from reading curl's exit code instead.

## 1. The model (how I think it works, and why)

**Each sbx sandbox is a microVM with its own network namespace.** The only way out is a
**host-side HTTP(S) proxy** — Docker's docs say exactly this: *"The only way traffic can
leave a sandbox is through an HTTP/HTTPS proxy on your host, which enforces access rules
on every outbound request."* UDP and ICMP are blocked at the network layer; non-HTTP TCP
needs explicit allow rules.

Inside the sandbox (observed via `sbx exec … env`):

```
HTTP_PROXY  = HTTPS_PROXY  = http://gateway.docker.internal:3128   (also lowercase)
NO_PROXY    = no_proxy     = localhost,127.0.0.1,::1,gateway.docker.internal
```

So: a well-behaved HTTP client sends everything to `gateway.docker.internal:3128`
**except** the `NO_PROXY` hosts, which it connects to directly.

**Policy** (`sbx policy …`): a **global base** (`sbx policy init allow-all|balanced|
deny-all`) plus **allow/deny rules** (global or `--sandbox <name>`-scoped). **Deny beats
allow.** Rule resources are hostnames/domains/IPs with optional `:port`; `**` = all
hosts. Crucially, **`balanced` is not network-default-allow**: `sbx policy ls` shows it
grants two curated allow-lists — `default-ai-services` (e.g. `api.anthropic.com:443`,
`**.openai.com:443`) and `default-package-managers` (pypi, npm, crates, …:443) — and
**nothing else on the network is permitted** (arbitrary hosts are denied). (It also has
`filesystem read/write allow-all`, unrelated to egress.)

**Why enforcement is at the proxy, not the kernel:** the proxy inspects each request's
target host:port against the policy and returns **403** for a denied one. This is why the
*routing* of a request (does it go through the proxy at all?) is decisive — a request
that bypasses the proxy is neither filtered **nor** delivered to the host.

**The proxy connects to targets from the *host's* network position.** This is the single
most important consequence and the thing I got wrong twice. The proxy is host-side, so
when it is asked to reach `X:port`, it dials `X:port` **as the host would** — including
the host's own loopback.

## 2. Evidence (the test matrix)

All rows: base policy `balanced`, one sandbox, a single per-sandbox allow rule, requests
from inside via `sbx exec`, read as HTTP codes.

| Allow rule | Request | Routing | Code | Meaning |
|---|---|---|---|---|
| `10.0.1.9:11434` (Mac LAN IP) | `http://10.0.1.9:11434/api/tags` | proxied (not in NO_PROXY) | **200** | reached Ollama |
| `10.0.1.9:11434` | `https://example.com` | proxied | **403** | internet denied |
| `10.0.1.9:11434` | `http://10.0.1.1` (LAN router) | proxied | **403** | LAN denied |
| `10.0.1.9:11434` | `http://10.0.1.9:22` (SSH, same host) | proxied | **403** | **port-scoped** |
| `127.0.0.1:11434` | `http://127.0.0.1:11434/api/tags` **forced `--proxy`** | proxied | **200** | reached Ollama via host loopback |
| `127.0.0.1:11434` | `http://127.0.0.1:11434/api/tags` (default) | **NO_PROXY → direct** | **000** | hit the sandbox's own loopback |
| `127.0.0.1:11434` | `http://127.0.0.1:22` forced `--proxy` | proxied | **403** | port-scoped |
| *(none)* | `http://127.0.0.1:11434` forced `--proxy` | proxied | **403** | it's the policy, not an open proxy |

Name resolution **inside** the sandbox (observed): `host.docker.internal → fe80::1`
(link-local), `gateway.docker.internal → fd91:…::` (the proxy host, ULA), `localhost →
::1`, `127.0.0.1 → 127.0.0.1` (the sandbox's own loopback).

## 3. Why the docker-style aliases DON'T reach Ollama on sbx

- **`docker sandbox` (legacy) rewrote `host.docker.internal`→`localhost` on the host
  side**, so its allow-list token `localhost:11434` mapped to the host's Ollama. **sbx has
  no such rewrite** — this is the whole gap (upstream feature request:
  [NVIDIA/OpenShell#263](https://github.com/NVIDIA/OpenShell/issues/263)).
- On sbx, `host.docker.internal` resolves to `fe80::1` — a link-local address the policy
  can't match and that doesn't reach Ollama; allowing `host.docker.internal:11434` +
  dialing it returned **403**.
- `gateway.docker.internal` is the proxy host; Ollama isn't on it (`:11434 → 000`).
- `localhost`/`127.0.0.1` **direct** are the sandbox's *own* loopback (a different netns
  from the host) → **000**.

## 4. How to reach local Ollama, and why (the mechanism)

Ollama on the Mac binds `0.0.0.0:11434`, so it listens on **every** host interface,
including the host's loopback `127.0.0.1`. The sbx proxy is host-side. Therefore:

> **Route `127.0.0.1:11434` through the host proxy** → the proxy connects to
> `127.0.0.1:11434` **from the host** → the host's own loopback → **Ollama** → 200.

Verified (table §2). And it is **policy-enforced** (no rule → 403) and **port-scoped**
(`:22` → 403). This is *better* than the LAN-IP approach: **network-independent** (no LAN
IP that changes with wifi/VPN), **works offline** (loopback always exists on the host),
and unambiguously *this host* (a LAN IP could be another machine).

**Two things are therefore required to reach a same-host Ollama:**

1. **Allow `127.0.0.1:11434`** in the sbx policy. *(danno does this today — `configure_proxy`
   resolves a local Ollama alias to `127.0.0.1`. Verified through `provision()`:
   `127.0.0.1:11434 → 200`, `example.com`/LAN/`:22` → 403.)*
2. **Make the harness's HTTP traffic to `127.0.0.1:11434` go THROUGH the proxy** rather
   than bypass it (default `NO_PROXY` contains `127.0.0.1`, so a naive client bypasses to
   the sandbox's empty loopback = 000). **This is the unfinished part.**

## 5. Why requirement #2 is hard (verified findings)

Making a client route `127.0.0.1` through the proxy needs the client to (a) have
`127.0.0.1` removed from its effective `NO_PROXY`, **and** (b) actually honor `HTTP_PROXY`.

- **`sbx exec --env-file NO_PROXY=…` is IGNORED** — inside the exec, `NO_PROXY` still
  resolved to sbx's injected value (`localhost,127.0.0.1,::1,gateway.docker.internal`). So
  danno cannot set it via the env-file it already uses.
- **An in-shell `export` works, but must set BOTH `NO_PROXY` and `no_proxy`** — setting
  only uppercase gave **000** (curl honored the still-`127.0.0.1` lowercase `no_proxy`);
  setting both gave **200**.
- **Even with `NO_PROXY` fixed, only `HTTP_PROXY`-honoring clients benefit.** `curl` does.
  The harness clients are the problem: **opencode (Node), claurst (Rust), occ (Node)**. I
  have *not re-tested* these this session, but danno's existing architecture is strong
  evidence they do NOT honor it: **claurst and occ already dial an in-sandbox relay** at
  `127.0.0.1:11434` precisely because their clients ignore the proxy env (claurst's Rust
  client is documented in-code as ignoring `HTTP(S)_PROXY`).

## 6. The proposed fix (the relay) — planned, NOT yet implemented/verified

danno already runs a small Python **relay** inside the sandbox listening on
`127.0.0.1:11434` (`driver.py:_OLLAMA_RELAY_SOURCE`), used by claurst and occ. The harness
dials `127.0.0.1:11434` **directly** (it's in `NO_PROXY`, so this hop needs no proxy — it
reaches the relay in the sandbox). The relay, a process danno controls, then forwards to
the host Ollama **through the proxy** (it can force this in its own `urllib`).

Today the relay's `UPSTREAM = http://host.docker.internal:11434` → broken on sbx. The
change:

1. Add `DANNO_RELAY_UPSTREAM_HOST` (default `host.docker.internal`); the claurst/occ
   launchers set it to **`127.0.0.1`** when the backend is sbx.
2. In the relay, when a proxy is configured, **clear `no_proxy`/`NO_PROXY`** so
   `urllib.ProxyHandler` forces even `127.0.0.1` through the proxy (otherwise urllib's
   proxy-bypass sends it to the sandbox's own loopback = the relay itself → loop). No-op on
   docker (its upstream isn't loopback).
3. **opencode** does *not* use the relay (it relied on the docker proxy-rewrite). On sbx,
   route opencode through the relay too (base_url `http://127.0.0.1:11434/v1` + launch the
   relay), unless a real turn shows opencode honors `HTTP_PROXY` (I doubt it).

**Verification gate (per harness):** a real model turn returns output **and** the boundary
still shows `example.com`/LAN → 403.

## 7. Remote Ollama (Ollama on a different machine)

Loopback can't reach another host. For a remote Ollama the sandbox must name the remote's
**real routable IP:port** (a concrete base_url, which danno passes through literally and
allows). Verified analog: allowing the Mac's LAN IP `10.0.1.9:11434` and dialing it → 200,
while the LAN router and other ports stay 403.

## 8. What is VERIFIED vs INFERRED

**Verified this session (HTTP codes / observed env, on `sbx v0.34.0`/macOS):**
- Egress is proxy-enforced; `balanced` is default-deny for arbitrary hosts (§1, §2).
- A per-sandbox allow of exactly `host:port` permits that and denies everything else,
  including other ports on the same host (port-scoped).
- `127.0.0.1:11434` **through the proxy** reaches the host's Ollama (200); direct → 000;
  no allow → 403 (§2, §4).
- Name resolutions of the docker aliases inside the sandbox (§2).
- `--env-file NO_PROXY` is overridden; in-shell `export` needs both cases (§5).
- danno's `configure_proxy` emits `127.0.0.1:11434` and the boundary holds through
  `provision()` (§4).

**Inferred, NOT re-tested this session (a reviewer should challenge these):**
- That opencode/occ/claurst clients don't honor `HTTP_PROXY` — based on danno's existing
  relay design + in-code notes, not a fresh test.
- That the relay change (§6) will work end-to-end — reasoned from the mechanism, not run.
- That the proxy runs literally on the Mac host process vs a dedicated gateway VM — I say
  "host-side"; the ULA address for `gateway.docker.internal` suggests a gateway netns, but
  the *behavior* (it reaches the host's `127.0.0.1`) is what's verified, not the topology.

## 9. Where I could still be wrong (invitation to the reviewer)

- **Does the relay's `urllib` actually force `127.0.0.1` through the proxy after clearing
  `no_proxy`?** Reasoned, not tested. If `ProxyHandler` still bypasses loopback, the relay
  loops on itself.
- **Does opencode honor `HTTP_PROXY`?** If yes, the simpler env route (base_url +
  both-case `NO_PROXY` via an in-shell export wrapper) could avoid routing opencode through
  the relay.
- **Is allowing host-loopback egress an SSRF-class risk?** I argue no: it's port-scoped
  (`:22`→403) and identical in kind to allowing the host's LAN Ollama, which is the
  intended hole. But a reviewer should confirm the policy can't be widened by a wildcard.
- **`sbx` is experimental ("the API will change").** Everything here is `v0.34.0`-specific
  and may shift.

## 10. How to reproduce (minimal)

```bash
# boundary probe (run each request THROUGH the proxy to test the policy, not NO_PROXY):
N=t; sbx create --name $N shell /tmp; sbx policy allow network --sandbox $N 127.0.0.1:11434; sbx stop $N
sbx exec $N sh -lc 'for u in http://127.0.0.1:11434/api/tags http://127.0.0.1:22 https://example.com; do
  c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 --noproxy "" --proxy http://gateway.docker.internal:3128 "$u");
  echo "$u -> $c"; done'   # expect 200 / 403 / 403
sbx rm --force $N
```

Related: [`plan-sbx-migration.md`](plan-sbx-migration.md) (full migration + Phase-2 plan),
[`sbx-workarounds.md`](sbx-workarounds.md) (the declared, removable accommodations), and
the `sandbox-security-contract-fail-loud` memory (the isolation invariant + the
verify-by-403 lesson).
